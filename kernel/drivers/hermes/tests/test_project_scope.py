from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.hermes import config_yaml, isolated_runtime, project_scope
from drivers.hermes.driver import HermesDriver
from drivers.hermes.projector import project_capabilities
from runtime.capability_matrix import BackendCapabilities


def settings(project, tools):
    return EffectiveCapabilities(project_id=project.id, entries=(EffectiveCapability(
        capability_type="hermes:runtime-config", capability_id="toolsets",
        config={"value": tools}, source_project_id=project.id, inherited=False,
    ),))


def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(isolated_runtime, "available", lambda _: True)
    root = tmp_path / "native"
    root.mkdir()
    (root / "config.yaml").write_text("model:\n  default: global-model\ntoolsets: [global-tool]\n")
    (root / ".env").write_text("FAKE_AUTH_FOR_TEST=placeholder\n")
    (root / "state.db").write_bytes(b"private history placeholder")
    (root / "memories").mkdir()
    (root / "memories" / "private.md").write_text("private context")
    driver = HermesDriver(hermes_root=root)
    driver._capabilities = BackendCapabilities()
    return root, driver


def test_first_use_allocates_no_files_and_keeps_credentials_out_of_binding(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    binding = driver.prepare_project_binding(AgentBinding.create(project="one", backend="hermes", origin="domain"))
    assert binding.runtime_config == {"project_execution": "isolated"}
    assert binding.native_scope_ref is None
    assert not (root / ".kaus-scopes").exists()


async def test_two_projects_project_into_private_homes_and_cli_uses_same_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ACP_PYTHON", sys.executable)
    root, driver = seeded(tmp_path, monkeypatch)
    (root / "bin").mkdir()
    browser = root / "bin/browser-use"
    browser.write_text("#!/bin/sh\nexit 0\n")
    browser.chmod(0o700)
    source = (root / "config.yaml").read_bytes()
    bindings = []
    for name, tools in (("one", ["web"]), ("two", ["terminal"])):
        project = Project.create(slug=name, workspace_root=str(tmp_path))
        binding = driver.prepare_project_binding(AgentBinding.create(project=project, backend="hermes",
            origin="domain", runtime_config={"workspace_root": str(tmp_path)}))
        driver.register_binding(binding)
        before = driver.projection_scope(binding)
        await driver.materialize_project_capabilities(project, binding, settings(project, tools))
        assert not before.exists(), "dry run must remain read only"
        result = await driver.materialize_project_capabilities(project, binding, settings(project, tools), dry_run=False)
        assert result.applied[0].after == tools
        home = driver.projection_scope(binding)
        assert config_yaml.load((home / "config.yaml").read_text())["toolsets"] == tools
        assert (home / ".env").is_symlink() and (home / ".env").resolve() == root / ".env"
        assert not (home / "state.db").exists()
        assert not (home / "memories").exists()
        bindings.append(binding)
    assert driver.projection_scope(bindings[0]) != driver.projection_scope(bindings[1])
    assert (root / "config.yaml").read_bytes() == source
    from drivers.hermes.testing.fake_api_server import CAPABILITIES
    from drivers.hermes.capabilities import translate_capabilities
    driver._capabilities = translate_capabilities(CAPABILITIES).capabilities
    conversation = Conversation.create(title="Scoped", project_id=bindings[0].project_id, agent_binding_id=bindings[0].id).evolve(native_session_id="native-one")
    from drivers.hermes.terminal_settings import remember_mcp
    remember_mcp(driver.projection_scope(bindings[0]), conversation, [])
    launch = await driver.build_external_cli_launch(conversation)
    assert launch.path_prefix == (str(root / "bin"),)
    import shutil
    isolated_spec = isolated_runtime.create_driver(driver, bindings[0]).agent_spec
    assert isolated_spec.env["HERMES_HOME"] == str(driver.projection_scope(bindings[0]))
    assert shutil.which("browser-use", path=isolated_spec.env["PATH"]) == str(browser)
    assert launch.config_directory == ("HERMES_HOME", str(driver.projection_scope(bindings[0])))
    assert launch.cwd == str(tmp_path)
    assert "--resume" in launch.command and launch.command[-1] == "native-one"
    assert "-p" not in launch.command


@pytest.mark.parametrize("adopt", [False, True])
def test_legacy_writer_cannot_overwrite_another_project_even_with_adopt(tmp_path, adopt):
    for name in ("one", "two"):
        project = Project.create(slug=name)
        result = project_capabilities(binding_id="binding:" + name, hermes_home=tmp_path,
            effective=settings(project, [name]), capabilities=BackendCapabilities(), project_id=project.id,
            dry_run=False, adopt=adopt)
        if name == "two":
            assert not result.applied
            assert [r.reason for r in result.unsupported] == ["workspace_shared"]
    assert config_yaml.load((tmp_path / "config.yaml").read_text())["toolsets"] == ["one"]


def test_existing_native_profile_mapping_is_preserved(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    binding = AgentBinding.create(project="existing", backend="hermes", native_scope_ref="architect")
    assert driver.projection_scope(binding) == root / "profiles" / "architect"
    assert driver.gateway_config_for(binding).profile == "architect"


def test_new_scope_does_not_inherit_project_managed_values(tmp_path, monkeypatch):
    from drivers.projection_store import Provenance
    root, driver = seeded(tmp_path, monkeypatch)
    provenance = Provenance.load(root)
    provenance.claim("toolsets", project_id="project:another", capability_type="hermes:runtime-config",
        capability_id="toolsets", applied_at="test")
    provenance.save()
    binding = driver.prepare_project_binding(AgentBinding.create(project="one", backend="hermes"))
    home = project_scope.prepare(root, root, binding)
    assert "toolsets" not in config_yaml.load((home / "config.yaml").read_text())


@pytest.mark.parametrize("retire_transport", [False, True])
async def test_project_runtime_uses_shared_acp_events_and_restores_same_native_id(tmp_path, monkeypatch, retire_transport):
    from unittest.mock import AsyncMock
    from drivers.acp.driver import AcpDriver
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec
    from drivers.base import MessageInput
    from drivers.contract_tests.harness import collect_until_run_terminal
    from drivers.computer_use import probe
    # This protocol fake must not mount tools from the developer's machine.
    monkeypatch.setattr(probe, "probe_fallbacks", AsyncMock(return_value=()))
    root, owner = seeded(tmp_path, monkeypatch)
    project = Project.create(slug="one")
    binding = owner.prepare_project_binding(AgentBinding.create(project=project, backend="hermes",
        runtime_config={"workspace_root": str(tmp_path)}))
    owner.register_binding(binding)
    delegate = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key="hermes")
    delegate.register_binding(binding)
    owner._project_drivers[binding.id] = delegate
    monkeypatch.setattr(isolated_runtime, "create_driver", lambda *args, **kwargs: delegate)
    conversation = Conversation.create(title="Scoped", project_id=project.id, agent_binding_id=binding.id)
    projection = await owner.session_options_for(conversation, settings(project, ["web"]))
    assert projection.summary == {"projectSettings": ["toolsets"]}
    runtime = await owner.start_runtime(conversation, "card", session_options=projection.options)
    try:
        assert owner.runtime_reusable(runtime)
        await owner.send_message(runtime, MessageInput(text="hello"))
        events = await collect_until_run_terminal(owner, runtime, timeout=15)
        assert events[-1].event.type == "run.completed"
        assert runtime.binding_id == binding.id
        if retire_transport:
            child, child_handle = owner._isolated_runtimes[runtime.runtime_id]
            assert child_handle.runtime_id != runtime.runtime_id
            child_state = child._state(child_handle)
            child_state.transport_retired = True
            await child_state.connection.aclose()
            assert not owner.runtime_reusable(runtime)
    finally:
        await owner.stop_runtime(runtime)
    assert not owner.runtime_reusable(runtime)
    restored = conversation.evolve(native_session_id=runtime.native_session_id)
    resumed = await owner.start_runtime(restored, "card", session_options=projection.options)
    try:
        assert owner.runtime_reusable(resumed)
        assert resumed.native_session_id == runtime.native_session_id
        await owner.send_message(resumed, MessageInput(text="continue"))
        events = await collect_until_run_terminal(owner, resumed, timeout=15)
        assert events[-1].event.type == "run.completed"
    finally:
        await owner.stop_runtime(resumed)


@pytest.mark.parametrize("closed", [None, False, True])
def test_http_runtime_reusability_preserves_open_gateway_handles(tmp_path, closed):
    owner = HermesDriver(hermes_root=tmp_path)
    runtime = SimpleNamespace(runtime_id="http-fixture")
    if closed is not None:
        owner._runtimes[runtime.runtime_id] = SimpleNamespace(closed=closed)
    assert owner.runtime_reusable(runtime) is (closed is False)


async def test_refresh_removes_project_overrides_and_keeps_native_history(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    project = Project.create(slug="one")
    binding = driver.prepare_project_binding(AgentBinding.create(project=project, backend="hermes"))
    driver.register_binding(binding)
    instructions = EffectiveCapability(capability_type="instructions", capability_id="persona",
        config={"body": "Project-specific identity"}, source_project_id=project.id, inherited=False)
    effective = settings(project, ["web"]).model_copy(update={"entries": (*settings(project, ["web"]).entries, instructions)})
    await driver.materialize_project_capabilities(project, binding, effective, dry_run=False)
    home = driver.projection_scope(binding)
    assert "Project-specific identity" in (home / "SOUL.md").read_text()
    (home / "state.db").write_bytes(b"preserved private history")
    (root / "config.yaml").write_text("model:\n  default: new-global-model\ntoolsets: [global-tool]\n")
    await driver.materialize_project_capabilities(project, binding, EffectiveCapabilities(project_id=project.id), dry_run=False)
    current = config_yaml.load((home / "config.yaml").read_text())
    assert current["toolsets"] == ["global-tool"]
    assert current["model"]["default"] == "new-global-model"
    assert "Project-specific identity" not in (home / "SOUL.md").read_text()
    assert (home / "state.db").read_bytes() == b"preserved private history"


def test_skill_policy_survives_scope_refresh_and_updates_cached_launch(tmp_path, monkeypatch):
    root, owner = seeded(tmp_path, monkeypatch)
    binding = owner.prepare_project_binding(AgentBinding.create(project="one", backend="hermes"))
    home = project_scope.prepare(root, root, binding)
    cached = SimpleNamespace(agent_spec="original", register_binding=lambda _: None)
    owner._project_drivers[binding.id] = cached
    monkeypatch.setattr(isolated_runtime, "create_driver", lambda *_: SimpleNamespace(agent_spec="guarded"))
    assert owner._project_driver(binding).agent_spec == "original"
    marker = home / ".kaus-scope.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "skillPolicyRequired": True}))
    project_scope.prepare(root, root, binding, refresh=True)
    assert json.loads(marker.read_text())["skillPolicyRequired"] is True
    assert owner._project_driver(binding) is cached
    assert cached.agent_spec == "guarded"
