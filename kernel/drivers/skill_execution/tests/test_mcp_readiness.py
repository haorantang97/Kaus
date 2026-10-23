"""Session-scoped MCP readiness, independent of native catalog visibility."""
from copy import deepcopy

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import grok, qwen


REQUEST = {'name': 'requested', 'command': '/synthetic/bin', 'args': ['current'], 'env': []}


class Connection:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def call(self, method, params, *, timeout):
        self.calls.append((method, params, timeout))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def qstatus(*, completed=True, origin='runtime', state='connected', command='/synthetic/bin'):
    return {'sessionId': 'sid', 'mcp': {'initialized': True,
        'discoveryState': 'completed' if completed else 'in_progress', 'servers': [
            {'name': 'requested', 'status': 'ok' if state == 'connected' else 'error',
             'mcpStatus': state, 'configOrigin': origin, 'disabled': False,
             'transport': 'stdio', 'config': {'command': command, 'args': ['current']}}]}}


def gstatus(*, completed=True, state='ready', tools=None):
    return {'result': {'sessionMcpResolved': completed, 'servers': [
        {'name': 'requested', 'type': 'stdio', 'command': '', 'session':
         {'enabled': True, 'status': state, 'tools': tools or []}}]}}


@pytest.mark.parametrize('adapter', [qwen, grok])
async def test_empty_preserves_native_mcp_without_status_query(adapter):
    conn = Connection([])
    await adapter.after_runtime_session(conn, None, None, {'mcpServers': []}, 'sid')
    assert not conn.calls


async def test_qwen_waits_then_accepts_connected_runtime_zero_tool_server(monkeypatch):
    monkeypatch.setattr(qwen, 'MCP_POLL_INTERVAL', 0)
    conn = Connection([qstatus(completed=False, state='connecting'), qstatus()])
    await qwen.after_runtime_session(conn, None, None, {'mcpServers': [REQUEST]}, 'sid')
    assert len(conn.calls) == 2
    assert all(method == 'qwen/status/session/resources' and params == {'sessionId': 'sid'}
               for method, params, _ in conn.calls)


@pytest.mark.parametrize('change, message', [
    ({'configOrigin': 'user_settings'}, '来源'),
    ({'mcpStatus': 'disconnected', 'status': 'error'}, '启动失败'),
    ({'approvalState': 'pending'}, '授权'),
    ({'requiresAuth': True}, '授权'),
    ({'config': {'command': '/old/global', 'args': ['current']}}, '来源'),
    ({'config': {'command': '/synthetic/bin', 'args': ['old']}}, '来源'),
])
async def test_qwen_failure_not_mistaken_for_global_ready(change, message):
    data = qstatus()
    data['mcp']['servers'][0].update(change)
    with pytest.raises(UnsupportedCapabilityError, match=message):
        await qwen.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


@pytest.mark.parametrize('adapter', [qwen, grok])
async def test_malformed_duplicate_and_missing_or_wrong_session_are_rejected(adapter):
    data = qstatus() if adapter is qwen else gstatus()
    rows = data['mcp']['servers'] if adapter is qwen else data['result']['servers']
    rows.append(deepcopy(rows[0]))
    with pytest.raises(UnsupportedCapabilityError, match='来源不唯一'):
        await adapter.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


async def test_qwen_other_session_status_rejected():
    data = qstatus(); data['sessionId'] = 'other'
    with pytest.raises(UnsupportedCapabilityError, match='当前会话'):
        await qwen.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


@pytest.mark.parametrize('adapter', [qwen, grok])
async def test_status_timeout_and_rpc_failure_are_bounded_and_sanitized(adapter, monkeypatch):
    monkeypatch.setattr(adapter, 'MCP_READY_TIMEOUT', 0)
    with pytest.raises(UnsupportedCapabilityError, match='超时'):
        await adapter.after_runtime_session(Connection([]), None, None, {'mcpServers': [REQUEST]}, 'sid')
    monkeypatch.setattr(adapter, 'MCP_READY_TIMEOUT', 1)
    with pytest.raises(UnsupportedCapabilityError, match='无法核验') as error:
        await adapter.after_runtime_session(Connection([RuntimeError('secret-token')]), None, None, {'mcpServers': [REQUEST]}, 'sid')
    assert 'secret-token' not in str(error.value)


async def test_grok_waits_for_resolution_and_accepts_zero_tool_server(monkeypatch):
    monkeypatch.setattr(grok, 'MCP_POLL_INTERVAL', 0)
    conn = Connection([gstatus(completed=False, state='initializing'), gstatus()])
    await grok.after_runtime_session(conn, None, None, {'mcpServers': [REQUEST]}, 'sid')
    assert len(conn.calls) == 2
    assert all(method == '_x.ai/mcp/list' and params == {'sessionId': 'sid', 'cache': True}
               for method, params, _ in conn.calls)


@pytest.mark.parametrize('state, message', [
    ({'authRequired': True}, '授权'), ({'setupRequired': True}, '配置'),
    ({'blockedReason': 'managed-policy'}, '策略'), ({'enabled': False}, '启动失败'),
    ({'status': 'unavailable'}, '启动失败'), ({'status': None}, '启动失败'),
])
async def test_grok_failure_status(state, message):
    data = gstatus(); data['result']['servers'][0]['session'].update(state)
    with pytest.raises(UnsupportedCapabilityError, match=message):
        await grok.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


async def test_grok_resolved_missing_server_fails_even_if_other_server_ready():
    data = gstatus(); data['result']['servers'][0]['name'] = 'unrelated'
    with pytest.raises(UnsupportedCapabilityError, match='启动失败'):
        await grok.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


@pytest.mark.parametrize('adapter', [qwen, grok])
async def test_duplicate_requested_names_are_not_silently_deduplicated(adapter):
    with pytest.raises(UnsupportedCapabilityError, match='名称重复'):
        await adapter.after_runtime_session(Connection([]), None, None, {'mcpServers': [REQUEST, REQUEST]}, 'sid')


@pytest.mark.parametrize('change', [{'type': 'managedGateway'}, {'source': 'managed'}])
async def test_grok_gateway_catalog_ready_is_not_session_client_proof(change):
    data = gstatus(); data['result']['servers'][0].update(change)
    with pytest.raises(UnsupportedCapabilityError, match='来源无法核验'):
        await grok.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


async def test_grok_native_baseline_same_name_does_not_override_live_session_status():
    data = gstatus(); data['result']['servers'][0].update({'source': 'local', 'command': '/global/older'})
    await grok.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')
    data['result']['servers'][0]['session'].pop('status')
    with pytest.raises(UnsupportedCapabilityError, match='启动失败'):
        await grok.after_runtime_session(Connection([data]), None, None, {'mcpServers': [REQUEST]}, 'sid')


@pytest.mark.parametrize('adapter', [qwen, grok])
async def test_failure_identifies_only_sanitized_bounded_name(adapter):
    name = 'server\n\x1b' + 'a' * 80
    request = {**REQUEST, 'name': name}
    data = qstatus(state='disconnected') if adapter is qwen else gstatus(state='unavailable')
    row = data['mcp']['servers'][0] if adapter is qwen else data['result']['servers'][0]
    row['name'] = name
    with pytest.raises(UnsupportedCapabilityError) as caught:
        await adapter.after_runtime_session(Connection([data]), None, None, {'mcpServers': [request]}, 'sid')
    message = str(caught.value)
    assert message.endswith('server' + 'a' * 42)
    assert '\n' not in message and '\x1b' not in message and '/synthetic/bin' not in message
