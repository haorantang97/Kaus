"""Translate public Alma v0.4.148 local wire contracts, without upstream code.

Each Kaus conversation owns one Alma thread. Native account configuration stays
inside Alma. Only explicit model selection changes a thread; no provider or
global security setting is modified. Chat mode sends noTools on every turn.
Tool mode uses Alma's own policy and forwards the approval requests it emits.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit
import uuid

import httpx
from websockets.asyncio.client import connect

from drivers.error_text import safe_error_text
from drivers.stdio_bridge import BridgeError, Peer


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise BridgeError('Alma requires a loopback HTTP service URL', -32602)
    return value.rstrip('/')


def message_parts(blocks: list[dict]) -> list[dict]:
    parts = []
    for block in blocks:
        kind = block.get('type')
        if kind == 'text':
            parts.append({'type': 'text', 'text': str(block.get('text', ''))})
        elif kind == 'image' and isinstance(block.get('data'), str):
            import base64
            try:
                base64.b64decode(block['data'], validate=True)
            except ValueError:
                raise BridgeError('Invalid image data', -32602) from None
            mime = block.get('mimeType', 'image/png')
            if not isinstance(mime, str) or not mime.startswith('image/'):
                raise BridgeError('Invalid image content type', -32602)
            parts.append({'type': 'file', 'mediaType': mime, 'url': f"data:{mime};base64,{block['data']}"})
        elif kind == 'resource' and isinstance(block.get('resource', {}).get('text'), str):
            resource = block['resource']
            parts.append({'type': 'text', 'text': f"{resource.get('uri', '')}\n{resource['text']}"})
        elif kind == 'resource_link':
            parts.append({'type': 'text', 'text': f"{block.get('name', '')}: {block.get('uri', '')}"})
        else:
            raise BridgeError('Unsupported Alma attachment type', -32602)
    return parts


class Session:
    def __init__(self, bridge: 'Bridge', ident: str, record: dict):
        self.bridge, self.ident, self.record = bridge, ident, record
        self.sockets: dict = {}
        self.readers: list[asyncio.Task] = []
        self.interactions: dict[str, asyncio.Task] = {}
        self.turn: asyncio.Future | None = None
        self.cancelled = False
        self.broken = False
        self.native_busy = False
        self.generation_succeeded = False
        self.native_idle = False
        self.tools: dict[tuple[str, int], str] = {}
        self.lock = asyncio.Lock()

    @property
    def native_id(self):
        return self.record['threadId']

    @property
    def path(self):
        return '/api/threads/' + quote(self.native_id, safe='')

    def update(self, value: dict):
        self.bridge.peer.notify('session/update', {'sessionId': self.ident, 'update': value})

    def result(self):
        return {'sessionId': self.ident, 'models': {
            'currentModelId': self.record['model'], 'availableModels': self.bridge.models},
            'modes': {'currentModeId': self.record.get('mode', 'chat'), 'availableModes': [
                {'id': 'chat', 'name': '对话'}, {'id': 'tools', 'name': '工具'}]}}

    async def open(self):
        try:
            for channel in ('threads', 'handoff/tool-approvals', 'handoff/user-questions'):
                ws = await connect(self.bridge.url.replace('http:', 'ws:', 1) + '/ws/' + channel,
                                   open_timeout=10, max_size=16 * 1024 * 1024, proxy=None)
                self.sockets[channel] = ws
                # The initial snapshot fences off a generation left running by a lost client.
                if channel == 'threads':
                    initial = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if initial.get('type') != 'generating_snapshot':
                        raise BridgeError('This Alma version does not expose a generation snapshot')
                    if self.native_id in initial.get('data', {}).get('ids', []):
                        raise BridgeError('This Alma thread is still running; stop it in Alma before resuming')
                self.readers.append(asyncio.create_task(self.read(channel, ws)))
        except Exception:
            await self.close()
            raise

    async def read(self, channel, ws):
        try:
            async for raw in ws:
                event = json.loads(raw)
                if channel == 'threads':
                    self.event(event)
                elif event.get('type') == 'show':
                    request = event.get('request', {})
                    if request.get('threadId') != self.native_id:
                        continue
                    ident = request.get('requestId')
                    if not isinstance(ident, str) or ident in self.interactions:
                        continue
                    task = asyncio.create_task(self.interact(channel, request))
                    self.interactions[ident] = task
                    task.add_done_callback(lambda done, key=ident: self.interactions.pop(key, None))
                elif event.get('type') == 'resolved':
                    task = self.interactions.pop(event.get('decision', {}).get('requestId'), None)
                    if task:
                        task.cancel()
            raise BridgeError('Alma event stream disconnected')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.broken = True
            if self.turn and not self.turn.done():
                self.turn.set_exception(BridgeError('Alma connection lost before completion; resume to reconnect'))

    def event(self, envelope):
        data = envelope.get('data') or {}
        kind = envelope.get('type')
        own = data.get('threadId', data.get('id')) == self.native_id
        if own and kind == 'thread_generating':
            self.native_busy = bool(data.get('isGenerating'))
        if not self.turn or self.turn.done():
            return
        if kind == 'error':
            self.turn.set_exception(BridgeError(safe_error_text(data.get('error', 'Alma request failed'))))
            return
        if not own:
            return
        if kind == 'generation_error':
            self.broken = True
            self.turn.set_exception(BridgeError(safe_error_text(data.get('error', 'Alma generation failed'))))
        elif kind in ('generation_completed', 'thread_generating'):
            if kind == 'generation_completed':
                self.generation_succeeded = True
            elif data.get('isGenerating') is False:
                self.native_idle = True
            if self.native_idle and (self.generation_succeeded or self.cancelled):
                self.turn.set_result({'stopReason': 'cancelled' if self.cancelled else 'end_turn'})
        elif kind == 'message_delta':
            for delta in data.get('deltas', []):
                typ = delta.get('type')
                if typ == 'text_append' and isinstance(delta.get('text'), str):
                    self.update({'sessionUpdate': 'agent_thought_chunk' if delta.get('partType') == 'reasoning' else 'agent_message_chunk',
                                 'content': {'type': 'text', 'text': delta['text']}})
                elif typ == 'part_add':
                    part = delta.get('part', {})
                    part_type = part.get('type', '')
                    if part_type.startswith('tool-'):
                        ident = str(part.get('toolCallId') or f"{data.get('messageId')}:{delta.get('partIndex')}")
                        self.tools[(str(data.get('messageId')), delta.get('partIndex'))] = ident
                        self.update({'sessionUpdate': 'tool_call', 'toolCallId': ident,
                                     'title': part.get('toolName') or part_type.removeprefix('tool-'),
                                     'status': 'in_progress', 'rawInput': part.get('input', {})})
                    elif part_type == 'file':
                        uri, mime = part.get('url', ''), part.get('mediaType', 'application/octet-stream')
                        if isinstance(uri, str) and uri.startswith(('https://', 'http://', 'data:')):
                            if uri.startswith('data:') and mime.startswith('image/') and ';base64,' in uri:
                                self.update({'sessionUpdate': 'agent_message_chunk', 'content': {
                                    'type': 'image', 'mimeType': mime, 'data': uri.split(';base64,', 1)[1]}})
                            elif uri.startswith(('https://', 'http://')):
                                self.update({'sessionUpdate': 'agent_message_chunk', 'content': {
                                    'type': 'resource_link', 'uri': uri, 'mimeType': mime, 'name': part.get('filename', '文件')}})
                elif typ in ('tool_output_set', 'tool_output_streaming'):
                    ident = self.tools.get((str(data.get('messageId')), delta.get('partIndex')))
                    if ident:
                        content = delta.get('output', delta.get('stream', {}))
                        self.update({'sessionUpdate': 'tool_call_update', 'toolCallId': ident,
                                     'status': ('failed' if delta.get('state') == 'output-error' else 'completed') if typ == 'tool_output_set' else 'in_progress',
                                     'content': [{'type': 'content', 'content': {'type': 'text', 'text': json.dumps(content, ensure_ascii=False)}}]})

    async def interact(self, channel, request):
        approval = channel.endswith('tool-approvals')
        response = {'action': 'deny' if approval else 'cancel'}
        try:
            if not self.turn or self.turn.done() or self.cancelled:
                return
            if approval:
                result = await self.bridge.peer.call('session/request_permission', {
                    'sessionId': self.ident, 'toolCall': {'toolCallId': request['requestId'],
                    'title': request.get('title') or 'Alma', 'rawInput': {'description': request.get('message', '')}},
                    'options': [{'optionId': 'allow_once', 'name': '允许', 'kind': 'allow_once'},
                                {'optionId': 'deny', 'name': '拒绝', 'kind': 'reject_once'}]})
                outcome = result.get('outcome', {})
                if outcome.get('outcome') == 'selected' and outcome.get('optionId') == 'allow_once':
                    response = {'action': 'allow_once'}
            else:
                questions = []
                for index, question in enumerate(request.get('questions', [])):
                    choices = [o['label'] for o in question.get('options', []) if isinstance(o, dict) and isinstance(o.get('label'), str)]
                    questions.append({'id': str(index), 'prompt': question.get('question', ''),
                        'options': [{'id': v, 'label': v} for v in choices],
                        'allowMultiple': bool(question.get('multiSelect')),
                        'allowFreeText': bool(question.get('allowCustomAnswer') or not choices)})
                result = await self.bridge.peer.call('_alma/questions', {'sessionId': self.ident, 'questions': questions})
                outcome = result.get('outcome', {})
                if outcome.get('outcome') != 'answered':
                    return
                rows = outcome.get('answers', [])
                if len(rows) != len(questions):
                    return
                answers = []
                for question, row in zip(questions, rows):
                    if row.get('questionId') != question['id']:
                        return
                    selected = row.get('selectedOptionIds', [])
                    if not isinstance(selected, list) or any(v not in {o['id'] for o in question['options']} for v in selected):
                        return
                    custom = row.get('text')
                    if custom is not None and (not question['allowFreeText'] or not isinstance(custom, str)):
                        return
                    answers.append({'selected': selected, **({'custom': custom} if custom is not None else {})})
                response = {'action': 'accept', 'answers': answers}
        except asyncio.CancelledError:
            response = {'action': 'deny' if approval else 'cancel'}
        except Exception:
            response = {'action': 'deny' if approval else 'cancel'}
        finally:
            if self.cancelled:
                response = {'action': 'deny' if approval else 'cancel'}
            with contextlib.suppress(Exception):
                await self.bridge.request('POST', '/api/' + channel, {'requestId': request['requestId'], 'response': response})

    async def prompt(self, blocks):
        parts = message_parts(blocks)
        async with self.lock:
            if self.turn and not self.turn.done():
                raise BridgeError('This Alma session already has a running turn', -32602)
            if self.broken:
                raise BridgeError('Resume the Alma session to reconnect')
            if self.native_busy:
                raise BridgeError('This Alma thread is running in another client', -32602)
            if not self.record['model']:
                raise BridgeError('Configure a model in Alma before chatting', -32000)
            self.cancelled = False
            self.native_idle = False
            self.generation_succeeded = False
            self.tools.clear()
            self.turn = asyncio.get_running_loop().create_future()
            try:
                await self.sockets['threads'].send(json.dumps({'type': 'generate_response', 'data': {
                    'threadId': self.native_id, 'model': self.record['model'], 'userMessage': {'role': 'user', 'parts': parts},
                    'noTools': self.record.get('mode', 'chat') != 'tools', 'enabledMCPServerIds': []}}))
            except Exception:
                self.broken = True
                self.turn.cancel()
                raise BridgeError('Alma disconnected while sending; resume before retrying') from None
        try:
            return await asyncio.wait_for(asyncio.shield(self.turn), self.bridge.turn_timeout)
        except asyncio.TimeoutError:
            await self.cancel()
            raise BridgeError('Alma turn timed out') from None
        finally:
            tasks = list(self.interactions.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel(self):
        if not self.turn or self.turn.done():
            return
        self.cancelled = True
        for task in list(self.interactions.values()):
            task.cancel()
        await self.sockets['threads'].send(json.dumps({'type': 'stop_generation', 'data': {'threadId': self.native_id}}))
        try:
            await asyncio.wait_for(asyncio.shield(self.turn), 8)
        except asyncio.TimeoutError:
            self.broken = True
            raise BridgeError('Alma has not confirmed stopping; check the thread in Alma') from None

    async def close(self):
        if self.turn and not self.turn.done():
            with contextlib.suppress(Exception):
                await self.cancel()
        for task in [*self.readers, *self.interactions.values()]:
            task.cancel()
        await asyncio.gather(*self.readers, *self.interactions.values(), return_exceptions=True)
        for ws in self.sockets.values():
            await ws.close()


class Bridge:
    def __init__(self, url: str, state: Path, peer: Peer, turn_timeout=1800):
        self.url, self.state, self.peer = local_url(url), state, peer
        self.turn_timeout = turn_timeout
        self.client = httpx.AsyncClient(base_url=self.url, timeout=20, trust_env=False, follow_redirects=False)
        self.sessions: dict[str, Session] = {}
        self.models: list[dict] = []

    async def request(self, method, path, body=None):
        try:
            result = await self.client.request(method, path, json=body)
        except httpx.HTTPError:
            raise BridgeError('Alma local service is unavailable; start Alma and enable its local API') from None
        if result.status_code >= 400:
            raise BridgeError(f'Alma request failed (HTTP {result.status_code})')
        return result.json() if result.content else {}

    def record_path(self, ident):
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise BridgeError('Unknown Alma session', -32602) from None
        return self.state / (ident + '.json')

    def save(self, session):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.record_path(session.ident)
        temporary = path.with_suffix('.tmp')
        with open(temporary, 'w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(session.record, stream, ensure_ascii=False)
        temporary.replace(path)

    async def catalog(self):
        providers = await self.request('GET', '/api/providers')
        self.models = []
        for provider in providers if isinstance(providers, list) else []:
            if provider.get('enabled') is False:
                continue
            labels = {m['id']: m.get('name', m['id']) for m in provider.get('availableModels', []) if isinstance(m, dict) and isinstance(m.get('id'), str)}
            for entry in provider.get('models', []):
                model = entry if isinstance(entry, str) else entry.get('id') if isinstance(entry, dict) else None
                if not isinstance(model, str) or not model:
                    continue
                self.models.append({'modelId': f"{provider['id']}:{model}", 'name': f"{provider.get('name', provider['id'])} · {labels.get(model, model)}"})

    async def dispatch(self, method, params):
        if method == 'initialize':
            return {'protocolVersion': 1, 'agentInfo': {'name': 'kaus-alma-bridge', 'version': '1'},
                    'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'resume': {}}, 'promptCapabilities': {'image': True}}, 'authMethods': []}
        if method in ('session/new', 'session/load', 'session/resume'):
            if params.get('mcpServers'):
                raise BridgeError('Alma does not support per-session MCP projection', -32602)
            cwd = str(Path(params['cwd']).resolve())
            await self.catalog()
            if method == 'session/new':
                workspaces = await self.request('GET', '/api/workspaces')
                workspace = next((w for w in workspaces if w.get('path') == cwd and not w.get('remoteHostId')), None)
                if workspace is None:
                    workspace = await self.request('POST', '/api/workspaces', {'path': cwd, 'name': Path(cwd).name, 'isTemporary': False})
                settings = await self.request('GET', '/api/settings')
                model = settings.get('chat', {}).get('defaultModel') or (self.models[0]['modelId'] if self.models else '')
                native = await self.request('POST', '/api/threads', {'title': 'Kaus', 'workspaceId': workspace['id'], 'model': model, 'enableArtifacts': False})
                ident = str(uuid.uuid4())
                record = {'threadId': native['id'], 'cwd': cwd, 'model': native.get('model') or model, 'mode': 'chat', 'url': self.url}
            else:
                ident = params.get('sessionId')
                path = self.record_path(ident)
                if not path.is_file():
                    raise BridgeError('Alma session record is missing; a new thread was not created', -32602)
                record = json.loads(path.read_text())
                if record.get('cwd') != cwd or record.get('url') != self.url:
                    raise BridgeError('Alma session belongs to a different service or workspace', -32602)
                native = await self.request('GET', '/api/threads/' + quote(record['threadId'], safe=''))
                record['model'] = native.get('model') or record['model']
                if ident in self.sessions:
                    await self.sessions.pop(ident).close()
            session = Session(self, ident, record)
            await session.open()
            self.sessions[ident] = session
            self.save(session)
            return session.result()
        session = self.sessions.get(params.get('sessionId'))
        if session is None:
            raise BridgeError('Unknown Alma session', -32602)
        if method == 'session/prompt':
            return await session.prompt(params.get('prompt', []))
        if method == 'session/cancel':
            await session.cancel()
            return {}
        if method in ('session/set_mode', 'session/set_model'):
            async with session.lock:
                if session.broken:
                    raise BridgeError('Resume the Alma session to reconnect')
                if session.turn and not session.turn.done():
                    raise BridgeError('Wait until the current Alma turn has stopped', -32602)
                if method == 'session/set_mode':
                    if params.get('modeId') not in ('chat', 'tools'):
                        raise BridgeError('Unsupported Alma permission mode', -32602)
                    session.record['mode'] = params['modeId']
                else:
                    model = params.get('modelId')
                    if model not in {m['modelId'] for m in self.models}:
                        raise BridgeError('Unknown Alma model', -32602)
                    native = await self.request('PUT', session.path, {'model': model})
                    if native.get('model') != model:
                        raise BridgeError('Alma did not apply the selected model')
                    session.record['model'] = model
                self.save(session)
                return {}
        if method == 'session/close':
            await session.close()
            self.sessions.pop(session.ident, None)
            return {}
        raise BridgeError('Method not supported', -32601)

    async def close(self):
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()
        await self.client.aclose()
