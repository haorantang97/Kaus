import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.alma_bridge import runtime
from drivers.alma_bridge.terminal import TerminalProxy
from drivers.base import UnsupportedCapabilityError


@pytest.fixture
def terminal(tmp_path):
    record = tmp_path / 'record.json'
    value = {'threadId': 'owned', 'cwd': str(tmp_path), 'url': 'http://localhost:23001',
             'mode': 'chat', 'model': 'p:chosen'}
    record.write_text(json.dumps(value))
    config = {**value, 'nativeId': 'owned', 'recordPath': str(record)}
    calls = []
    native = {'id': 'owned', 'workspaceId': 'w', 'model': 'p:chosen'}
    def handle(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if path == '/api/threads/owned/messages':
            data = {'messages': [{'id': 'one', 'createdAt': '2026-10-17T00:00:00Z',
                'message': json.dumps({'role': 'assistant', 'parts': [
                    {'type': 'text', 'text': 'public'}, {'type': 'reasoning', 'text': 'private'},
                    {'type': 'text', 'text': 'hidden', 'hidden': True}]})}], 'hasMore': False}
        elif path == '/api/threads/owned':
            if request.method == 'PUT': native.update(body)
            data = native
        elif path == '/api/workspaces/w': data = {'path': str(tmp_path)}
        elif path == '/api/models': data = {'data': [{'id': 'p:chosen'}, {'id': 'p:other'}]}
        else: raise AssertionError(path)
        return httpx.Response(200, json=data)
    client = httpx.AsyncClient(base_url=value['url'], transport=httpx.MockTransport(handle))
    return TerminalProxy(config, client=client), calls, record


async def test_native_tui_proxy_is_owned_thread_only_and_no_global_writes(terminal):
    proxy, calls, _ = terminal
    await proxy.verify()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://test/' + proxy.token + '/') as client:
        assert (await client.get('api/settings')).json()['chat']['defaultModel'] == 'p:chosen'
        rows = (await client.get('api/threads/owned/messages')).json()
        assert rows == [{'id': 'one', 'createdAt': '2026-10-17T00:00:00Z',
                         'message': {'role': 'assistant', 'parts': [{'type': 'text', 'text': 'public'}]}}]
        assert (await client.get('api/threads/other/messages')).status_code == 404
        assert (await client.post('api/threads', json={'title': 'new'})).status_code == 403
        assert (await client.put('api/settings', json={'chat': {'defaultModel': 'p:other'}})).status_code == 403
        assert (await client.get('/wrong/api/threads')).status_code == 404
    assert all(method == 'GET' for method, *_ in calls)
    await proxy.close()


async def test_tui_generation_preserves_chat_and_selected_model_and_record(terminal):
    proxy, calls, record = terminal
    await proxy.verify()
    message = {'type': 'generate_response', 'data': {'threadId': 'owned', 'model': 'p:other',
        'userMessage': {'role': 'user', 'parts': [{'type': 'text', 'text': 'hello'}]},
        'noTools': False, 'enabledMCPServerIds': ['unrelated']}}
    result = await proxy.generation(message)
    assert result['data']['noTools'] is True
    assert result['data']['enabledMCPServerIds'] == []
    assert result['data']['threadId'] == 'owned'
    assert result['data']['model'] == 'p:other'
    assert json.loads(record.read_text())['model'] == 'p:other'
    assert [x for x in calls if x[0] != 'GET'] == [('PUT', '/api/threads/owned', {'model': 'p:other'})]
    with pytest.raises(ValueError, match='等待'):
        await proxy.generation(message)
    assert await proxy.generation({'type': 'stop_generation', 'data': {'threadId': 'owned'}}) == {
        'type': 'stop_generation', 'data': {'threadId': 'owned'}}
    with pytest.raises(ValueError, match='其他对话'):
        await proxy.generation({'type': 'generate_response', 'data': {'threadId': 'other'}})
    await proxy.close()


async def test_changed_mode_or_workspace_cannot_silently_downgrade(terminal):
    proxy, _, record = terminal
    value = json.loads(record.read_text()); value['mode'] = 'tools'; record.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='配置已变化'):
        await proxy.verify()
    with pytest.raises(ValueError, match='工具审批'):
        TerminalProxy({**proxy.config, 'mode': 'tools'})
    await proxy.close()


async def test_history_pagination_deduplicates_stable_ids_and_rejects_stuck_page():
    offsets = []
    def handle(request):
        offset = int(request.url.params['offset']); offsets.append(offset)
        return httpx.Response(200, json={'messages': [{'id': 'same'}, {'id': str(offset)}],
            'hasMore': offset == 0, 'nextOffset': offset + 2})
    async with httpx.AsyncClient(base_url='http://localhost', transport=httpx.MockTransport(handle)) as client:
        assert await runtime.read_messages(client, 'owned') == [{'id': 'same'}, {'id': '0'}, {'id': '2'}]
        assert offsets == [0, 2]
    async with httpx.AsyncClient(base_url='http://localhost', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'messages': [], 'hasMore': True, 'nextOffset': 0}))) as client:
        with pytest.raises(ValueError, match='pagination'):
            await runtime.read_messages(client, 'owned')


def test_external_launcher_validates_native_bundle_mode_workspace_and_same_id(tmp_path, monkeypatch):
    ident = str(uuid.uuid4()); state = tmp_path / 'state'; state.mkdir()
    record = {'threadId': 'native-owned', 'cwd': str(tmp_path), 'url': 'http://127.0.0.1:23001',
              'mode': 'chat', 'model': 'p:chosen'}
    (state / (ident + '.json')).write_text(json.dumps(record))
    tui = tmp_path / 'tui.mjs'; tui.write_text('test-native-bundle')
    monkeypatch.setattr(runtime, 'TUI_HASH', hashlib.sha256(tui.read_bytes()).hexdigest())
    monkeypatch.setattr(runtime.shutil, 'which', lambda *args, **kwargs: '/fixture/node')
    spec = AcpAgentSpec(command=('python', 'bridge', '--state-dir', str(state)), cwd=str(tmp_path),
        env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'runtime')})
    binding = SimpleNamespace(runtime_config={'workspace_root': str(tmp_path)})
    conversation = SimpleNamespace(id='conversation', native_session_id=ident, approval_mode='read_only',
                                   model_id=None, config_options={})
    first = runtime.external_cli_command(None, (str(tmp_path / 'alma'),), conversation=conversation, binding=binding, spec=spec)
    second = runtime.external_cli_command(None, (str(tmp_path / 'alma'),), conversation=conversation, binding=binding, spec=spec)
    assert first == second
    value = json.loads(Path(first[-1]).read_text())
    assert value['nativeId'] == 'native-owned' and value['mode'] == 'chat'
    conversation.approval_mode = 'bypass'
    with pytest.raises(UnsupportedCapabilityError, match='工具审批'):
        runtime.external_cli_command(None, (str(tmp_path / 'alma'),), conversation=conversation, binding=binding, spec=spec)
    conversation.approval_mode = 'read_only'
    tui.write_text('updated-native-bundle')
    with pytest.raises(UnsupportedCapabilityError, match='版本'):
        runtime.external_cli_command(None, (str(tmp_path / 'alma'),), conversation=conversation, binding=binding, spec=spec)


async def test_registered_driver_reads_stable_public_history_without_new_native_thread(terminal, tmp_path, monkeypatch):
    from app.projects.models import AgentBinding
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset
    proxy, calls, record = terminal
    ident = str(uuid.uuid4())
    state = tmp_path / 'state'; state.mkdir()
    (state / (ident + '.json')).write_text(record.read_text())
    client_type = httpx.AsyncClient
    transport = proxy.client._transport
    monkeypatch.setattr(runtime.httpx, 'AsyncClient', lambda **kwargs: client_type(transport=transport, **kwargs))
    binding = AgentBinding.create(project='fixture', backend='alma',
        runtime_config={'workspace_root': str(tmp_path)})
    spec = AcpAgentSpec(command=('python', 'bridge', '--state-dir', str(state), '--url', proxy.upstream),
                        cwd=str(tmp_path))
    driver = AcpDriver(spec, backend_key='alma', preset=get_preset('alma'))
    first = await driver.load_native_history(binding, ident)
    second = await driver.load_native_history(binding, ident)
    assert first == second
    assert not first.complete and first.native_session_id == ident
    assert [(item.entry_id, item.text) for item in first.entries] == [('one', 'public')]
    assert not any(method != 'GET' for method, *_ in calls)
    await proxy.close()
