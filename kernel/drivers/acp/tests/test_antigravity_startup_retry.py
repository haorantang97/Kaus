"""Reproduce the native pre-session TLS failure, without network or accounts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpAgentSpec, AcpConnection, AcpRpcError
from drivers.acp.driver import AcpDriver
from drivers.base import CreateSessionOptions
from drivers.skill_execution import antigravity


TRACE = '''File "server.py", line 2717, in new_session
    await self._assert_authenticated()
File "server.py", line 2599, in _assert_authenticated
File "server.py", line 1980, in _ensure_oauth_logged_in
google.auth.exceptions.TransportError: HTTPSConnectionPool(host='oauth2.googleapis.com', port=443):
SSLEOFError: EOF occurred in violation of protocol
'''


@pytest.mark.parametrize('code,attempt,trace,delay', [
    (-32603, 0, TRACE, 0.5), (-32603, 1, TRACE, 1.0),
    (-32603, 2, TRACE, None), (-32000, 0, TRACE, None),
    (-32603, 0, '', None), (-32603, 0, 'Internal error', None),
    (-32603, 0, TRACE.replace('in new_session', 'in prompt'), None),
    (-32603, 0, TRACE + ' invalid_grant', None),
    (-32603, 0, TRACE + ' RefreshError', None),
    (-32603, 0, TRACE + ' CERTIFICATE_VERIFY_FAILED', None),
])
def test_only_observed_pre_allocation_transport_failure_is_retryable(code, attempt, trace, delay):
    assert antigravity.session_start_retry_delay(AcpRpcError(code, 'Internal error'), trace, attempt) == delay


async def connect_fixture(tmp_path, scenario):
    script = tmp_path / 'agent.py'
    calls = tmp_path / 'calls.jsonl'
    script.write_text('import json,sys\nfrom pathlib import Path\n' +
        f'trace={TRACE!r}\nscenario={scenario!r}\ncalls=Path({str(calls)!r})\ncount=0\n' + '''
for line in sys.stdin:
    request = json.loads(line)
    count += 1
    with calls.open('a') as output:
        output.write(json.dumps(request) + '\\n')
    fail = scenario == 'always' or count == 1 or scenario == 'generic-after-first'
    if fail:
        if scenario != 'generic-after-first' or count == 1:
            print(trace, file=sys.stderr, flush=True)
        response = {'error': {'code': -32603, 'message': 'Internal error'}}
    else:
        response = {'result': {'sessionId': 'created-once'}}
    print(json.dumps({'jsonrpc':'2.0', 'id':request['id'], **response}), flush=True)
''')
    spec = AcpAgentSpec(command=(sys.executable, str(script)), cwd=str(tmp_path))
    return await AcpConnection.spawn(spec), AcpDriver(spec, default_cwd=str(tmp_path)), calls


@pytest.mark.parametrize('scenario,expected_calls,success', [
    ('once', 2, True), ('always', 3, False), ('generic-after-first', 2, False),
])
async def test_bounded_retry_uses_only_current_attempt_and_preserves_projection(tmp_path, monkeypatch, scenario, expected_calls, success):
    connection, driver, calls = await connect_fixture(tmp_path, scenario)
    connection.session_start_retry_policy = antigravity.session_start_retry_delay
    options = CreateSessionOptions(title='fixture', workspace_root=str(tmp_path),
        metadata={'mcpServers': [{'name': 'project-only', 'command': 'fixture', 'args': [], 'env': []}],
                  '_meta': {'selectedSkills': ['only-this-project']}})
    try:
        if success:
            result, _ = await driver._new_session(connection, options)
            assert result['sessionId'] == 'created-once'
        else:
            with pytest.raises(AcpRpcError) as caught:
                await driver._new_session(connection, options)
            if scenario == 'always':
                assert caught.value.failure.code == 'connection_interrupted'
                assert caught.value.failure.message == '连接暂时中断'
            else:
                assert caught.value.failure is None
    finally:
        await connection.aclose()
        await driver.aclose()
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(requests) == expected_calls
    assert {request['method'] for request in requests} == {'session/new'}
    assert all(request['params'] == requests[0]['params'] for request in requests)
    assert requests[0]['params']['_meta']['selectedSkills'] == ['only-this-project']
    assert requests[0]['params']['mcpServers'][0]['name'] == 'project-only'


async def test_other_agents_do_not_retry_internal_error(tmp_path):
    connection, driver, calls = await connect_fixture(tmp_path, 'once')
    try:
        with pytest.raises(AcpRpcError):
            await driver._new_session(connection, CreateSessionOptions(title='fixture'))
    finally:
        await connection.aclose()
        await driver.aclose()
    assert len(calls.read_text().splitlines()) == 1


async def test_unverified_connection_does_not_use_adapter_retry_hook(tmp_path, monkeypatch):
    connection, driver, calls = await connect_fixture(tmp_path, 'once')
    monkeypatch.setattr(driver, '_runtime_adapter', lambda: antigravity)
    try:
        assert connection.session_start_retry_policy is None
        with pytest.raises(AcpRpcError):
            await driver._new_session(connection, CreateSessionOptions(title='fixture'))
    finally:
        await connection.aclose()
        await driver.aclose()
    assert len(calls.read_text().splitlines()) == 1


async def test_cancel_during_backoff_never_sends_another_request(tmp_path, monkeypatch):
    connection, driver, calls = await connect_fixture(tmp_path, 'always')
    entered = asyncio.Event()
    def delayed_policy(*args):
        entered.set()
        return 30
    connection.session_start_retry_policy = delayed_policy
    task = asyncio.create_task(driver._new_session(connection, CreateSessionOptions(title='fixture')))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await connection.aclose()
        await driver.aclose()
    assert len(calls.read_text().splitlines()) == 1


@pytest.mark.parametrize('replacement', ['replace', 'inplace'])
async def test_policy_factory_verifies_actual_binary_and_invalidates_replacements(tmp_path, monkeypatch, replacement):
    native = tmp_path / 'native'
    original = b'verified-native-A'
    native.write_bytes(original)
    native.chmod(0o700)
    monkeypatch.setattr(antigravity, 'PAR_HASH', hashlib.sha256(original).hexdigest())
    spec = AcpAgentSpec(command=(str(native),))
    original_hash = antigravity.binary_hash
    hash_threads = []
    def recorded_hash(path):
        hash_threads.append(threading.get_ident())
        return original_hash(path)
    monkeypatch.setattr(antigravity, 'binary_hash', recorded_hash)
    assert await antigravity.session_start_retry_policy(spec) is antigravity.session_start_retry_delay
    assert await antigravity.session_start_retry_policy(spec) is antigravity.session_start_retry_delay
    assert len(hash_threads) == 1
    assert hash_threads[0] != threading.get_ident()

    before = native.stat()
    if replacement == 'replace':
        other = tmp_path / 'updated-native'
        other.write_bytes(original[:-1] + b'B')
        other.chmod(0o700)
        other.replace(native)
    else:
        native.write_bytes(original[:-1] + b'B')
    # Same path, size and mtime must still invalidate the verified entry.
    assert native.stat().st_size == before.st_size
    os.utime(native, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert await antigravity.session_start_retry_policy(spec) is None
    assert len(hash_threads) == 2


async def test_policy_factory_does_not_enable_retry_for_missing_or_unknown_build(tmp_path):
    missing = AcpAgentSpec(command=(str(tmp_path / 'missing'),))
    assert await antigravity.session_start_retry_policy(missing) is None
    unknown = tmp_path / 'unknown'
    unknown.write_bytes(b'unknown native build')
    unknown.chmod(0o700)
    assert await antigravity.session_start_retry_policy(AcpAgentSpec(command=(str(unknown),))) is None


async def test_connect_binds_policy_for_actual_spawn_spec_before_launch(tmp_path, monkeypatch):
    default_spec = AcpAgentSpec(command=('default-agent',), cwd=str(tmp_path))
    actual_spec = AcpAgentSpec(command=('selected-agent',), cwd=str(tmp_path))
    driver = AcpDriver(default_spec)
    seen = []
    async def factory(spec):
        seen.append(('verify', spec))
        return antigravity.session_start_retry_delay
    connection = SimpleNamespace(call=AsyncMock(return_value={}), aclose=AsyncMock())
    async def spawn(spec):
        seen.append(('spawn', spec))
        return connection
    monkeypatch.setattr(driver, '_runtime_adapter', lambda: SimpleNamespace(session_start_retry_policy=factory))
    monkeypatch.setattr(driver, '_absorb_initialize', lambda result: None)
    monkeypatch.setattr(AcpConnection, 'spawn', spawn)
    result, _ = await driver._connect(agent_spec=actual_spec)
    assert result is connection
    assert seen == [('verify', actual_spec), ('spawn', actual_spec)]
    assert connection.session_start_retry_policy is antigravity.session_start_retry_delay
    assert connection.graceful_shutdown_timeout is None


async def test_connect_unknown_build_keeps_normal_initialize_without_retry(tmp_path, monkeypatch):
    native = tmp_path / 'unknown'
    native.write_bytes(b'unknown native build')
    native.chmod(0o700)
    driver = AcpDriver(AcpAgentSpec(command=(str(native),)))
    connection = SimpleNamespace(call=AsyncMock(return_value={}), aclose=AsyncMock())
    monkeypatch.setattr(driver, '_runtime_adapter', lambda: antigravity)
    monkeypatch.setattr(driver, '_absorb_initialize', lambda result: None)
    monkeypatch.setattr(AcpConnection, 'spawn', AsyncMock(return_value=connection))
    result, _ = await driver._connect()
    assert result is connection
    assert connection.session_start_retry_policy is None
    assert connection.call.call_args.args[0] == 'initialize'
    assert connection.graceful_shutdown_timeout == antigravity.GRACEFUL_SHUTDOWN_TIMEOUT


async def test_cancel_verification_does_not_spawn_native_process(tmp_path, monkeypatch):
    driver = AcpDriver(AcpAgentSpec(command=('fixture',), cwd=str(tmp_path)))
    entered = asyncio.Event()
    async def factory(spec):
        entered.set()
        await asyncio.Event().wait()
    spawn = AsyncMock()
    monkeypatch.setattr(driver, '_runtime_adapter', lambda: SimpleNamespace(session_start_retry_policy=factory))
    monkeypatch.setattr(AcpConnection, 'spawn', spawn)
    task = asyncio.create_task(driver._connect())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    spawn.assert_not_called()
