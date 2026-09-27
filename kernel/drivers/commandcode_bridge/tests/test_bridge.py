import asyncio
import json
from pathlib import Path
import sys
import uuid

import pytest

from drivers.commandcode_bridge.bridge import Bridge, DEFAULT_MODEL, model_rows, prompt_text
from drivers.stdio_bridge import BridgeError, Peer


@pytest.fixture
def setup(tmp_path):
    executable = tmp_path / "native.py"
    executable.write_text('''import json, pathlib, sys, time
def emit(value):
 print(json.dumps(value), flush=True)
if "--list-models" in sys.argv:
 print("Available models  ·  2 models\\n\\nProvider\\nvendor/first  Test one\\nvendor/second  Test two\\nDecision models\\ntypesafe/jev  not a chat model")
 sys.exit(0)
prompt=sys.stdin.read()
args=sys.argv[1:]
pathlib.Path("argv.json").write_text(json.dumps(args))
if prompt == "auth":
 sys.exit(3)
if prompt == "broken":
 emit({"type":"event","event":{"type":"run_start","sessionId":"native-exact-1"}})
 sys.exit(1)
emit({"type":"event","event":{"type":"run_start","sessionId":"native-exact-1"}})
if prompt == "wait":
 while True: time.sleep(.02)
if prompt == "slow":
 time.sleep(10)
emit({"type":"event","event":{"type":"message_start"}})
emit({"type":"event","event":{"type":"thinking_delta","delta":"consider"}})
emit({"type":"event","event":{"type":"thinking_end","text":"consider"}})
emit({"type":"event","event":{"type":"tool_queued","toolCallId":"t1","toolName":"write_file","input":{"file_path":"a.txt"}}})
mode=args[args.index("--permission-mode")+1]
if mode=="plan":
 emit({"type":"event","event":{"type":"tool_denied","toolCallId":"t1"}})
else:
 pathlib.Path("a.txt").write_text("only explicit bypass")
 emit({"type":"event","event":{"type":"tool_running","toolCallId":"t1","toolName":"write_file"}})
 emit({"type":"event","event":{"type":"tool_completed","toolCallId":"t1","result":[{"type":"text","text":"written"}]}})
emit({"type":"event","event":{"type":"text_delta","delta":"hello "}})
emit({"type":"event","event":{"type":"text_delta","delta":"world"}})
emit({"type":"result","subtype":"success","sessionId":"native-exact-1","stopReason":"end_turn","finalText":"hello world","usage":{},"durationMs":1})
''')
    frames = []
    bridge = Bridge((sys.executable, str(executable)), tmp_path / "state", Peer(frames.append), turn_timeout=1)
    return bridge, frames, tmp_path


async def new(setup):
    bridge, frames, root = setup
    result = await bridge.dispatch("session/new", {"cwd": str(root), "mcpServers": []})
    return bridge, frames, root, result["sessionId"]


def test_native_model_table_excludes_headers_and_decision_models():
    assert [item["value"] for item in model_rows("Available models  ·  2 models\n\nAnthropic\nclaude-sonnet-5   chat\nvendor/model  choice\nDecision models (headless only)\ntypesafe/jev  classifier")] == ["claude-sonnet-5", "vendor/model"]


def test_binary_attachments_are_explicitly_rejected():
    assert prompt_text([{"type": "resource", "resource": {"uri": "file:///notes", "text": "retain me"}}]) == "file:///notes\nretain me"
    with pytest.raises(BridgeError, match="text attachments"):
        prompt_text([{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}])


async def test_default_plan_does_not_silently_enable_writes(setup):
    bridge, frames, root, ident = await new(setup)
    result = await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "write"}]})
    args = json.loads((root / "argv.json").read_text())
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert "--yolo" not in args and "yolo" not in args
    assert "--continue" not in args
    assert not (root / "a.txt").exists()
    assert result == {"stopReason": "end_turn"}
    messages = [row["params"]["update"] for row in frames]
    assert "".join(row["content"]["text"] for row in messages if row["sessionUpdate"] == "agent_message_chunk") == "hello world"
    assert "".join(row["content"]["text"] for row in messages if row["sessionUpdate"] == "agent_thought_chunk") == "consider"
    assert any(row.get("status") == "failed" and row.get("toolCallId") == "t1" for row in messages)


async def test_only_explicit_bypass_and_model_effort_selection_changes_flags(setup):
    bridge, _, root, ident = await new(setup)
    for key, value in (("mode", "yolo"), ("model", "vendor/second"), ("effort", "high")):
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "optionId": key, "value": value})
    await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "write"}]})
    args = json.loads((root / "argv.json").read_text())
    assert args[args.index("--permission-mode") + 1] == "yolo"
    assert args[args.index("--model") + 1] == "vendor/second"
    assert args[args.index("--effort") + 1] == "high"
    assert (root / "a.txt").read_text() == "only explicit bypass"
    for value in ("ask", "auto", "default", "accept-edits"):
        with pytest.raises(BridgeError):
            await bridge.dispatch("session/set_mode", {"sessionId": ident, "modeId": value})


async def test_resume_uses_exact_native_id_and_load_does_not_prompt(setup):
    bridge, _, root, ident = await new(setup)
    await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "first"}]})
    frames = []
    restored = Bridge(bridge.command, bridge.state_dir, Peer(frames.append), turn_timeout=1)
    restored.models_loaded = True
    before = (root / "argv.json").read_bytes()
    await restored.dispatch("session/load", {"sessionId": ident, "cwd": str(root), "mcpServers": []})
    assert (root / "argv.json").read_bytes() == before
    assert any(row["params"]["update"]["sessionUpdate"] == "agent_message_chunk" for row in frames)
    await restored.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "second"}]})
    args = json.loads((root / "argv.json").read_text())
    assert args[args.index("--resume") + 1] == "native-exact-1"
    assert "--continue" not in args


async def test_prompt_cancel_terminates_native_and_preserves_resume_id(setup):
    bridge, _, _, ident = await new(setup)
    task = asyncio.create_task(bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "wait"}]}))
    for _ in range(100):
        if bridge.sessions[ident].native_id:
            break
        await asyncio.sleep(.01)
    process = bridge.sessions[ident].process
    await bridge.dispatch("session/cancel", {"sessionId": ident})
    assert await task == {"stopReason": "cancelled"}
    assert process.returncode is not None
    assert bridge.sessions[ident].native_id == "native-exact-1"


async def test_turn_timeout_terminates_process(setup):
    bridge, _, _, ident = await new(setup)
    bridge.turn_timeout = .05
    with pytest.raises(BridgeError, match="timed out"):
        await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "slow"}]})
    assert bridge.sessions[ident].process is None


async def test_mcp_and_cross_directory_restore_are_not_silently_accepted(setup):
    bridge, _, root, ident = await new(setup)
    with pytest.raises(BridgeError, match="MCP projection"):
        await bridge.dispatch("session/new", {"cwd": str(root), "mcpServers": [{"name": "m", "command": "noop"}]})
    with pytest.raises(BridgeError, match="another working directory"):
        await bridge.dispatch("session/load", {"sessionId": ident, "cwd": str(root.parent)})
    for value in ("../../auth", "unknown", str(uuid.uuid4())):
        with pytest.raises(BridgeError):
            bridge.session(value)


async def test_native_errors_do_not_report_success_or_leak_stderr(setup):
    bridge, _, _, ident = await new(setup)
    for prompt, error in (("auth", "Authentication required"), ("broken", "without a result")):
        with pytest.raises(BridgeError, match=error):
            await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": prompt}]})


async def test_native_permission_change_stops_instead_of_widening(setup):
    bridge, _, _, ident = await new(setup)
    with pytest.raises(BridgeError, match="changed its permission"):
        bridge.sessions[ident].event({"type": "permission_mode_changed", "mode": "yolo"}, {})
    assert bridge.sessions[ident].mode == "plan"


async def test_model_discovery_never_invents_vendor_models(setup):
    bridge, _, _, ident = await new(setup)
    assert [row["value"] for row in bridge.models] == ["vendor/first", "vendor/second"]
    assert bridge.sessions[ident].model == DEFAULT_MODEL
    assert "--model" not in bridge.sessions[ident].argv()
    with pytest.raises(BridgeError, match="Unknown model"):
        bridge.sessions[ident].set_option("model", "invented-model")


async def test_own_session_file_is_private_and_native_config_is_not_written(setup):
    bridge, _, root, ident = await new(setup)
    state = bridge.state_dir / f"{ident}.json"
    assert state.stat().st_mode & 0o777 == 0o600
    assert not (root / ".commandcode").exists()
    capabilities = (await bridge.dispatch("initialize", {}))["agentCapabilities"]
    assert capabilities["promptCapabilities"]["image"] is False
    assert capabilities["mcpCapabilities"] == {"http": False, "sse": False}


async def test_shared_driver_applies_model_effort_and_only_offers_real_permissions(setup):
    from drivers.acp.client import AcpAgentSpec
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset
    from drivers.acp.testing.harness import FakeAcpHarness
    from drivers.base import MessageInput, UnsupportedCapabilityError
    from drivers.contract_tests.harness import collect_until_run_terminal

    bridge, _, root = setup
    native = Path(bridge.command[1])
    native.write_text(f"#!{sys.executable}\n" + native.read_text())
    native.chmod(0o700)
    preset = get_preset("commandcode")
    driver = AcpDriver(AcpAgentSpec(command=(*preset.command, "--command", str(native),
        "--state-dir", str(root / "driver-state"))), preset=preset, default_cwd=str(root))
    harness = FakeAcpHarness(workspace_root=str(root))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).evolve(
        model_id="vendor/second", reasoning_mode="high", approval_mode="plan")
    runtime = await driver.start_runtime(conversation, "card")
    try:
        controls = driver.conversation_controls(binding)
        assert set(controls["approvalModes"]) == {"plan", "bypass"}
        assert controls["approvalDefault"] == "plan"
        assert controls["executionModes"] == []
        assert controls["reasoningDefault"] == "high"
        await driver.send_message(runtime, MessageInput(text="write"))
        events = await collect_until_run_terminal(driver, runtime, timeout=5)
        assert events[-1].event.type == "run.completed"
        args = json.loads((root / "argv.json").read_text())
        assert args[args.index("--model") + 1] == "vendor/second"
        assert args[args.index("--effort") + 1] == "high"
        assert args[args.index("--permission-mode") + 1] == "plan"
        assert not (root / "a.txt").exists()
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_selection(runtime, conversation.evolve(approval_mode="ask"))
        await driver.set_conversation_selection(runtime, conversation.evolve(approval_mode="bypass"))
        await driver.send_message(runtime, MessageInput(text="write"))
        assert (await collect_until_run_terminal(driver, runtime, timeout=5))[-1].event.type == "run.completed"
        args = json.loads((root / "argv.json").read_text())
        assert args[args.index("--permission-mode") + 1] == "yolo"
        assert args[args.index("--resume") + 1] == "native-exact-1"
        assert (root / "a.txt").read_text() == "only explicit bypass"
    finally:
        await driver.stop_runtime(runtime)
