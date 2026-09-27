"""Protocol-level selection checks: grouped catalogs, opaque ids and isolation."""
import copy
from dataclasses import replace

import pytest

from drivers.acp.client import AcpConnection, AcpRpcError
from drivers.acp.presets import AcpPreset, AgentQuirks
from drivers.acp.tests.test_driver import _driver_with
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import MessageInput, ModelRejectedError, UnsupportedCapabilityError
from drivers.contract_tests.harness import collect_until_run_terminal


MODEL = '["route-a","shared-name"]'
OPTIONS = [
    {"id": "choose-model", "type": "select", "category": "model", "currentValue": MODEL,
     "options": [{"group": "available", "name": "Available", "options": [
         {"value": MODEL, "name": "Model A"}, {"value": "route-b/shared-name", "name": "Model B"}]}]},
    {"id": "work-style", "type": "select", "category": "mode", "currentValue": "code",
     "options": [{"value": "code", "name": "Code"}, {"value": "debug", "name": "Debug"}]},
    {"id": "compute-budget", "type": "select", "category": "thought_level", "currentValue": "low",
     "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}]},
]


def driver_for_options():
    return _driver_with(AgentQuirks(model_switch="config_option", mode_semantics="none"),
        dress_extra={"configOptions": True, "configOptionModel": True, "configOptionDefinitions": OPTIONS})


async def test_mode_and_effort_reach_separate_rpc_ids_before_first_prompt(monkeypatch):
    driver = driver_for_options()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(
        model_id=MODEL, reasoning_mode="high", execution_mode="debug")
    calls = []
    original = AcpConnection.call
    async def capture(self, method, params, **kwargs):
        if method == "session/set_config_option":
            calls.append((params.get("optionId"), params["value"]))
        return await original(self, method, params, **kwargs)
    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert calls == [("work-style", "debug"), ("choose-model", MODEL), ("compute-budget", "high")]
        controls = driver.conversation_controls(binding)
        assert controls["reasoning"] and controls["approvalModes"] == []
        assert controls["executionModes"] == [{"id": "code", "name": "Code"}, {"id": "debug", "name": "Debug"}]
        assert controls["executionDefault"] == "debug"
        assert controls["reasoningDefault"] == "high"
        catalog = await driver.get_model_catalog(binding)
        assert [model.model_id for model in catalog.models] == [MODEL, "route-b/shared-name"]
        await driver.send_message(runtime, MessageInput(text="hello"))
        events = await collect_until_run_terminal(driver, runtime)
        assert events[-1].event.type == "run.completed"
    finally:
        await driver.stop_runtime(runtime)


async def test_model_switch_uses_engine_returned_reasoning_default(monkeypatch):
    driver = driver_for_options()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(model_id=MODEL, reasoning_mode="high")
    runtime = await driver.start_runtime(conversation, "card")
    state = driver._state(runtime)
    returned = copy.deepcopy(OPTIONS)
    returned[0]["currentValue"] = "route-b/shared-name"
    returned[2]["currentValue"] = "low"
    returned[2]["options"] = [{"value": "low", "name": "Low"}]

    async def change_model(method, params, **kwargs):
        assert method == "session/set_config_option"
        assert params["value"] == "route-b/shared-name"
        return {"configOptions": returned}

    monkeypatch.setattr(state.connection, "call", change_model)
    try:
        applied = await driver.set_conversation_selection(runtime, conversation.evolve(model_id="route-b/shared-name"))
        assert applied.reasoning_mode == "low"
        assert state.conversation.reasoning_mode == "low"
        assert driver.conversation_controls(binding)["reasoningDefault"] == "low"
    finally:
        await driver.stop_runtime(runtime)


async def test_rejected_mode_keeps_selection_and_unknown_values_are_not_sent(monkeypatch):
    driver = driver_for_options()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(execution_mode="code")
    runtime = await driver.start_runtime(conversation, "card")
    state = driver._state(runtime)
    original = state.connection.call
    attempts = []
    async def reject(method, params, **kwargs):
        if method == "session/set_config_option":
            attempts.append(params)
            raise AcpRpcError(-32000, "mode blocked")
        return await original(method, params, **kwargs)
    monkeypatch.setattr(state.connection, "call", reject)
    try:
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, conversation.evolve(execution_mode="debug"))
        assert len(attempts) == 1  # a rejected value must not try another field spelling
        assert state.conversation.execution_mode == "code"
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_selection(runtime, conversation.evolve(reasoning_mode="unlisted"))
        assert len(attempts) == 1
    finally:
        await driver.stop_runtime(runtime)


async def test_config_update_is_session_bound_and_changes_available_efforts():
    driver = driver_for_options()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conv = harness.make_conversation(project, binding)
    first = await driver.start_runtime(conv, "card")
    second = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    one, two = driver._state(first), driver._state(second)
    changed = copy.deepcopy(OPTIONS)
    changed[0]["id"] = "another-session-model"
    changed[2]["options"] = [{"value": "medium", "name": "Medium"}]
    try:
        driver._on_notification(one, "session/update", {"sessionId": "wrong-session", "update": {
            "sessionUpdate": "config_option_update", "configOptions": changed}})
        assert one.session_result["configOptions"][0]["id"] == "choose-model"
        driver._on_notification(two, "session/update", {"sessionId": two.session_id, "update": {
            "sessionUpdate": "config_option_update", "configOptions": changed}})
        assert driver._catalog[binding.id].models[0].reasoning_levels == ("medium",)
        # Binding catalog now belongs to two. One must still send its own id.
        await driver.set_conversation_model(first, MODEL)
        assert one.session_result["configOptions"][0]["id"] == "choose-model"
    finally:
        await driver.stop_runtime(first)
        await driver.stop_runtime(second)


def test_external_variant_added_or_changed_invalidates_discovery_without_reading_it(tmp_path, monkeypatch):
    from drivers.acp.driver import AcpDriver
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec
    preset = AcpPreset(id="sample", label="Sample", command=("noop",), config_root_env="SAMPLE_HOME",
        config_root_default=str(tmp_path), config_watch_files=("variants/*/settings.yml",))
    driver = AcpDriver(fake_agent_spec().with_env(SAMPLE_HOME=str(tmp_path)), preset=preset)
    harness = FakeAcpHarness()
    binding = harness.make_binding(harness.make_project(), driver)
    before = driver._config_stamp(binding)
    target = tmp_path / "variants/acp/settings.yml"
    target.parent.mkdir(parents=True)
    target.write_text("model: custom-a\n")
    monkeypatch.setattr(type(target), "read_text", lambda *a, **k: pytest.fail("Config contents must not be read"))
    after = driver._config_stamp(binding)
    assert after != before
    target.write_text("model: custom-longer-b\n")
    assert driver._config_stamp(binding) != after


def test_explicit_config_root_wins_over_xdg_and_only_metadata_is_read(tmp_path, monkeypatch):
    from drivers.acp.driver import AcpDriver
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec
    native = tmp_path / "native"
    xdg = tmp_path / "xdg"
    native.mkdir()
    (xdg / "sample").mkdir(parents=True)
    preset = AcpPreset(id="sample", label="Sample", command=("noop",),
        config_root_env="SAMPLE_HOME", config_root_default=str(tmp_path / "fallback"),
        config_xdg_dir="sample", config_watch_files=("config.json",))
    driver = AcpDriver(fake_agent_spec().with_env(SAMPLE_HOME=str(native), XDG_CONFIG_HOME=str(xdg)), preset=preset)
    harness = FakeAcpHarness()
    binding = harness.make_binding(harness.make_project(), driver)
    monkeypatch.setattr(type(native), "read_text", lambda *a, **k: pytest.fail("Config contents must not be read"))
    before = driver._config_stamp(binding)
    (xdg / "sample/config.json").write_text("unrelated")
    assert driver._config_stamp(binding) == before
    (native / "config.json").write_text("changed")
    assert driver._config_stamp(binding) != before
    driver.agent_spec = fake_agent_spec().with_env(XDG_CONFIG_HOME=str(xdg))
    assert str(xdg / "sample/config.json") in {row[0] for row in driver._config_stamp(binding)}


async def test_permissions_are_independent_from_execution_and_rejected_changes_do_not_mutate(monkeypatch):
    options = copy.deepcopy(OPTIONS) + [{"id":"_approval", "category":"other", "type":"select", "currentValue":"ask", "options":[{"value":"ask"}, {"value":"bypass"}]}]
    driver = _driver_with(AgentQuirks(model_switch="config_option", mode_semantics="none"), dress_extra={"configOptions":True, "configOptionModel":True, "configOptionDefinitions":options})
    driver.preset = replace(driver.preset, approval_option_id="_approval", approval_mode_ids={"ask":"ask", "bypass":"bypass"})
    h = FakeAcpHarness()
    p = h.make_project()
    b = h.make_binding(p, driver)
    conv = h.make_conversation(p, b).evolve(execution_mode="debug", approval_mode="ask")
    runtime = await driver.start_runtime(conv, "card")
    state = driver._state(runtime)
    try:
        controls = driver.conversation_controls(b)
        assert controls['approvalModes'] == ['ask', 'bypass']
        assert controls['executionDefault'] == 'debug'
        applied = await driver.set_conversation_selection(runtime, conv.evolve(approval_mode='bypass'))
        values = {v['id']:v['currentValue'] for v in state.session_result['configOptions']}
        assert values['_approval'] == 'bypass' and values['work-style'] == 'debug'
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_selection(runtime, applied.evolve(approval_mode='auto'))
        assert state.conversation.approval_mode == 'bypass'
        await driver.set_conversation_selection(runtime, applied.evolve(execution_mode='code'))
        values = {v['id']:v['currentValue'] for v in state.session_result['configOptions']}
        assert values['_approval'] == 'bypass' and values['work-style'] == 'code'
    finally:
        await driver.stop_runtime(runtime)
