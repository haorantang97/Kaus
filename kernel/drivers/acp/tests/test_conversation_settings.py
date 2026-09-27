from __future__ import annotations

import pytest

from drivers.acp.client import AcpConnection, AcpRpcError
from drivers.acp.presets import AgentQuirks
from drivers.acp.tests.test_driver import _driver_with
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import ModelRejectedError


async def test_effort_updates_reach_rpc_and_rejected_change_keeps_old_snapshot(monkeypatch):
    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model", model_id_format="effort_suffix"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(reasoning_mode="high")
    calls = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        if method == "session/set_model":
            calls.append(params["modelId"])
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert calls[-1] == "fake:small[high]"
        updated = conversation.snapshot_model(model_id="fake:large", reasoning_mode="low")
        await driver.set_conversation_selection(runtime, updated)
        assert calls[-1] == "fake:large[low]"
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, updated.snapshot_model(model_id="unknown", reasoning_mode="medium"))
        assert driver._state(runtime).conversation == updated
    finally:
        await driver.stop_runtime(runtime)


async def test_explicit_permission_uses_exact_mode_and_client_fs_policy(monkeypatch, tmp_path):
    driver = _driver_with(AgentQuirks(supports_set_mode=True, mode_semantics="approval"), dress_extra={"modes": ["default", "acceptEdits", "readOnly", "plan", "bypassPermissions"]})
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(approval_mode="read_only")
    calls = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        if method == "session/set_mode":
            calls.append(params["modeId"])
            if params["modeId"] == "bypassPermissions":
                raise AcpRpcError(-32602, "policy rejected")
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert calls == ["readOnly"]
        state = driver._state(runtime)
        state.workspace_root = str(tmp_path)
        responses = []
        monkeypatch.setattr(state.connection, "respond", lambda *args, **kwargs: responses.append((args, kwargs)))
        await driver._on_fs_request(state, "write", "fs/write_text_file", {"path": str(tmp_path / "denied.txt"), "content": "no"})
        assert not (tmp_path / "denied.txt").exists()
        assert responses[-1][1]["error"]["code"] == -32001
        updated = conversation.evolve(approval_mode="ask")
        await driver.set_conversation_selection(runtime, updated)
        assert calls[-1] == "default"
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, updated.evolve(approval_mode="bypass"))
        assert state.approval_mode == "ask"
        assert state.conversation.approval_mode == "ask"
    finally:
        await driver.stop_runtime(runtime)


def test_permission_menu_does_not_confuse_workspace_access_with_full_access():
    from drivers.acp.capabilities import resolve_conversation_mode_id
    modes = ["read-only", "agent", "agent-full-access"]
    assert resolve_conversation_mode_id("read_only", modes) == "read-only"
    assert resolve_conversation_mode_id("auto", modes) == "agent"
    assert resolve_conversation_mode_id("bypass", modes) == "agent-full-access"
    assert resolve_conversation_mode_id("ask", modes) is None


def test_edit_approval_aliases_do_not_imply_unrestricted_access():
    from drivers.acp.capabilities import resolve_conversation_mode_id

    for mode in ("accept_edits", "auto_edit"):
        assert resolve_conversation_mode_id("auto", [mode]) == mode
        assert resolve_conversation_mode_id("bypass", [mode]) is None
    assert resolve_conversation_mode_id("bypass", ["dont_ask"]) is None


def test_reported_permission_kind_overrides_legacy_mode_names():
    from drivers.acp.capabilities import resolve_conversation_mode_id
    modes = [{"id": "read-only", "name": "Ask for approval", "_meta": {"kind": "standard"}}, {"id": "agent", "_meta": {"kind": "auto_review"}}, {"id": "agent-full-access", "_meta": {"kind": "full_access"}}]
    assert resolve_conversation_mode_id("ask", modes) == "read-only"
    assert resolve_conversation_mode_id("read_only", modes) is None
    assert resolve_conversation_mode_id("auto", modes) == "agent"
    assert resolve_conversation_mode_id("bypass", modes) == "agent-full-access"
    assert resolve_conversation_mode_id("plan", ["plan"]) == "plan"


def test_suffix_catalog_exposes_only_advertised_reasoning_levels():
    driver = _driver_with(AgentQuirks(model_id_format="effort_suffix"))
    snapshot = driver._snapshot_from_session({"models": {"availableModels": [{"modelId": "one[low]"}, {"modelId": "one[high]"}, {"modelId": "two[medium]"}]}})
    assert [(m.model_id, m.reasoning_levels) for m in snapshot.models] == [("one[low]", ("low", "high")), ("one[high]", ("low", "high")), ("two[medium]", ("medium",))]
