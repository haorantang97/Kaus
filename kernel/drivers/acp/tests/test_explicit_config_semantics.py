"""Explicit metadata keeps native option categories from changing their meaning."""
from copy import deepcopy
from dataclasses import replace

import pytest

from drivers.acp.capabilities import available_modes, config_options_from_session_result
from drivers.acp.client import AcpConnection
from drivers.acp.presets import AgentQuirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_driver import _driver_with
from drivers.base import MessageInput, UnsupportedCapabilityError
from drivers.contract_tests.harness import collect_until_run_terminal


OPTIONS = [
    {"id": "budget", "category": "model", "type": "select", "currentValue": "low",
     "options": [{"value": "low"}, {"value": "high"}]},
    {"id": "file-scope", "category": "mode", "type": "select", "currentValue": "workspace",
     "options": [{"value": "read-only"}, {"value": "workspace"}, {"value": "all-files"}]},
    {"id": "choose-model", "category": "model", "type": "select", "currentValue": "model-a",
     "options": [{"value": "model-a"}, {"value": "model-b"}]},
]
MODES = {"currentModeId": "code", "availableModes": [
    {"id": "code", "name": "Code"}, {"id": "plan", "name": "Plan"}]}


def make_driver():
    driver = _driver_with(AgentQuirks(model_switch="config_option", mode_semantics="none",
                                    supports_set_mode=True, supports_session_load=True),
        dress_extra={"configOptions": True, "configOptionDefinitions": OPTIONS,
                     "modes": ["code", "plan"]})
    driver.preset = replace(driver.preset, approval_option_id="file-scope", thought_option_id="budget",
                            approval_mode_ids={"read_only": "read-only", "bypass": "all-files"})
    return driver


def test_explicit_option_ids_override_categories_without_guessing_from_names():
    result = {"configOptions": deepcopy(OPTIONS)}
    config = config_options_from_session_result(result, approval_option_id="file-scope", thought_option_id="budget")
    assert config.model_option_id == "choose-model"
    assert [model.model_id for model in config.models] == ["model-a", "model-b"]
    assert config.thought_option_id == "budget" and config.thought_levels == ("low", "high")
    assert config.approval_option_id == "file-scope" and config.current_approval_id == "workspace"
    assert config.mode_option_id is None and config.mode_ids == ()
    assert available_modes(result, approval_option_id="file-scope", thought_option_id="budget") == ()
    assert available_modes({**result, "modes": MODES}, approval_option_id="file-scope", thought_option_id="budget") == tuple(MODES["availableModes"])


def test_single_observed_model_does_not_turn_reasoning_levels_into_model_choices():
    driver = make_driver()
    snapshot = driver._snapshot_from_session({"sessionId": "observed", "models": {"currentModelId": "custom-model"},
        "configOptions": deepcopy(OPTIONS[:2])})
    assert [model.model_id for model in snapshot.models] == ["custom-model"]
    assert snapshot.models[0].reasoning_levels == ("low", "high")
    assert snapshot.current_model_id == "custom-model" and snapshot.model_option_id is None
    assert snapshot.mode_definitions == () and snapshot.mode_ids == ()


async def test_legacy_work_modes_and_explicit_options_reach_distinct_rpcs_and_restore(monkeypatch):
    driver = make_driver()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conv = harness.make_conversation(project, binding).evolve(
        model_id="model-b", reasoning_mode="high", approval_mode="read_only", execution_mode="plan")
    original = AcpConnection.call
    calls = []

    async def capture(self, method, params, **kwargs):
        result = await original(self, method, params, **kwargs)
        if method in ("session/new", "session/resume", "session/load"):
            # A native session can use legacy modes alongside configOptions.
            result["modes"] = deepcopy(MODES)
        if method in ("session/set_mode", "session/set_config_option"):
            calls.append((method, params.get("optionId"), params.get("value", params.get("modeId"))))
        return result

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conv, "card")
    try:
        assert calls == [
            ("session/set_config_option", "file-scope", "read-only"),
            ("session/set_mode", None, "plan"),
            ("session/set_config_option", "choose-model", "model-b"),
            ("session/set_config_option", "budget", "high"),
        ]
        controls = driver.conversation_controls(binding)
        assert controls["approvalModes"] == ["bypass", "read_only"]
        assert controls["executionModes"] == MODES["availableModes"]
        assert controls["executionDefault"] == "plan" and controls["reasoningDefault"] == "high"
        assert [model.model_id for model in (await driver.get_model_catalog(binding)).models] == ["model-a", "model-b"]
        changed = await driver.set_conversation_selection(runtime, conv.evolve(approval_mode="bypass"))
        changed = await driver.set_conversation_selection(runtime, changed.evolve(execution_mode="code"))
        assert calls[-2:] == [("session/set_config_option", "file-scope", "all-files"), ("session/set_mode", None, "code")]
        before = len(calls)
        for unsupported in ("ask", "auto"):
            with pytest.raises(UnsupportedCapabilityError):
                await driver.set_conversation_selection(runtime, changed.evolve(approval_mode=unsupported))
        assert len(calls) == before
        await driver.send_message(runtime, MessageInput(text="hello"))
        assert (await collect_until_run_terminal(driver, runtime))[-1].event.type == "run.completed"
        saved = changed.evolve(native_session_id=runtime.native_session_id)
    finally:
        await driver.stop_runtime(runtime)
    calls.clear()
    restored = await driver.start_runtime(saved, "card")
    try:
        assert calls[:2] == [("session/set_config_option", "file-scope", "all-files"), ("session/set_mode", None, "code")]
        assert driver._state(restored).conversation == saved
    finally:
        await driver.stop_runtime(restored)


async def test_unmapped_native_workspace_default_is_not_elevated_automatically(monkeypatch):
    driver = make_driver()
    harness = FakeAcpHarness()
    binding = harness.make_binding(harness.make_project(), driver)
    conversation = harness.make_conversation(harness.make_project(), binding)
    original = AcpConnection.call
    permission_changes = []

    async def capture(self, method, params, **kwargs):
        if method == "session/set_config_option" and params.get("optionId") == "file-scope":
            permission_changes.append(params)
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert not permission_changes
        assert driver.conversation_controls(binding)["approvalDefault"] is None
        assert driver.conversation_controls(binding)["executionModes"] == []
    finally:
        await driver.stop_runtime(runtime)


async def test_preferred_load_does_not_override_an_effective_version_guard():
    driver = _driver_with(AgentQuirks(supports_session_resume=False, supports_session_load=False,
                                    prefer_session_load=True))

    class NoRpc:
        async def call(self, *args, **kwargs):
            pytest.fail("An explicitly disabled resume/load must not be attempted")

    with pytest.raises(UnsupportedCapabilityError):
        await driver._resume_session(NoRpc(), "old-session", "/tmp")
