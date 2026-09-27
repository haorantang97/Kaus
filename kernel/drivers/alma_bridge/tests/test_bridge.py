import asyncio
import json

import httpx
import pytest

from drivers.alma_bridge.bridge import Bridge, local_url, message_parts
from drivers.stdio_bridge import BridgeError, Peer


class Socket:
    def __init__(self, path, harness):
        self.path, self.harness = path, harness
        self.queue = asyncio.Queue()
        self.closed = False
        self.fail_send = False
        if path == 'threads':
            self.queue.put_nowait(json.dumps({'type': 'generating_snapshot', 'data': {'ids': harness.active}}))

    async def recv(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        return await self.queue.get()

    async def send(self, raw):
        if self.fail_send:
            raise OSError('socket closed')
        value = json.loads(raw)
        self.harness.sent.append(value)
        ident = value['data']['threadId']
        if value['type'] == 'stop_generation':
            await self.queue.put(json.dumps({'type': 'thread_generating', 'data': {'id': ident, 'isGenerating': False}}))
        elif self.harness.complete:
            await self.queue.put(json.dumps({'type': 'message_delta', 'data': {'threadId': ident, 'messageId': 'm', 'deltas': [
                {'type': 'text_append', 'partType': 'reasoning', 'text': 'public summary'},
                {'type': 'text_append', 'partType': 'text', 'text': 'hello'},
                {'type': 'part_add', 'partIndex': 1, 'part': {'type': 'tool-read', 'toolCallId': 't', 'input': {'file': 'a'}}},
                {'type': 'tool_output_set', 'partIndex': 1, 'output': 'read', 'state': 'output-available'},
                {'type': 'part_add', 'partIndex': 2, 'part': {'type': 'file', 'url': 'data:image/png;base64,aGk=', 'mediaType': 'image/png'}},
            ]}}))
            await self.queue.put(json.dumps({'type': 'generation_completed', 'data': {'threadId': ident}}))
            await self.queue.put(json.dumps({'type': 'thread_generating', 'data': {'id': ident, 'isGenerating': False}}))

    async def close(self):
        self.closed = True


class Harness:
    def __init__(self):
        self.threads, self.workspaces = {}, []
        self.calls, self.sent, self.sockets, self.active = [], [], [], []
        self.complete = True

    async def connect(self, url, **kwargs):
        socket = Socket(url.split('/ws/')[1], self)
        self.sockets.append(socket)
        return socket

    def request(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == '/api/providers':
            data = [{'id': 'p', 'name': 'Provider', 'apiKey': 'SECRET_MUST_NOT_ESCAPE', 'models': ['one', 'two'], 'availableModels': [{'id': 'one', 'name': 'First'}]}]
        elif path == '/api/settings':
            data = {'chat': {'defaultModel': 'p:one'}}
        elif path == '/api/workspaces':
            if request.method == 'POST':
                data = {**body, 'id': 'workspace'}
                self.workspaces.append(data)
            else:
                data = self.workspaces
        elif path == '/api/threads':
            ident = 'native-' + str(len(self.threads))
            data = {**body, 'id': ident}
            self.threads[ident] = data
        elif path.startswith('/api/threads/'):
            data = self.threads.get(path.split('/')[-1])
            if data is None:
                return httpx.Response(404, json={'error': 'missing'})
            if request.method == 'PUT':
                data.update(body)
        elif path.startswith('/api/handoff/'):
            data = {'ok': True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    harness, frames = Harness(), []
    monkeypatch.setattr('drivers.alma_bridge.bridge.connect', harness.connect)
    bridge = Bridge('http://127.0.0.1:23001', tmp_path / 'state', Peer(frames.append), turn_timeout=1)
    bridge.client = httpx.AsyncClient(base_url=bridge.url, transport=httpx.MockTransport(harness.request))
    return bridge, harness, frames, tmp_path


async def new(setup):
    bridge, harness, frames, root = setup
    result = await bridge.dispatch('session/new', {'cwd': str(root), 'mcpServers': []})
    return bridge, harness, frames, root, result


def test_only_local_service_urls_and_explicit_supported_attachments():
    for url in ('https://example.com', 'http://127.0.0.1/other', 'http://a:b@localhost', 'http://localhost?token=a'):
        with pytest.raises(BridgeError):
            local_url(url)
    assert local_url('http://localhost:23001/') == 'http://localhost:23001'
    with pytest.raises(BridgeError):
        message_parts([{'type': 'image', 'data': 'not base64'}])
    with pytest.raises(BridgeError):
        message_parts([{'type': 'resource', 'resource': {'blob': 'aA=='}}])


async def test_round_trip_defaults_no_tools_does_not_write_account_settings(setup):
    bridge, harness, frames, _, result = await new(setup)
    try:
        assert result['models']['currentModelId'] == 'p:one'
        assert 'SECRET' not in json.dumps(result)
        answer = await bridge.dispatch('session/prompt', {'sessionId': result['sessionId'], 'prompt': [{'type': 'text', 'text': 'hello'}]})
        assert answer['stopReason'] == 'end_turn'
        assert harness.sent[-1]['data']['noTools'] is True
        assert harness.sent[-1]['data']['enabledMCPServerIds'] == []
        updates = [row['params']['update'] for row in frames]
        assert any(isinstance(row.get('content'), dict) and row['content'].get('type') == 'image' for row in updates)
        assert any(row.get('toolCallId') == 't' and row.get('status') == 'completed' for row in updates)
        assert any(row['sessionUpdate'] == 'agent_thought_chunk' for row in updates)
        assert not any(method != 'GET' and path in ('/api/settings', '/api/providers') for method, path, _ in harness.calls)
    finally:
        await bridge.close()


async def test_explicit_tools_and_model_are_session_local_and_resume_exactly(setup):
    bridge, harness, _, root, first = await new(setup)
    try:
        second = await bridge.dispatch('session/new', {'cwd': str(root), 'mcpServers': []})
        ident = first['sessionId']
        await bridge.dispatch('session/set_mode', {'sessionId': ident, 'modeId': 'tools'})
        await bridge.dispatch('session/set_model', {'sessionId': ident, 'modelId': 'p:two'})
        native = bridge.sessions[ident].native_id
        assert bridge.sessions[second['sessionId']].record['mode'] == 'chat'
        assert bridge.sessions[second['sessionId']].record['model'] == 'p:one'
        await bridge.dispatch('session/close', {'sessionId': ident})
        restored = await bridge.dispatch('session/load', {'sessionId': ident, 'cwd': str(root), 'mcpServers': []})
        assert restored['models']['currentModelId'] == 'p:two'
        assert bridge.sessions[ident].native_id == native
        assert len(harness.threads) == 2
        await bridge.dispatch('session/prompt', {'sessionId': ident, 'prompt': [{'type': 'text', 'text': 'next'}]})
        assert harness.sent[-1]['data']['noTools'] is False
        assert harness.sent[-1]['data']['threadId'] == native
        for mode in ('ask', 'auto'):
            with pytest.raises(BridgeError):
                await bridge.dispatch('session/set_mode', {'sessionId': ident, 'modeId': mode})
        with pytest.raises(BridgeError):
            await bridge.dispatch('session/load', {'sessionId': ident, 'cwd': str(root / 'different')})
    finally:
        await bridge.close()


async def test_cancel_waits_for_native_ack_and_cannot_complete_another_session(setup):
    bridge, harness, _, _, result = await new(setup)
    harness.complete = False
    session = bridge.sessions[result['sessionId']]
    try:
        task = asyncio.create_task(session.prompt([{'type': 'text', 'text': 'wait'}]))
        await asyncio.sleep(0)
        session.event({'type': 'generation_completed', 'data': {'threadId': 'other-thread'}})
        assert not task.done()
        with pytest.raises(BridgeError):
            await session.prompt([{'type': 'text', 'text': 'overlap'}])
        await session.cancel()
        assert await task == {'stopReason': 'cancelled'}
        assert harness.sent[-1] == {'type': 'stop_generation', 'data': {'threadId': session.native_id}}
    finally:
        await bridge.close()


async def test_send_failure_clears_turn_and_rejects_settings_until_resume(setup):
    bridge, _, _, _, result = await new(setup)
    session = bridge.sessions[result['sessionId']]
    session.sockets['threads'].fail_send = True
    try:
        with pytest.raises(BridgeError, match='disconnected'):
            await session.prompt([{'type': 'text', 'text': 'broken'}])
        assert session.turn.done()
        with pytest.raises(BridgeError, match='reconnect'):
            await bridge.dispatch('session/set_mode', {'sessionId': session.ident, 'modeId': 'tools'})
    finally:
        await bridge.close()


async def test_native_idle_before_error_is_not_success(setup):
    bridge, harness, _, _, result = await new(setup)
    harness.complete = False
    session = bridge.sessions[result['sessionId']]
    try:
        prompt = asyncio.create_task(session.prompt([{'type': 'text', 'text': 'empty native response'}]))
        await asyncio.sleep(0)
        session.event({'type': 'thread_generating', 'data': {'id': session.native_id, 'isGenerating': False}})
        assert not prompt.done()
        session.event({'type': 'generation_error', 'data': {'threadId': session.native_id, 'error': 'LLM returned empty response'}})
        with pytest.raises(BridgeError, match='empty response'):
            await prompt
        assert session.broken
    finally:
        await bridge.close()


async def test_active_native_generation_and_missing_records_never_create_replacement(setup):
    bridge, harness, _, root, result = await new(setup)
    ident = result['sessionId']
    native = bridge.sessions[ident].native_id
    await bridge.dispatch('session/close', {'sessionId': ident})
    harness.active = [native]
    try:
        with pytest.raises(BridgeError, match='still running'):
            await bridge.dispatch('session/load', {'sessionId': ident, 'cwd': str(root)})
        assert len(harness.threads) == 1
        with pytest.raises(BridgeError, match='Unknown'):
            await bridge.dispatch('session/load', {'sessionId': '../escape', 'cwd': str(root)})
        with pytest.raises(BridgeError, match='MCP'):
            await bridge.dispatch('session/new', {'cwd': str(root), 'mcpServers': [{'name': 'unsafe'}]})
    finally:
        await bridge.close()


async def test_question_and_approval_replies_are_filtered_and_cancelled_safely(setup):
    bridge, harness, frames, _, result = await new(setup)
    harness.complete = False
    session = bridge.sessions[result['sessionId']]
    prompt = asyncio.create_task(session.prompt([{'type': 'text', 'text': 'questions'}]))
    await asyncio.sleep(0)
    try:
        ws = session.sockets['handoff/tool-approvals']
        await ws.queue.put(json.dumps({'type': 'show', 'request': {'requestId': 'unrelated', 'threadId': 'other'}}))
        await asyncio.sleep(.01)
        assert not frames
        question = asyncio.create_task(session.interact('handoff/user-questions', {'requestId': 'q', 'questions': [
            {'question': 'Choose', 'multiSelect': True, 'allowCustomAnswer': True, 'options': [{'label': 'one'}, {'label': 'two'}]}]}))
        await asyncio.sleep(0)
        frame = frames[-1]
        assert frame['method'] == '_alma/questions'
        bridge.peer.response({'id': frame['id'], 'result': {'outcome': {'outcome': 'answered', 'answers': [{'questionId': '0', 'text': 'custom response'}]}}})
        await question
        assert harness.calls[-1][2]['response']['answers'] == [{'selected': [], 'custom': 'custom response'}]
        approval = asyncio.create_task(session.interact('handoff/tool-approvals', {'requestId': 'p', 'title': 'Write'}))
        await asyncio.sleep(0)
        frame = frames[-1]
        session.cancelled = True
        bridge.peer.response({'id': frame['id'], 'result': {'outcome': {'outcome': 'selected', 'optionId': 'allow_once'}}})
        await approval
        assert harness.calls[-1][2]['response'] == {'action': 'deny'}
        await session.cancel()
        await prompt
    finally:
        await bridge.close()
