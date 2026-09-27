"""Exercise the shared driver seam, external config changes and native bridging."""
import pytest

from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AcpPreset, AgentQuirks, get_preset
from drivers.acp.testing.fake_acp_agent import fake_agent_spec, dress_from_quirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal
from drivers.group_execution import claude
from drivers.hermes.driver import HermesDriver


@pytest.mark.parametrize("preset_id", ["claude-code", "codex", "antigravity", "opencode",
    "gemini", "qwen", "pi", "openclaw", "dsh", "deepseek-acp", "kilo"])
async def test_coordinator_uses_normal_stdio_driver_in_separate_processes(tmp_path, preset_id):
    preset = get_preset(preset_id)
    harness = FakeAcpHarness(preset=preset, workspace_root=str(tmp_path))
    driver = harness.make_driver()
    project = harness.make_project()
    original = harness.make_binding(project, driver)
    binding = original.model_copy(update={"runtime_config": {
        **original.runtime_config, "group_execution": "planning"}})
    driver.register_binding(binding)
    assert driver.group_context_isolation()
    first = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    second = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert first.conversation_id != second.conversation_id
        assert driver._runtimes[first.runtime_id].connection is not driver._runtimes[second.runtime_id].connection
        assert not first.metadata["resumed"] and not second.metadata["resumed"]
        assert not original.runtime_config.get("group_execution")
        await driver.send_message(first, MessageInput(text="hello"))
        events = await collect_until_run_terminal(driver, first)
        assert any(e.event.type == "message.completed" for e in events)
    finally:
        await driver.stop_runtime(first)
        await driver.stop_runtime(second)


def test_current_custom_model_is_visible_without_a_catalog():
    driver = AcpDriver(fake_agent_spec())
    snapshot = driver._snapshot_from_session({"models": {
        "currentModelId": "custom/model-x", "availableModels": []}})
    assert snapshot.current_model_id == "custom/model-x"
    assert [m.model_id for m in snapshot.models] == ["custom/model-x"]


def test_memory_override_preserves_external_route_and_config_home():
    spec = fake_agent_spec().with_env(CLAUDE_CONFIG_DIR="/tmp/source-config",
        ANTHROPIC_BASE_URL="http://127.0.0.1:1234")
    changed = claude.prepare(spec, "review")
    assert changed.command == spec.command
    assert changed.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert changed.env["CLAUDE_CONFIG_DIR"] == spec.env["CLAUDE_CONFIG_DIR"]
    assert changed.env["ANTHROPIC_BASE_URL"] == spec.env["ANTHROPIC_BASE_URL"]
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in spec.env


async def test_external_switch_invalidates_catalog_without_waiting_for_ttl(tmp_path, monkeypatch):
    config = tmp_path / "settings.json"
    config.write_text('{"model":"one"}')
    preset = AcpPreset(id="sample", label="Sample", command=("fake",),
        config_root_default=str(tmp_path), config_watch_files=("settings.json",))
    harness = FakeAcpHarness(preset=preset)
    driver = harness.make_driver()
    binding = harness.make_binding(harness.make_project(), driver)
    calls = []
    async def probe(_):
        calls.append(config.stat().st_size)
        return driver._snapshot_from_session({"models": {"currentModelId": str(len(calls))}})
    monkeypatch.setattr(driver, "_probe_catalog", probe)
    first = await driver._catalog_snapshot(binding)
    assert await driver._catalog_snapshot(binding) is first
    config.write_text('{"model":"a-new-custom-model"}')
    next_catalog = await driver._catalog_snapshot(binding)
    assert len(calls) == 2
    assert next_catalog.current_model_id == "2"


async def test_native_binding_reuses_acp_lifecycle_and_retains_identity(tmp_path, monkeypatch):
    from drivers.hermes import isolated_runtime
    from app.projects.models import AgentBinding, Project
    from app.conversations.models import Conversation
    project = Project.create(slug="bridge", display_name="Bridge")
    binding = AgentBinding.create(project=project.id, backend="hermes", native_scope_ref="source",
        runtime_config={"workspace_root": str(tmp_path), "group_execution": "review"})
    owner = HermesDriver(hermes_root=tmp_path)
    owner.register_binding(binding)
    created = []
    def factory(_, selected):
        preset = AcpPreset(id="sample", label="Sample", command=("fake",),
            quirks=AgentQuirks(supports_set_model=True, model_switch="set_model"))
        d = AcpDriver(fake_agent_spec("text-stream", cwd=str(tmp_path), dress=dress_from_quirks(preset.quirks)),
            backend_key="hermes", preset=preset)
        d.register_binding(selected)
        created.append(d)
        return d
    monkeypatch.setattr(isolated_runtime, "create_driver", factory)
    conv = Conversation.create(title="Review", project_id=project.id, agent_binding_id=binding.id,
        model_id="small", provider_id="fake")
    first = await owner.start_runtime(conv, "card")
    second = await owner.start_runtime(Conversation.create(title="Review two", project_id=project.id,
        agent_binding_id=binding.id), "card")
    try:
        assert first.runtime_id != second.runtime_id
        assert first.backend_id == binding.backend_id
        assert first.binding_id == binding.id
        assert len(created) == 2 and not owner._runtimes
        await owner.send_message(first, MessageInput(text="hello"))
        events = await collect_until_run_terminal(owner, first)
        assert all(e.backend_id == binding.backend_id for e in events)
        assert any(e.event.type == "run.completed" for e in events)
    finally:
        await owner.stop_runtime(first)
        await owner.stop_runtime(second)
    assert not owner._isolated_runtimes
    assert all(not d._runtimes for d in created)


async def test_delegated_execution_mounts_project_tools_and_routes_permissions(tmp_path, monkeypatch):
    from drivers.hermes import isolated_runtime
    from drivers.acp.tests.test_batch43_mcp_session_new import _effective
    from drivers.base import InteractionResponse
    from app.projects.models import AgentBinding
    from app.conversations.models import Conversation
    owner = HermesDriver(hermes_root=tmp_path)
    binding = AgentBinding.create(project="project:batch43", backend="hermes",
        runtime_config={"workspace_root": str(tmp_path), "group_execution": "planning"})
    owner.register_binding(binding)
    def factory(_, binding):
        driver = AcpDriver(fake_agent_spec("mcp-ping-permission", cwd=str(tmp_path)), backend_key="hermes")
        driver.register_binding(binding)
        return driver
    monkeypatch.setattr(isolated_runtime, "create_driver", factory)
    conv = Conversation.create(title="Tool check", project_id=binding.project_id, agent_binding_id=binding.id)
    projection = await owner.session_options_for(conv, _effective("shared-tool-nonce"))
    assert projection and projection.summary == {"mcpServers": ["kaus-probe"]}
    handle = await owner.start_runtime(conv, "card", session_options=projection.options)
    async def approve(envelope):
        await owner.resolve_interaction(handle, envelope.event.request.request_id,
            InteractionResponse(kind="permission", option_id="allow"))
    try:
        await owner.send_message(handle, MessageInput(text="Use the attached tool"))
        assert await owner.run_is_active(handle) is not False
        events = await collect_until_run_terminal(owner, handle, on_interaction=approve, timeout=20)
        assert any(e.event.type == "permission.requested" for e in events)
        assert any("pong-shared-tool-nonce" in (getattr(e.event, "text", "") or "") for e in events)
        assert events[-1].event.type == "run.completed"
    finally:
        await owner.stop_runtime(handle)


async def test_current_protocol_config_id_is_negotiated_without_changing_model_value():
    from types import SimpleNamespace
    from drivers.acp.client import AcpRpcError
    accepted = []
    class Connection:
        async def call(self, method, params, **_):
            if "configId" not in params:
                raise AcpRpcError(-32602, "Missing configId")
            accepted.append((method, params))
    driver = AcpDriver(fake_agent_spec())
    state = SimpleNamespace(handle=SimpleNamespace(binding_id="sample"), session_id="session",
        connection=Connection())
    await driver._set_model_config_option(state, '["third-party","model-x"]')
    assert accepted == [("session/set_config_option", {"sessionId": "session", "configId": "model",
        "value": '["third-party","model-x"]'})]


async def test_fixed_engine_never_silently_substitutes_a_coordinator_model(tmp_path):
    from drivers.base import UnsupportedCapabilityError
    harness = FakeAcpHarness(preset=AcpPreset(id="fixed", label="Fixed", command=("fake",)),
        workspace_root=str(tmp_path))
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver).model_copy(update={"runtime_config": {
        "workspace_root": str(tmp_path), "group_execution": "planning"}})
    driver.register_binding(binding)
    conv = harness.make_conversation(project, binding).evolve(model_id="fake:large")
    with pytest.raises(UnsupportedCapabilityError, match="不支持切换"):
        await driver.start_runtime(conv, "card")
    assert not driver._runtimes
