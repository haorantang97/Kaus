from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpRpcError
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import CreateSessionOptions, MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal


@pytest.fixture(autouse=True)
def isolated_optional_computer_providers(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))


@pytest.mark.parametrize('managed', [False, True])
async def test_native_failure_receipt_overrides_end_turn_without_marking_login_success(tmp_path, monkeypatch, managed):
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    if managed:
        driver._skill_plans[conversation.id] = SimpleNamespace(spec=driver.agent_spec, metadata={}, root=None)
    seen = []
    async def prepare(spec, conv, metadata):
        return spec.with_env(RUNTIME_RECEIPT='fixture')
    async def before(connection, spec, conv, metadata, session_id, params):
        assert spec.env['RUNTIME_RECEIPT'] == 'fixture'
        assert metadata['scope'] == 'fixture'
        assert metadata['_kausWorkspaceRoot'] == str(tmp_path)
        assert metadata.get('_kausComputerUse', {}).get('resources', []) == []
        assert conv.id == conversation.id and params['sessionId'] == session_id
        seen.append('before')
        return {'sequence': len(seen)}
    async def after(connection, spec, conv, metadata, session_id, params, result, token):
        seen.append('after')
        assert result['stopReason'] == 'end_turn' and token['sequence'] > 0
        if len(seen) == 2:
            raise AcpRpcError(-32000, 'Synthetic service connection failed', {'code': 'mcp.server_failed'})
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(
        prepare_runtime_spec=prepare, before_runtime_prompt=before, after_runtime_prompt=after))
    runtime = await driver.start_runtime(conversation, 'card', session_options=CreateSessionOptions(metadata={'scope': 'fixture'}))
    initial_auth = driver._catalog[binding.id].auth_state
    try:
        await driver.send_message(runtime, MessageInput(text='first synthetic request'))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == 'run.failed'
        assert not any(item.event.type == 'run.completed' for item in events)
        assert driver._catalog[binding.id].auth_state == initial_auth
        await driver.send_message(runtime, MessageInput(text='second synthetic request'))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == 'run.completed'
        assert driver._catalog[binding.id].auth_state == 'signed_in'
        assert seen == ['before', 'after', 'before', 'after']
    finally:
        await driver.stop_runtime(runtime)


@pytest.mark.parametrize('auth_state,expected', [('unknown', 'unknown'), ('signed_out', 'signed_out'),
                                               ('signed_in', 'signed_in'), ('invalid', 'signed_in')])
async def test_native_control_completion_can_report_auth_without_claiming_model_success(tmp_path, monkeypatch, auth_state, expected):
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    async def after(*args):
        return {'auth_state': auth_state}
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(after_runtime_prompt=after))
    runtime = await driver.start_runtime(conversation, 'card')
    try:
        await driver.send_message(runtime, MessageInput(text='synthetic local control'))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == 'run.completed'
        assert driver._catalog[binding.id].auth_state == expected
    finally:
        await driver.stop_runtime(runtime)


@pytest.mark.parametrize('stage', ['before', 'after'])
async def test_unexpected_runtime_hook_failure_finishes_turn_without_exposing_diagnostics(tmp_path, monkeypatch, stage):
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    async def failed(*args):
        raise ValueError('private-fixture-secret /private/fixture/config.json')
    adapter = SimpleNamespace(**{stage + '_runtime_prompt': failed})
    monkeypatch.setattr(driver, '_runtime_adapter', lambda: adapter)
    runtime = await driver.start_runtime(conversation, 'card')
    initial_auth = driver._catalog[binding.id].auth_state
    try:
        await driver.send_message(runtime, MessageInput(text='synthetic hook failure'))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == 'run.failed'
        assert 'private-fixture-secret' not in str(events)
        assert '/private/fixture' not in str(events)
        assert driver._catalog[binding.id].auth_state == initial_auth
        monkeypatch.setattr(driver, '_runtime_adapter', lambda: None)
        await driver.send_message(runtime, MessageInput(text='retry'))
        assert (await collect_until_run_terminal(driver, runtime))[-1].event.type == 'run.completed'
    finally:
        await driver.stop_runtime(runtime)
