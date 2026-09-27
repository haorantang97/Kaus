"""Conversation and Group selections persist independently of permissions."""
import asyncio

from fastapi.testclient import TestClient

from app.tests.test_batch16_backend import Harness


def test_mode_round_trip_does_not_change_permissions_other_conversations_or_binding(tmp_path, monkeypatch):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    monkeypatch.setattr(built.driver, "conversation_controls", lambda _: {
        "reasoning": False, "approvalModes": [],
        "executionModes": [{"id": "code", "name": "Code"}, {"id": "debug", "name": "Debug"}]})
    first = asyncio.run(built.new_conversation())
    other = asyncio.run(built.new_conversation())
    try:
        with TestClient(built.app, headers={"Authorization": f"Bearer {built.token}"}) as client:
            url = f"/api/conversations/{first.id}"
            response = client.patch(url, json={"executionMode": "debug"})
            assert response.status_code == 200, response.text
            assert response.json()["conversation"]["executionMode"] == "debug"
            assert client.get(url).json()["conversation"]["executionMode"] == "debug"
            assert client.get(f"/api/conversations/{other.id}").json()["conversation"]["executionMode"] is None
            assert client.patch(url, json={"executionMode": "unknown"}).status_code == 501
            assert client.patch(url, json={"executionMode": "code", "approvalMode": "bypass"}).status_code == 400
            assert client.patch(url, json={"approvalMode": "code"}).status_code == 501
        saved = asyncio.run(built.repositories.conversations.get(first.id))
        assert saved.execution_mode == "debug" and saved.approval_mode is None
        assert asyncio.run(built.repositories.bindings.get(built.binding.id)) == built.binding
    finally:
        built.close()


async def test_group_accepts_engine_without_permission_selector_and_preserves_mode_and_effort(tmp_path):
    from app.projects.models import AgentBinding, Backend
    from app.tests.test_batch22_groups import GroupHarness, _api, _new_group
    from drivers.acp.tests.test_config_selection import MODEL, driver_for_options
    from drivers.base import MessageInput
    from drivers.contract_tests.harness import collect_until_run_terminal
    built = GroupHarness(tmp_path, coordinator_enabled=True)
    await built.seed()
    driver = driver_for_options()
    built.registry.register(driver)
    await built.repositories.backends.save(Backend.create(key="acp", display_name="Options", driver_kind=driver.driver_kind))
    binding = await built.repositories.bindings.save(AgentBinding.create(
        project=built.binding.project_id, backend="acp", runtime_config={"approval_mode": "ask"}))
    api = _api(built)
    runtime = None
    try:
        group = await _new_group(api)
        config = {"bindingId": binding.id, "modelId": MODEL, "executionMode": "debug", "reasoningMode": "high"}
        response = await api.put(f"/api/groups/{group['id']}/coordinator", json={"config": config, "expectedRevision": 0})
        assert response.status_code == 200, response.text
        saved = await built.repositories.collaborations.get(group['id'])
        selected = saved.settings['coordinator']
        assert selected['executionMode'] == 'debug' and selected['approvalMode'] is None
        conv = await built.group_router.group_coordinator.execution(saved, {'configSnapshot': selected}, 'planning')
        assert conv.execution_mode == 'debug' and conv.reasoning_mode == 'high'
        driver.register_binding(await built.repositories.bindings.get(conv.agent_binding_id))
        runtime = await driver.start_runtime(conv, 'card')
        await driver.send_message(runtime, MessageInput(text='hello'))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == 'run.completed'
        assert await built.repositories.bindings.get(binding.id) == binding
    finally:
        if runtime:
            await driver.stop_runtime(runtime)
        await api.aclose()
        await built.aclose()
