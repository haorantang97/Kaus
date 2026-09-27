"""Translate one native session stream; never infer thought or model output."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from drivers.error_text import safe_error_text
from drivers.stdio_bridge import BridgeError


def file_content(part: dict) -> dict:
    url, mime = part.get('url', ''), part.get('mime', 'application/octet-stream')
    if url.startswith('data:') and ';base64,' in url:
        header, data = url.split(';base64,', 1)
        mime = header[5:]
        try:
            base64.b64decode(data, validate=True)
        except ValueError:
            raise BridgeError('Native output has invalid file encoding') from None
        if mime.startswith('image/'):
            return {'type': 'image', 'mimeType': mime, 'data': data}
        return {'type': 'resource', 'resource': {
            'uri': part.get('filename') or 'output', 'mimeType': mime, 'blob': data}}
    if Path(url).is_absolute():
        url = Path(url).as_uri()
    return {'type': 'resource_link', 'uri': url, 'mimeType': mime,
            'name': part.get('filename') or url.rsplit('/', 1)[-1] or '文件'}


class Events:
    def __init__(self, session):
        self.session = session
        self.seq = -1
        self.tools = {}
        self.files = set()
        self.output = False

    def update(self, update_type, **fields):
        self.session.bridge.peer.notify('session/update', {
            'sessionId': self.session.ident, 'update': {'sessionUpdate': update_type, **fields}})

    def replay(self, snapshot):
        for message in snapshot.get('messages', []):
            role = message.get('info', {}).get('role')
            if role not in ('assistant', 'user'):
                continue
            for part in message.get('parts', []):
                if part.get('type') in ('text', 'reasoning'):
                    kind = ('user_message_chunk' if role == 'user' else
                            'agent_thought_chunk' if part['type'] == 'reasoning' else 'agent_message_chunk')
                    self.update(kind, content={'type': 'text', 'text': part.get('text', '')})
                elif part.get('type') == 'file':
                    self.update('user_message_chunk' if role == 'user' else 'agent_message_chunk', content=file_content(part))
                elif role == 'assistant' and part.get('type') == 'tool':
                    self.part_tool(part)

    def event(self, event):
        session, payload = self.session, event.get('payload') or {}
        seq = event.get('seq')
        if event.get('sessionId') != session.native_id or not isinstance(seq, int) or seq <= self.seq:
            return
        self.seq = seq
        if not session.turn or session.turn.done():
            return
        kind = event.get('type')
        if kind == 'turn.started':
            if payload.get('inputId') == session.input_id:
                session.turn_id = event.get('turnId')
            return
        if payload.get('inputId') not in (None, session.input_id):
            return
        if (kind in ('turn.failed', 'turn.completed') and not session.turn_id
                and payload.get('inputId') == session.input_id):
            session.turn_id = event.get('turnId')
        if event.get('turnId') and event.get('turnId') != session.turn_id:
            return
        if kind == 'model.streaming':
            typ, delta = payload.get('kind'), payload.get('delta')
            if typ in ('text_delta', 'reasoning_delta') and isinstance(delta, str) and delta:
                self.update('agent_thought_chunk' if typ == 'reasoning_delta' else 'agent_message_chunk',
                            content={'type': 'text', 'text': delta})
                self.output = self.output or typ == 'text_delta'
            elif typ == 'tool_call' and payload.get('toolCallId'):
                self.tool({**payload, 'kind': 'scheduled'})
        elif kind == 'tool.updated':
            self.tool(payload)
        elif kind in ('part.upserted', 'part.started'):
            part = payload.get('part') or {}
            if part.get('type') == 'file' and part.get('partId') not in self.files:
                self.files.add(part.get('partId'))
                self.update('agent_message_chunk', content=file_content(part))
            elif part.get('type') == 'tool':
                self.part_tool(part)
        elif kind == 'turn.failed':
            session.turn.set_exception(BridgeError(safe_error_text(
                (payload.get('error') or {}).get('message') or 'ZCode execution failed')))
        elif kind == 'turn.completed':
            result = payload.get('resultType')
            if result not in ('success', 'cancelled'):
                session.turn.set_exception(BridgeError('ZCode execution stopped: ' + str(result)))
                return
            if not self.output and payload.get('response'):
                self.update('agent_message_chunk', content={'type': 'text', 'text': payload['response']})
                self.output = True
            response = {'stopReason': 'cancelled' if result == 'cancelled' else 'end_turn'}
            if isinstance(payload.get('usage'), dict):
                response['usage'] = payload['usage']
            session.turn.set_result(response)

    def part_tool(self, part):
        # The persisted part contains only a text projection. A later snapshot
        # must not erase structured output (for example images) from tool.updated.
        if self.tools.get(part.get('callId'), {}).get('status') in ('completed', 'failed'):
            return
        state = part.get('state') or {}
        kind = {'pending': 'scheduled', 'running': 'started', 'completed': 'result', 'error': 'error'}.get(state.get('status'))
        if kind:
            self.tool({'kind': kind, 'toolCallId': part.get('callId'), 'toolName': part.get('tool'),
                       'input': state.get('input'), 'result': {'output': state.get('output', '')},
                       'error': {'message': state.get('error', '')}})

    def tool(self, payload):
        ident, kind = payload.get('toolCallId'), payload.get('kind')
        if not ident or kind not in ('scheduled', 'started', 'progress', 'result', 'error'):
            return
        child = payload.get('childSessionId')
        if child:
            self.session.children.add(child)
        exists = ident in self.tools
        previous = self.tools.get(ident, {})
        # Native emits both tool.updated and part snapshots. Do not regress a terminal tool.
        if previous.get('status') in ('completed', 'failed') and kind not in ('result', 'error'):
            return
        status = {'scheduled': 'pending', 'started': 'in_progress', 'progress': 'in_progress',
                  'result': 'completed', 'error': 'failed'}[kind]
        row = {**previous, 'toolCallId': ident, 'status': status,
               'title': payload.get('toolName') or previous.get('title') or '工具', 'kind': 'other'}
        if payload.get('input') is not None:
            row['rawInput'] = payload['input']
        if child or payload.get('source') == 'subagent':
            row['_meta'] = {key: payload[key] for key in ('childSessionId', 'parentToolCallId', 'agentId', 'agentType', 'source') if key in payload}
        if kind == 'result':
            result = payload.get('result') or {}
            row['rawOutput'] = result
            output = result.get('output', result.get('content'))
            content = [{'type': 'content', 'content': {'type': 'text', 'text': output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)}}] if output is not None else []
            display = result.get('display') or {}
            if display.get('kind') == 'node_repl_images':
                for item in display.get('images', []):
                    content.append({'type': 'content', 'content': file_content({
                        'url': f"data:{item['mimeType']};base64,{item['base64']}"})})
            row['content'] = content
        elif kind == 'error':
            message = safe_error_text((payload.get('error') or {}).get('message') or 'Tool failed')
            row['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': message}}]
        elif kind == 'progress' and payload.get('stdoutTail'):
            row['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': payload['stdoutTail']}}]
        if row == previous:
            return
        self.tools[ident] = row
        self.update('tool_call_update' if exists else 'tool_call', **row)
