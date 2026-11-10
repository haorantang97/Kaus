"""Driver tests on the fake app-server child: turns, approvals, interrupt, resume,
model selection, config delivery, error and auth paths. No real binary, no home dir."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.acp.presets import get_preset
from drivers.base import AuthRequiredError, InteractionResponse, MessageInput, TurnAlreadyRunningError
from drivers.codex_app_server.driver import CodexAppServerDriver, build_driver
from drivers.codex_app_server.testing.fake_app_server import fake_app_server_spec
from drivers.contract_tests.harness import collect_in_background, collect_until_run_terminal, events_of_type


@pytest.fixture(autouse=True)
def _no_native_inventory(monkeypatch):
    monkeypatch.setattr("drivers.mcp_inventory.preset_sources", lambda *a, **k: ())


def make_driver(tmp_path: Path, scenario: str = "text-stream", *, record: Path | None = None, preset=None) -> CodexAppServerDriver:
    return CodexAppServerDriver(
        fake_app_server_spec(scenario, cwd=str(tmp_path), record=str(record) if record else None),
        backend_key="codex", default_cwd=str(tmp_path), call_timeout=20.0, turn_timeout=30.0,
        interrupt_grace=3.0, native_history=False, preset=preset,
    )


def wire(driver: CodexAppServerDriver, tmp_path: Path, **config):
    project = Project.create(slug="p", display_name="P", workspace_root=str(tmp_path))
    binding = AgentBinding.create(project=project, backend=driver.backend_id, native_scope_ref="s", is_default=True,
                                  runtime_config={"workspace_root": str(tmp_path), **config})
    driver.register_binding(binding)
    return project, binding, Conversation.create(project_id=project.id, agent_binding_id=binding.id, title="t")


def records(path: Path, method: str) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row["params"] for row in rows if row["method"] == method]


async def test_full_turn_streams_reasoning_and_text_into_one_message(tmp_path) -> None:
    driver = make_driver(tmp_path)
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        assert handle.metadata["transport"] == "app-server" and handle.native_session_id
        await driver.send_message(handle, MessageInput(text="hi"))
        envelopes = await collect_until_run_terminal(driver, handle, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    types = [e.event.type for e in envelopes]
    assert types[0] == "session.created" and types[-1] == "run.completed"
    assert types.count("message.started") == 1 and types.count("message.completed") == 1
    assert "reasoning.delta" in types and types.count("message.delta") >= 3
    assert "".join(e.event.text for e in events_of_type(envelopes, "message.delta")) == "Hello from the fake app-server."
    run_ids = {e.run_id for e in envelopes if e.run_id}
    assert len(run_ids) == 1 and next(iter(run_ids)).startswith(handle.native_session_id + ":")


async def test_tool_item_streams_output_increments_on_one_card(tmp_path) -> None:
    driver = make_driver(tmp_path, "tool-lifecycle")
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(handle, MessageInput(text="ls"))
        envelopes = await collect_until_run_terminal(driver, handle, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    starts = events_of_type(envelopes, "tool.started")
    updates = events_of_type(envelopes, "tool.updated")
    completes = events_of_type(envelopes, "tool.completed")
    assert len(starts) == 1 and len(completes) == 1 and len(updates) == 2
    assert {e.event.call_id for e in (*starts, *updates, *completes)} == {starts[0].event.call_id}
    assert all(u.event.cumulative is False for u in updates)
    assert completes[0].event.output == "total 0\ndrwxr-xr-x  2 u  w  64 .\n" and completes[0].event.is_error is False


async def test_command_approval_round_trip_accept_and_decline(tmp_path) -> None:
    for option, expected in (("accept", "Done."), ("decline", "Skipped.")):
        driver = make_driver(tmp_path, "permission")
        _, _, conversation = wire(driver, tmp_path)
        handle = await driver.start_runtime(conversation, "card")
        try:
            async def resolve(envelope, option=option):
                await driver.resolve_interaction(handle, envelope.event.request.request_id,
                                                 InteractionResponse(kind="permission", option_id=option))
            await driver.send_message(handle, MessageInput(text="run"))
            envelopes = await collect_until_run_terminal(driver, handle, on_interaction=resolve, timeout=10)
        finally:
            await driver.stop_runtime(handle)
        requested = events_of_type(envelopes, "permission.requested")
        resolved = events_of_type(envelopes, "permission.resolved")
        assert len(requested) == 1 and len(resolved) == 1 and resolved[0].event.decision == option
        assert requested[0].event.request.title == "ls -la"
        assert [o.kind for o in requested[0].event.request.options] == ["allow_once", "allow_always", "reject_once", "reject_always"]
        text = "".join(e.event.text for e in events_of_type(envelopes, "message.delta"))
        assert text == expected
        tools = events_of_type(envelopes, "tool.completed")
        assert tools[-1].event.is_error is (option == "decline")


async def test_file_change_approval_emits_file_changed(tmp_path) -> None:
    driver = make_driver(tmp_path, "file-change")
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        async def resolve(envelope):
            assert envelope.event.request.detail == "/tmp"
            await driver.resolve_interaction(handle, envelope.event.request.request_id,
                                             InteractionResponse(kind="permission", option_id="acceptForSession"))
        await driver.send_message(handle, MessageInput(text="patch"))
        envelopes = await collect_until_run_terminal(driver, handle, on_interaction=resolve, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    assert [e.event.path for e in events_of_type(envelopes, "file.changed")] == ["/tmp/a.txt", "/tmp/b.txt"]
    assert events_of_type(envelopes, "tool.started")[0].event.name == "apply_patch"


async def test_user_input_question_round_trip(tmp_path) -> None:
    driver = make_driver(tmp_path, "question")
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        async def resolve(envelope):
            request = envelope.event.request
            assert [o.label for o in request.options] == ["red", "blue"] and request.allow_free_text
            await driver.resolve_interaction(handle, request.request_id, InteractionResponse(kind="question", option_id="o2"))
        await driver.send_message(handle, MessageInput(text="ask"))
        envelopes = await collect_until_run_terminal(driver, handle, on_interaction=resolve, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    assert len(events_of_type(envelopes, "question.requested")) == 1 and len(events_of_type(envelopes, "question.resolved")) == 1
    assert "".join(e.event.text for e in events_of_type(envelopes, "message.delta")) == "You chose blue."


async def test_interrupt_sends_turn_interrupt_and_converges(tmp_path) -> None:
    record = tmp_path / "record.jsonl"
    driver = make_driver(tmp_path, "interrupt", record=record)
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        collector = await collect_in_background(driver, handle, timeout=10)
        await driver.send_message(handle, MessageInput(text="long"))
        await asyncio.sleep(0.2)
        await driver.interrupt(handle)
        envelopes = await collector
    finally:
        await driver.stop_runtime(handle)
    assert events_of_type(envelopes, "run.interrupted")
    interrupts = records(record, "turn/interrupt")
    assert len(interrupts) == 1 and interrupts[0]["threadId"] == handle.native_session_id


async def test_second_send_while_a_turn_runs_is_rejected(tmp_path) -> None:
    driver = make_driver(tmp_path, "interrupt")
    _, _, conversation = wire(driver, tmp_path)
    handle = await driver.start_runtime(conversation, "card")
    try:
        collector = await collect_in_background(driver, handle, timeout=10)
        await driver.send_message(handle, MessageInput(text="long"))
        await asyncio.sleep(0.1)
        with pytest.raises(TurnAlreadyRunningError):
            await driver.send_message(handle, MessageInput(text="again"))
        await driver.interrupt(handle)
        await collector
    finally:
        await driver.stop_runtime(handle)


async def test_failed_turn_and_rejected_turn_start_converge(tmp_path) -> None:
    for scenario, code in (("failure", "responseStreamDisconnected"), ("turn-start-error", "-32603")):
        driver = make_driver(tmp_path, scenario)
        _, _, conversation = wire(driver, tmp_path)
        handle = await driver.start_runtime(conversation, "card")
        try:
            await driver.send_message(handle, MessageInput(text="x"))
            envelopes = await collect_until_run_terminal(driver, handle, timeout=10)
        finally:
            await driver.stop_runtime(handle)
        failures = events_of_type(envelopes, "run.failed")
        assert len(failures) == 1 and failures[0].event.error.code == code


async def test_auth_required_before_the_first_turn(tmp_path) -> None:
    driver = make_driver(tmp_path, "auth-required", preset=get_preset("codex"))
    _, binding, conversation = wire(driver, tmp_path)
    with pytest.raises(AuthRequiredError) as caught:
        await driver.start_runtime(conversation, "card")
    assert caught.value.failure is not None and caught.value.failure.code == "auth_required"
    assert "codex login" in (caught.value.failure.hint or "")
    auth = await driver.read_auth_state(binding)
    assert auth.state == "signed_out" and auth.model == "own-auth"
    assert not driver._runtimes


async def test_resume_uses_thread_resume_with_the_saved_id(tmp_path) -> None:
    record = tmp_path / "record.jsonl"
    driver = make_driver(tmp_path, record=record)
    _, _, conversation = wire(driver, tmp_path)
    saved = conversation.evolve(native_session_id="7d7c2a0e-4a7e-4a2a-9a38-5a1e2e5b3c21")
    handle = await driver.start_runtime(saved, "card")
    try:
        assert handle.native_session_id == saved.native_session_id and handle.metadata["resumed"] is True
        await driver.send_message(handle, MessageInput(text="continue"))
        envelopes = await collect_until_run_terminal(driver, handle, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    assert envelopes[0].event.type == "session.resumed"
    resumes = records(record, "thread/resume")
    assert len(resumes) == 1 and resumes[0]["threadId"] == saved.native_session_id and resumes[0]["cwd"] == str(tmp_path)
    assert records(record, "thread/start") == []
    turns = records(record, "turn/start")
    assert turns[0]["threadId"] == saved.native_session_id


async def test_model_effort_and_policy_reach_thread_start_and_turn_start(tmp_path) -> None:
    record = tmp_path / "record.jsonl"
    driver = make_driver(tmp_path, record=record)
    _, binding, conversation = wire(driver, tmp_path, approval_mode="auto")
    chosen = conversation.evolve(model_id="gpt-5.1", reasoning_mode="high", approval_mode="read_only")
    handle = await driver.start_runtime(chosen, "card")
    try:
        await driver.send_message(handle, MessageInput(text="go"))
        await collect_until_run_terminal(driver, handle, timeout=10)
        controls = driver.runtime_conversation_controls(handle, binding)
        assert controls["approvalDefault"] == "read_only" and controls["reasoningDefault"] == "high"
        # a mid-conversation policy change is resent on the next turn only
        await driver.set_conversation_selection(handle, chosen.evolve(approval_mode="bypass", reasoning_mode="low"))
        await driver.send_message(handle, MessageInput(text="again"))
        await collect_until_run_terminal(driver, handle, timeout=10)
    finally:
        await driver.stop_runtime(handle)
    start = records(record, "thread/start")[0]
    assert (start["model"], start["approvalPolicy"], start["approvalsReviewer"], start["sandbox"]) == ("gpt-5.1", "on-request", "user", "read-only")
    assert start["config"]["model_reasoning_effort"] == "high"
    turns = records(record, "turn/start")
    assert (turns[0]["model"], turns[0]["effort"]) == ("gpt-5.1", "high") and "approvalPolicy" not in turns[0]
    assert (turns[1]["effort"], turns[1]["approvalPolicy"], turns[1]["sandboxPolicy"]) == ("low", "never", {"type": "dangerFullAccess"})
    assert turns[0]["input"] == [{"type": "text", "text": "go"}]


async def test_launch_delivery_lands_in_thread_start_config(tmp_path, monkeypatch) -> None:
    """Instructions → developerInstructions; native MCP exclusion, Kaus MCP and memories → config."""
    monkeypatch.setattr("drivers.mcp_inventory.preset_sources", lambda *a, **k: ("src",))
    monkeypatch.setattr("drivers.mcp_inventory.discover_mcp", lambda sources: [
        {"name": "native-fs", "capabilityId": "mcp:codex:native-fs", "sourcePath": "x", "server": {}},
        {"name": "kept", "capabilityId": "mcp:codex:kept", "sourcePath": "x", "server": {}}])
    record = tmp_path / "record.jsonl"
    driver = make_driver(tmp_path, record=record, preset=get_preset("codex"))
    _, _, conversation = wire(driver, tmp_path)
    from app.projects.builtin_mcp import BUILTIN_MCP_ID as MEMORY_MCP_ID
    effective = EffectiveCapabilities(project_id="project:p", entries=(
        EffectiveCapability(capability_type="instructions", capability_id="house", config={"body": "Be brief.", "title": "风格"},
                            source_project_id="project:p", inherited=False),
        EffectiveCapability(capability_type="mcp", capability_id=MEMORY_MCP_ID,
                            config={"value": {"command": sys.executable, "args": ["-m", "kaus_memory"]}, "memory": True},
                            source_project_id="project:p", inherited=False),
        EffectiveCapability(capability_type="mcp", capability_id="kept", config={"value": {"command": "uvx", "args": ["kept"]}},
                            source_project_id="project:p", inherited=False),
    ), native_mcp=(EffectiveCapability(capability_type="codex:mcp", capability_id="mcp:codex:kept", config={},
                                       source_project_id="project:p", inherited=False),))
    projection = await driver.session_options_for(conversation, effective)
    assert projection is not None
    assert projection.summary["instructions"] == ["house"] and projection.summary["nativeMcpExcluded"] == ["native-fs"]
    assert projection.summary["nativeMemory"] == ["unverified"] and projection.summary["nativeMcp"] == ["unverified"]
    assert set(projection.summary["mcpServers"]) == {MEMORY_MCP_ID, "kept"}
    assert await driver.unapplied_project_capabilities(conversation, effective) == ()
    handle = await driver.start_runtime(conversation, "card", session_options=projection.options)
    await driver.stop_runtime(handle)
    start = records(record, "thread/start")[0]
    assert start["developerInstructions"] == "## 风格\n\nBe brief."
    config = start["config"]
    assert "developer_instructions" not in config
    assert config["mcp_servers"]["native-fs"] == {"enabled": False}
    assert config["mcp_servers"][MEMORY_MCP_ID]["command"] == sys.executable and config["mcp_servers"][MEMORY_MCP_ID]["enabled"] is True
    assert config["mcp_servers"]["kept"]["args"] == ["kept"]
    assert config["memories"] == {"generate_memories": False, "use_memories": False} and config["features"]["memories"] is False
    assert not (tmp_path / "AGENTS.md").exists()


async def test_api_key_is_injected_only_into_the_child_environment(tmp_path, monkeypatch) -> None:
    from drivers import credentials
    monkeypatch.setattr(credentials, "_provider", lambda backend_id: "sk-test-fake" if backend_id == "backend:codex" else None)
    driver = make_driver(tmp_path, preset=get_preset("codex"))
    captured = {}
    from drivers.acp import client as client_module
    original = client_module.AcpConnection.spawn.__func__

    async def spawn(cls, spec):
        captured["env"] = spec.build_env().get("OPENAI_API_KEY")
        return await original(cls, spec)
    monkeypatch.setattr(client_module.AcpConnection, "spawn", classmethod(spawn))
    result = await driver.probe()
    assert result.state == "ready" and result.version == "0.167.0"
    assert captured["env"] == "sk-test-fake" and "OPENAI_API_KEY" not in driver.agent_spec.env


async def test_model_catalog_comes_from_model_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("drivers.discovery_scope.prepare_discovery_credentials", _noop)
    driver = make_driver(tmp_path)
    _, binding, _ = wire(driver, tmp_path)
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["gpt-5.1-codex", "gpt-5.1"]
    assert catalog.models[0].reasoning_levels == ("low", "medium", "high") and catalog.supports_reasoning
    assert catalog.default_model_id == "gpt-5.1-codex" and catalog.selection_applies is True and not catalog.degraded
    assert (await driver.read_auth_state(binding)).state == "signed_in"
    controls = driver.conversation_controls(binding)
    assert controls["approvalModes"] == ["ask", "auto", "read_only", "bypass"] and controls["reasoningLevels"] == ["low", "medium", "high"]


async def _noop(*args, **kwargs):
    return None


async def test_probe_of_missing_binary_is_unavailable(tmp_path) -> None:
    driver = CodexAppServerDriver(fake_app_server_spec().model_copy(update={"command": ("no-such-codex-xyz", "app-server")}),
                                  backend_key="codex", native_history=False)
    result = await driver.probe()
    assert result.state == "unavailable" and result.installed is False and result.message


def test_build_driver_uses_the_selected_cli_and_falls_back_to_path(tmp_path) -> None:
    preset = get_preset("codex")
    spec = SimpleNamespace(backend_id="backend:codex", cwd=None, options={"transport": "app-server"})
    driver = build_driver(spec, preset=preset, process_env={"PATH": "/nonexistent"})
    assert driver.agent_spec.command == ("codex", "app-server") and driver.preset is preset
    assert driver.driver_kind == "native" and driver.backend_id == "backend:codex"
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    driver = build_driver(spec, preset=preset, process_env={}, native_cli_path=str(fake))
    assert driver.agent_spec.command == (str(fake), "app-server") and driver.agent_spec.env["CODEX_PATH"] == str(fake)
