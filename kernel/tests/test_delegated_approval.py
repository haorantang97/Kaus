"""Exercise delegated approval over real local protocol fakes, never vendor models."""
import asyncio
import copy

import pytest

from drivers.base import InteractionResponse, MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal
from drivers.hermes.testing.harness import FakeHermesHarness
from drivers.hermes.testing.fake_api_server import approval_script, APPROVAL_REQUEST_ID
from tests.test_agent_approval_modes import fixture_driver


@pytest.mark.parametrize('key', ['cursor', 'copilot', 'grok', 'opencode', 'kilo', 'dsh', 'hermes-acp'])
async def test_acp_auto_approves_once_and_switches_back_without_affecting_other_conversation(key):
    driver, harness, project, binding = fixture_driver(key, [], None)
    driver.agent_spec = driver.agent_spec.model_copy(update={
        'command': tuple('permission' if part == 'text-stream' else part for part in driver.agent_spec.command)})
    first = harness.make_conversation(project, binding).evolve(model_id=None, approval_mode='host:auto')
    second = harness.make_conversation(project, binding).evolve(model_id=None, approval_mode='host:ask')
    one = await driver.start_runtime(first, 'card')
    two = await driver.start_runtime(second, 'card')
    try:
        await driver.send_message(one, MessageInput(text='synthetic tool call'))
        events = await collect_until_run_terminal(driver, one, timeout=5)
        decisions = [e.event.decision for e in events if e.event.type == 'permission.resolved']
        assert decisions == ['allow']
        assert any(e.event.type == 'tool.completed' and not e.event.is_error for e in events)
        assert driver._state(two).approval_mode == 'host:ask'
        await driver.set_conversation_selection(one, first.evolve(approval_mode='host:ask'))
        for runtime in (one, two):
            observed = []
            async def reject(event):
                observed.append(event.event.request.request_id)
                await driver.resolve_interaction(runtime, observed[-1], InteractionResponse(kind='permission', option_id='deny'))
            await driver.send_message(runtime, MessageInput(text='ask again'))
            events = await collect_until_run_terminal(driver, runtime, on_interaction=reject, timeout=5)
            assert len(observed) == 1
            assert [e.event.decision for e in events if e.event.type == 'permission.resolved'] == ['deny']
        assert binding.runtime_config['approval_mode'] == 'ask'
    finally:
        await driver.stop_runtime(one)
        await driver.stop_runtime(two)


async def test_hermes_http_delegates_to_exact_request_and_preserves_other_conversation(tmp_path, monkeypatch):
    rig = FakeHermesHarness(tmp_path)
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    server = rig.servers['default']
    server.set_script(approval_script)
    conv = rig.make_conversation(project, binding).evolve(approval_mode='host:auto')
    other = rig.make_conversation(project, binding).evolve(approval_mode='host:ask')
    runtimes = []
    try:
        for item in (conv, other):
            runtimes.append(await driver.start_runtime(item, 'card'))
        client = driver._state(runtimes[0]).supervisor.client()
        original = client.request
        approvals = []
        async def capture(method, path, **kwargs):
            if method == 'POST' and path.endswith('/approval'):
                approvals.append((path, copy.deepcopy(kwargs['body'])))
            return await original(method, path, **kwargs)
        monkeypatch.setattr(client, 'request', capture)
        await driver.send_message(runtimes[0], MessageInput(text='synthetic'))
        events = await collect_until_run_terminal(driver, runtimes[0], timeout=8)
        assert approvals[0][1] == {'choice': 'once', 'all': False, 'request_id': APPROVAL_REQUEST_ID}
        assert any(e.event.type == 'permission.resolved' for e in events)
        assert driver._state(runtimes[1]).conversation.approval_mode == 'host:ask'
        await driver.set_conversation_selection(runtimes[0], conv.evolve(approval_mode='host:ask'))
        requested = []
        async def reject(event):
            requested.append(event.event.request.request_id)
            await driver.resolve_interaction(runtimes[0], requested[-1], InteractionResponse(kind='permission', option_id='deny'))
        await driver.send_message(runtimes[0], MessageInput(text='ask again'))
        await collect_until_run_terminal(driver, runtimes[0], on_interaction=reject, timeout=8)
        assert requested and approvals[-1][1]['choice'] == 'deny'
    finally:
        for runtime in runtimes:
            await driver.stop_runtime(runtime)
        rig.close()


async def test_hermes_hides_delegation_without_both_protocol_features(tmp_path):
    rig = FakeHermesHarness(tmp_path)
    try:
        driver = rig.make_driver()
        server = rig.servers['default']
        server.capabilities = copy.deepcopy(server.capabilities)
        server.capabilities['features']['run_approval_response'] = False
        binding = rig.make_binding(rig.make_project(), driver)
        await driver.probe()
        assert driver.conversation_controls(binding)['approvalModes'] == []
    finally:
        rig.close()
