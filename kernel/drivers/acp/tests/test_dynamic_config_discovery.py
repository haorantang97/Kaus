"""Session-discovered options override old presets, including unrecognised agents."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.tests.test_batch16_backend import Harness
from drivers.acp.driver import _CatalogSnapshot
from drivers.acp.presets import AgentQuirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_driver import _driver_with
from drivers.base import ModelRejectedError


OPTIONS = [
    {"id": "vendor-model-v2", "name": "Model", "type": "select", "category": "model",
     "currentValue": "fake:small", "options": [
         {"value": "fake:small", "name": "Small"}, {"value": "fake:large", "name": "Large"}]},
    {"id": "compute-budget", "name": "Effort", "type": "select", "category": "thought_level",
     "currentValue": "low", "options": [
         {"value": "low", "name": "Low"}, {"value": "high", "name": "High"}]},
]


def dynamic_driver():
    # Deliberately stale/unknown preset: the protocol, not the brand, enables it.
    return _driver_with(AgentQuirks(model_switch="none", supports_set_model=False), dress_extra={
        "configOptions": True, "configOptionModel": True,
        "configOptionDefinitions": deepcopy(OPTIONS),
    })


async def test_unknown_agent_options_enable_display_and_real_settings_rpc(tmp_path):
    driver = dynamic_driver()
    h = Harness(tmp_path, driver=driver)
    await h.seed()
    conversation = await h.new_conversation()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://test",
                                    headers={"Authorization": f"Bearer {h.token}"}) as api:
            url = f"/api/bindings/{h.binding.id}/effective-settings?includeCatalog=true&conversationId={conversation.id}"
            settings = await api.get(url)
            assert settings.status_code == 200
            data = settings.json()
            assert data["uiCapabilities"]["models"]["conversationScoped"] == "supported"
            assert data["uiCapabilities"]["models"]["reasoning"] == "supported"
            assert [m["modelId"] for m in data["modelCatalog"]["models"]] == ["fake:small", "fake:large"]
            # Before runtime creation this choice must be applied at first start.
            assert (await api.patch(f"/api/conversations/{conversation.id}", json={"modelId": "fake:large"})).status_code == 200
            conversation = await h.repositories.conversations.get(conversation.id)
            handle = await h.host.ensure_runtime(conversation)
            state = driver._state(handle)
            assert driver._config_options(state.session_result).current_model_id == "fake:large"
            for patch in ({"reasoningMode": "high"}, {"modelId": "fake:small"}, {"reasoningMode": "low"}):
                response = await api.patch(f"/api/conversations/{conversation.id}", json=patch)
                assert response.status_code == 200, response.text
                assert response.json()["appliedToRuntime"] is True
            actual = driver._config_options(state.session_result)
            assert actual.current_model_id == "fake:small"
            assert actual.current_thought_level == "low"
    finally:
        await h.host.aclose()
        h.unit_of_work.close()
        await driver.aclose()


async def test_draft_model_preview_reads_changed_effort_without_a_prompt_or_global_change(monkeypatch, tmp_path):
    from drivers.acp.client import AcpConnection
    driver = dynamic_driver()
    h = Harness(tmp_path, driver=driver)
    await h.seed()
    original = AcpConnection.call
    calls = []

    async def capture(self, method, params=None, **kwargs):
        calls.append(method)
        result = await original(self, method, params, **kwargs)
        if method == "session/set_config_option" and params.get("value") == "fake:large":
            result["configOptions"][1].update(currentValue="max", options=[{"value": "max", "name": "Max"}])
        return result

    monkeypatch.setattr(AcpConnection, "call", capture)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://test",
                                    headers={"Authorization": f"Bearer {h.token}"}) as api:
            url = f"/api/bindings/{h.binding.id}/effective-settings?includeCatalog=true"
            before = (await api.get(url)).json()
            preview = (await api.get(url + "&modelId=fake:large")).json()
            assert preview["conversationControls"]["reasoningLevels"] == ["max"]
            assert preview["conversationControls"]["reasoningDefault"] == "max"
            count = len(calls)
            assert (await api.get(url + "&modelId=fake:large")).json() == preview
            assert len(calls) == count
            assert (await api.get(url)).json()["conversationControls"] == before["conversationControls"]
            assert "session/prompt" not in calls
            assert not h.host.active_conversation_ids()
    finally:
        await h.host.aclose()
        h.unit_of_work.close()
        await driver.aclose()


async def test_authoritative_reply_rejection_is_not_saved_as_success():
    driver = dynamic_driver()
    fixture = FakeAcpHarness()
    project = fixture.make_project()
    binding = fixture.make_binding(project, driver)
    conversation = fixture.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    state = driver._state(runtime)
    try:
        async def refuse(*args, **kwargs):
            return {"configOptions": deepcopy(OPTIONS)}
        driver._call_config_option = refuse
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, conversation.evolve(model_id="fake:large"))
        assert state.conversation.model_id == conversation.model_id
        assert driver._config_options(state.session_result).current_model_id == "fake:small"
    finally:
        await driver.stop_runtime(runtime)


async def test_new_config_protocol_overrides_legacy_effort_suffix_encoding():
    driver = _driver_with(AgentQuirks(model_switch="set_model", supports_set_model=True,
                                    model_id_format="effort_suffix"), dress_extra={
        "configOptions": True, "configOptionModel": True,
        "configOptionDefinitions": deepcopy(OPTIONS),
    })
    fixture = FakeAcpHarness()
    project = fixture.make_project()
    binding = fixture.make_binding(project, driver)
    assert driver.coordinator_selection("fake:small", "high") == "fake:small"
    # Restore a saved legacy selection under a newly upgraded config protocol.
    conversation = fixture.make_conversation(project, binding).evolve(model_id="fake:small[high]")
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert driver._config_options(driver._state(runtime).session_result).current_thought_level == "high"
        applied = await driver.set_conversation_selection(runtime, conversation.evolve(model_id="fake:large"))
        assert applied.model_id == "fake:large"
        assert driver._config_options(driver._state(runtime).session_result).current_model_id == "fake:large"
    finally:
        await driver.stop_runtime(runtime)


def test_config_options_replace_stale_legacy_models_and_options_can_disappear():
    driver = dynamic_driver()
    fixture = FakeAcpHarness()
    binding = fixture.make_binding(fixture.make_project(), driver)
    result = {"configOptions": deepcopy(OPTIONS), "models": {
        "currentModelId": "retired", "availableModels": [{"modelId": "retired", "name": "Retired"}]}}
    snapshot = driver._snapshot_from_session(result)
    assert snapshot.current_model_id == "fake:small"
    assert [m.model_id for m in snapshot.models] == ["fake:small", "fake:large"]
    assert driver._controls_from_snapshot(binding, snapshot)["reasoning"] is True
    next_snapshot = driver._snapshot_from_session({"configOptions": []})
    assert driver._controls_from_snapshot(binding, next_snapshot)["model"] is False
    assert driver._controls_from_snapshot(binding, next_snapshot)["reasoning"] is False


def test_failed_discovery_recovers_quickly_and_custom_executable_changes_invalidate_cache(tmp_path):
    now = datetime.now(timezone.utc)
    assert not _CatalogSnapshot(fetched_at=now - timedelta(seconds=6), degraded=True).is_fresh(now, 600)
    assert _CatalogSnapshot(fetched_at=now - timedelta(seconds=6)).is_fresh(now, 600)
    driver = dynamic_driver()
    executable = tmp_path / "agent"
    executable.write_text("version1")
    executable.chmod(0o700)
    driver.agent_spec = driver.agent_spec.model_copy(update={"command": (str(executable),)})
    fixture = FakeAcpHarness()
    binding = fixture.make_binding(fixture.make_project(), driver)
    before = driver._config_stamp(binding)
    executable.write_text("version2-new-options")
    assert driver._config_stamp(binding) != before


def test_successful_session_without_selector_uses_default_despite_old_preset():
    driver = _driver_with(AgentQuirks(model_switch='config_option'))
    h = FakeAcpHarness()
    project = h.make_project()
    binding = h.make_binding(project, driver)
    snapshot = driver._snapshot_from_session({'sessionId': 'native-default', 'configOptions': []})
    assert snapshot.engine_default_available and not snapshot.degraded
    assert not driver._controls_from_snapshot(binding, snapshot)['model']
