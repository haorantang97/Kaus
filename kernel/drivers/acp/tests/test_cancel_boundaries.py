"""Stopping a turn revokes both visible approvals and deferred file writes."""
import asyncio

import pytest

from drivers.acp.client import AcpRpcError, AcpTransportError
from drivers.acp.presets import AcpPreset, AgentQuirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import InteractionNotFoundError, InteractionResponse, MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal


async def opened(tmp_path, scenario="text-stream"):
    preset = AcpPreset(id="interaction-test", label="Test", command=("unused",),
        elicitation_forms=True, client_methods={"questions": "ext/questions", "plan": "ext/plan"},
        quirks=AgentQuirks(needs_client_fs=True))
    harness = FakeAcpHarness(preset=preset, workspace_root=str(tmp_path))
    driver = harness.make_driver()
    harness.arrange_script(driver, scenario)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    return driver, runtime, driver._state(runtime)


async def open_write(driver, state, tmp_path, rpc_id="write"):
    await driver._on_fs_request(state, rpc_id, "fs/write_text_file", {
        "sessionId": state.session_id, "path": str(tmp_path / "late.txt"), "content": "must not land"})
    return state.translator.pending_permissions()[-1].request_id


async def open_permission_and_form(driver, state):
    await driver._on_request(state, "permission", "session/request_permission", {
        "sessionId": state.session_id, "toolCall": {"toolCallId": "t1"},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]})
    await driver._on_request(state, "form", "elicitation/create", {
        "sessionId": state.session_id, "requestedSchema": {
            "type": "object", "properties": {"choice": {"type": "string"}}}})


async def test_interrupt_revokes_approval_and_blocks_late_requests(tmp_path, monkeypatch):
    driver, runtime, state = await opened(tmp_path)
    replies = []
    monkeypatch.setattr(state.connection, "respond", lambda *args, **kwargs: replies.append((args, kwargs)))
    try:
        request_id = await open_write(driver, state, tmp_path)
        await open_permission_and_form(driver, state)
        await driver.interrupt(runtime)
        assert not state.fs_writes and not state.translator.pending_permissions()
        assert not state.client_interactions.pending
        with pytest.raises(InteractionNotFoundError):
            await driver.resolve_interaction(runtime, request_id, InteractionResponse(kind="permission", option_id="allow"))
        assert not (tmp_path / "late.txt").exists()
        state.approval_mode = "auto"  # A delayed tool call must not use automatic approval.
        await driver._on_request(state, "late-write", "fs/write_text_file", {
            "path": str(tmp_path / "late.txt"), "content": "no"})
        await open_permission_and_form(driver, state)
        await driver._on_request(state, "late-plan", "ext/plan", {"plan": "write now"})
        assert not (tmp_path / "late.txt").exists()
        assert not state.translator.pending_permissions() and not state.client_interactions.pending
        assert any(args == ("permission", {"outcome": {"outcome": "cancelled"}}) for args, _ in replies)
        assert any(args == ("form", {"action": "cancel"}) for args, _ in replies)
        assert any(args == ("late-plan", {"outcome": {"outcome": "cancelled"}}) for args, _ in replies)
    finally:
        await driver.stop_runtime(runtime)


async def test_stop_cleans_local_requests_even_when_transport_cannot_reply(tmp_path, monkeypatch):
    driver, runtime, state = await opened(tmp_path)
    await open_write(driver, state, tmp_path)
    await open_permission_and_form(driver, state)
    def disconnected(*args, **kwargs):
        raise AcpTransportError("disconnected")
    monkeypatch.setattr(state.connection, "respond", disconnected)
    monkeypatch.setattr(state.client_interactions, "respond", disconnected)
    await driver.stop_runtime(runtime)
    assert state.closed
    assert not state.fs_writes and not state.translator.pending_permissions()
    assert not state.client_interactions.pending and not (tmp_path / "late.txt").exists()


@pytest.mark.parametrize("outcome", ["success", "rpc_error", "transport_error"])
async def test_prompt_end_resolves_all_cards_before_terminal_and_rejects_late_writes(tmp_path, monkeypatch, outcome):
    driver, runtime, state = await opened(tmp_path)
    replies = []
    monkeypatch.setattr(state.connection, "respond", lambda *args, **kwargs: replies.append((args, kwargs)))
    async def finish_with_pending(method, params, **kwargs):
        assert method == "session/prompt"
        await open_write(driver, state, tmp_path)
        await open_permission_and_form(driver, state)
        if outcome == "rpc_error":
            raise AcpRpcError(-32000, "failed")
        if outcome == "transport_error":
            raise AcpTransportError("closed")
        return {"stopReason": "end_turn"}
    monkeypatch.setattr(state.connection, "call", finish_with_pending)
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        events = await collect_until_run_terminal(driver, runtime, timeout=3)
        assert events[-1].event.type == ("run.completed" if outcome == "success" else "run.failed")
        assert sum(e.event.type in ("permission.resolved", "question.resolved") for e in events[:-1]) == 3
        assert not state.fs_writes and not state.translator.pending_permissions() and not state.client_interactions.pending
        await state.prompt_task
        state.approval_mode = "auto"
        await driver._on_fs_request(state, "after-end", "fs/write_text_file", {"path": str(tmp_path / "late.txt"), "content": "no"})
        assert not (tmp_path / "late.txt").exists()
    finally:
        await driver.stop_runtime(runtime)


async def test_stdio_permission_waiter_is_released_on_interrupt(tmp_path):
    driver, runtime, state = await opened(tmp_path, "permission")
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        seen = []
        async with asyncio.timeout(5):
            async for envelope in driver.events(runtime):
                seen.append(envelope.event.type)
                if envelope.event.type == "permission.requested":
                    await driver.interrupt(runtime)
                if envelope.event.type in ("run.completed", "run.interrupted", "run.failed"):
                    break
        assert "permission.resolved" in seen
        assert seen[-1] == "run.interrupted"
        assert not state.translator.pending_permissions()
    finally:
        await driver.stop_runtime(runtime)
