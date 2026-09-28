"""Cross-engine permission contracts, without invoking any vendor runtime."""
from copy import deepcopy

import pytest

from drivers.acp.capabilities import available_modes, config_options_from_session_result
from drivers.acp.client import AcpConnection, AcpTransportError
from drivers.acp.presets import get_preset
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_driver import _driver_with
from drivers.base import MessageInput, ModelRejectedError, UnsupportedCapabilityError


# Source/bridge contracts, independent of the preset's generic policy aliases.
CASES = [
    ("gemini", ["default", "autoEdit", "yolo", "plan"], None, ["ask", "auto", "bypass", "plan"]),
    ("goose", ["auto", "approve", "smart_approve", "chat"], "mode", ["bypass", "ask", "native:smart_approve", "native:chat"]),
    ("hermes-acp", ["default", "accept_edits", "dont_ask"], None, ["ask", "auto", "native:dont_ask"]),
    ("claude-code", ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"], None,
     ["ask", "auto", "plan", "native:dontAsk", "bypass", "native:auto"]),
    ("qwen", ["default", "auto-edit", "yolo", "plan"], None, ["ask", "native:auto-edit", "bypass", "plan"]),
    ("deepseek-acp", ["read-only", "workspace-write", "danger-full-access"], "sandbox",
     ["read_only", "native:workspace-write", "bypass"]),
    ("crush", ["ask", "bypass"], "_approval", ["ask", "bypass"]),
    ("commandcode", ["plan", "yolo"], None, ["plan", "bypass"]),
    ("alma", ["chat", "tools"], None, ["read_only", "bypass"]),
    ("zcode", ["plan", "build", "edit", "yolo"], "mode", ["plan", "ask", "auto", "bypass"]),
]


def fixture_driver(key, native_modes, option_id):
    preset = get_preset(key)
    dress = {"modes": native_modes}
    if option_id:
        dress.update(configOptions=True, configOptionDefinitions=[{
            "id": option_id, "category": "mode", "type": "select", "currentValue": native_modes[0],
            "options": [{"value": value, "name": "Native " + value} for value in native_modes],
        }])
    driver = _driver_with(preset.quirks, dress_extra=dress)
    driver.preset = preset
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    return driver, harness, project, binding


@pytest.mark.parametrize("key,native_modes,option_id,policies", CASES)
async def test_every_advertised_permission_reaches_exact_session_rpc_and_survives_resume(
        monkeypatch, key, native_modes, option_id, policies):
    driver, harness, project, binding = fixture_driver(key, native_modes, option_id)
    conv = harness.make_conversation(project, binding).evolve(model_id=None, approval_mode=policies[0])
    calls = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        if method in ("session/set_mode", "session/set_config_option"):
            calls.append((params["sessionId"], method, params.get("optionId"), params.get("value", params.get("modeId"))))
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conv, "card")
    try:
        controls = driver.conversation_controls(binding)
        assert controls["approvalModes"] == policies
        assert [row["nativeId"] for row in controls["approvalOptions"]] == native_modes
        for native_id, policy in zip(native_modes, policies):
            selected = conv.evolve(approval_mode=policy)
            await driver.set_conversation_selection(runtime, selected)
            assert calls[-1] == (runtime.native_session_id,
                                "session/set_config_option" if option_id else "session/set_mode", option_id, native_id)
            assert driver._state(runtime).approval_mode == policy
            conv = selected
        count = len(calls)
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_selection(runtime, conv.evolve(approval_mode="native:invented"))
        assert len(calls) == count
        saved = conv.evolve(native_session_id=runtime.native_session_id)
    finally:
        await driver.stop_runtime(runtime)
    # Respect native presets that don't expose a restore method.
    if driver.quirks.supports_session_resume or driver.quirks.supports_session_load:
        restored = await driver.start_runtime(saved, "card")
        try:
            assert calls[-1][-1] == native_modes[-1]
            assert driver._state(restored).approval_mode == policies[-1]
        finally:
            await driver.stop_runtime(restored)


def test_config_precedence_preserves_kinds_groups_and_empty_catalog_revocation():
    legacy = {"currentModeId": "old", "availableModes": [{"id": "old"}]}
    options = [{"id": "mode", "category": "mode", "currentValue": "review", "options": [{
        "name": "Permission group", "options": [{"value": "review", "name": "Review changes", "_meta": {"kind": "standard"}}],
    }]}]
    result = {"modes": legacy, "configOptions": options}
    assert available_modes(result) == ({"id": "review", "name": "Review changes", "_meta": {"kind": "standard"}},)
    driver, _h, _p, binding = fixture_driver("codex", ["old"], None)
    driver._remember_models(binding.id, result)
    assert driver.conversation_controls(binding)["approvalDefault"] == "ask"
    for invalid in (None, [], "wrong"):
        empty = deepcopy(result)
        empty["configOptions"][0]["options"] = invalid
        assert available_modes(empty) == ()
        assert config_options_from_session_result(empty).mode_definitions == ()


@pytest.mark.parametrize("failure", ["wrong_ack", "dropped_ack"])
async def test_ambiguous_permission_change_blocks_prompt_until_confirmed(monkeypatch, failure):
    driver, harness, project, binding = fixture_driver("goose", ["approve", "smart_approve", "auto"], "mode")
    conv = harness.make_conversation(project, binding).evolve(model_id=None, approval_mode="ask")
    runtime = await driver.start_runtime(conv, "card")
    original = AcpConnection.call

    async def bad_reply(self, method, params, **kwargs):
        reply = await original(self, method, params, **kwargs)
        if method == "session/set_config_option":
            if failure == "dropped_ack":
                raise AcpTransportError("reply lost after applying")
            reply["configOptions"][0]["currentValue"] = "approve"
        return reply

    try:
        monkeypatch.setattr(AcpConnection, "call", bad_reply)
        with pytest.raises(ModelRejectedError, match="尚未确认"):
            await driver.set_conversation_selection(runtime, conv.evolve(approval_mode="native:smart_approve"))
        assert driver._state(runtime).conversation.approval_mode == "ask"
        with pytest.raises(ModelRejectedError, match="尚未确认"):
            await driver.send_message(runtime, MessageInput(text="must not execute"))
        monkeypatch.setattr(AcpConnection, "call", original)
        await driver.set_conversation_selection(runtime, conv)
        assert not driver._state(runtime).permission_pending
    finally:
        await driver.stop_runtime(runtime)


@pytest.mark.parametrize("key,modes", [
    ("pi", ["off", "high"]), ("openclaw", ["off", "high"]),
    ("cursor", ["agent", "ask", "plan"]), ("omp", ["default", "plan"]),
    ("opencode", ["build", "plan"]), ("kilo", ["code", "ask", "architect"]),
    ("dsh", ["default", "plan"]), ("grok", ["default", "ask", "plan"]),
])
def test_other_mode_axes_never_become_permission_choices(key, modes):
    driver, _h, _p, binding = fixture_driver(key, modes, None)
    driver._remember_models(binding.id, {"modes": {"currentModeId": modes[0], "availableModes": [{"id": v} for v in modes]}})
    expected = ["host:ask", "host:auto"] if driver.preset.host_approval else []
    assert driver.conversation_controls(binding)["approvalModes"] == expected
    assert not set(modes) & set(expected)
