"""Native startup errors must retain trailing stderr without exposing secrets."""
from __future__ import annotations

import sys
import textwrap

import pytest

from drivers.acp.client import AcpAgentSpec, AcpConnection, AcpRpcError
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import CreateSessionOptions
from drivers.error_text import safe_exception_message


@pytest.fixture
def failing_agent(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent('''\
        import json, os, sys, time
        for line in sys.stdin:
            request = json.loads(line)
            if request['method'] == 'initialize':
                response = {'result': {'protocolVersion': 1, 'agentCapabilities': {},
                            'agentInfo': {'name': 'diagnostic-fixture', 'version': '1'}}}
            else:
                response = {'error': {'code': -32603, 'message': 'Internal error',
                                     'data': {'message': 'native startup failed; detail=' +
                                              os.environ['KAUS_FIXTURE_CREDENTIAL']}}}
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], **response}), flush=True)
        # The native cause may be emitted only during EOF cleanup, after the
        # RPC exception has already reached the host. No artificial host sleep.
        time.sleep(0.05)
        print('native fixture: bind failed; detail=' + os.environ['KAUS_FIXTURE_CREDENTIAL'],
              file=sys.stderr, flush=True)
    '''))
    return AcpAgentSpec(command=(sys.executable, str(script)), cwd=str(tmp_path),
                        env={"KAUS_FIXTURE_CREDENTIAL": "fixture-private-secret-01234"})


async def test_close_drains_native_stderr_after_rpc_error(failing_agent):
    connection = await AcpConnection.spawn(failing_agent)
    try:
        with pytest.raises(AcpRpcError):
            await connection.call("session/new", {}, timeout=5)
    finally:
        await connection.aclose()
    assert "native fixture: bind failed" in connection.stderr_text()
    assert connection.close_confirmed
    assert not connection.alive


async def test_startup_logs_native_tail_only_in_redacted_local_diagnostic(failing_agent, caplog):
    driver = AcpDriver(failing_agent, default_cwd=failing_agent.cwd, call_timeout=5)
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    try:
        with pytest.raises(AcpRpcError) as caught:
            await driver.start_runtime(conversation, "card", session_options=CreateSessionOptions(
                title="fixture", metadata={"_kausComputerUse": {}}))
        assert str(caught.value) == "-32603: Internal error"
        assert "native startup failed" in caplog.text
        assert "native fixture: bind failed" in caplog.text
        assert "fixture-private-secret-01234" not in caplog.text
        assert "<env:KAUS_FIXTURE_CREDENTIAL>" in caplog.text
        assert not driver._runtimes
    finally:
        await driver.aclose()


def test_subprocess_secret_is_removed_before_diagnostic_truncation():
    secret = 'private-boundary-value-0123456789'
    error = AcpRpcError(-32603, 'x' * 1980 + secret)
    result = safe_exception_message(error, environ={'ENGINE_CREDENTIAL': secret})
    assert 'private-boundary' not in result
