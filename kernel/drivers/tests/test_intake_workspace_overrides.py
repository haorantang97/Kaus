"""Intake directories reach native adapters without changing approval semantics.

All roots and Gateway state are synthetic. Hermes uses a real ACP fixture
subprocess; this verifies directory routing, not an operating-system sandbox.
"""
import copy
import sys
from pathlib import Path

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.tests.test_batch54_session_cwd import _spy
from drivers.hermes import config_yaml, isolated_runtime
from drivers.hermes.driver import HermesDriver
from drivers.openclaw import skill_adapter as openclaw_adapter
from drivers.openclaw.driver import OpenClawAcpDriver
from drivers.openclaw.tests.test_project_mcp import Gateway
from runtime.capability_matrix import BackendCapabilities


@pytest.mark.parametrize("project_scoped", [False, True])
async def test_hermes_override_preserves_settings_and_uses_acp_not_http(
    tmp_path, monkeypatch, project_scoped
):
    monkeypatch.setenv("HERMES_ACP_PYTHON", sys.executable)
    native = tmp_path / "native"
    original = tmp_path / "original"
    staging = tmp_path / "staging"
    for root in (native, original, staging):
        root.mkdir()
    native_config = "model:\n  default: original-model\ntoolsets: [native-tool]\n"
    (native / "config.yaml").write_text(native_config)
    owner = HermesDriver(hermes_root=native)
    owner._capabilities = BackendCapabilities()
    runtime_config = {"workspace_root": str(original), "approval_mode": "auto"}
    if project_scoped:
        runtime_config["project_execution"] = "isolated"
    binding = AgentBinding.create(project="intake", backend="hermes", runtime_config=runtime_config)
    owner.register_binding(binding)
    conversation = Conversation.create(
        project_id=binding.project_id, agent_binding_id=binding.id, title="Synthetic intake"
    ).evolve(reasoning_mode="high")
    entries = [EffectiveCapability(
        capability_type="mcp", capability_id="fixture-service",
        config={"command": "/synthetic/service", "args": ["--project"]},
        source_project_id=binding.project_id, inherited=False,
    )]
    if project_scoped:
        entries.append(EffectiveCapability(
            capability_type="hermes:runtime-config", capability_id="toolsets",
            config={"value": ["terminal"]}, source_project_id=binding.project_id, inherited=False,
        ))
    effective = EffectiveCapabilities(project_id=binding.project_id, entries=tuple(entries))
    created = []
    native_factory = isolated_runtime.create_driver

    def protocol_fixture(owner_arg, binding_arg, **kwargs):
        # Keep the actual Hermes profile selection and environment; only replace
        # the executable with the repository's real stdio protocol fixture.
        configured = native_factory(owner_arg, binding_arg, **kwargs)
        child = AcpDriver(
            fake_agent_spec("text-stream", cwd=str(original)).with_env(**configured.agent_spec.env),
            backend_key="hermes",
        )
        child.register_binding(binding_arg)
        created.append((configured, child))
        return child

    monkeypatch.setattr(isolated_runtime, "create_driver", protocol_fixture)
    monkeypatch.setattr(owner, "supervisor_for", lambda *_: pytest.fail("workspace override reached HTTP"))
    seen = _spy(monkeypatch)
    projection = await owner.workspace_session_projection(
        conversation, effective, workspace_root=str(staging)
    )
    assert projection.options.workspace_root == str(staging)
    assert projection.summary["mcpServers"] == ["fixture-service"]
    handle = await owner.start_runtime(conversation, "card", session_options=projection.options)
    try:
        child, native_handle = owner._isolated_runtimes[handle.runtime_id]
        state = child._runtimes[native_handle.runtime_id]
        assert not owner._runtimes
        assert state.runtime_spec.cwd == str(staging)
        assert state.approval_mode == "auto"
        assert state.conversation.reasoning_mode == "high"
        assert state.runtime_spec.env["KAUS_GROUP_REASONING"] == "high"
        calls = [params for method, params in seen if method == "session/new"]
        assert calls[-1]["cwd"] == str(staging)
        assert calls[-1]["mcpServers"] == list(projection.options.metadata["mcpServers"])
        home = Path(created[-1][0].agent_spec.env["HERMES_HOME"])
        if project_scoped:
            assert home != native
            assert config_yaml.load((home / "config.yaml").read_text())["toolsets"] == ["terminal"]
        else:
            assert home == native
        assert (native / "config.yaml").read_text() == native_config
        assert binding.runtime_config == runtime_config
        assert child._workspace_root_for(binding.id) == str(original)
    finally:
        await owner.stop_runtime(handle)


@pytest.mark.parametrize("approval_mode", ["ask", "auto"])
async def test_openclaw_override_allocates_owned_agents_without_skills_or_mcp(
    tmp_path, monkeypatch, approval_mode
):
    class SyntheticGateway(Gateway):
        config = {"agents": {"ownership": "explicit", "entries": {
            "user": {"workspace": "/synthetic/original", "skills": ["native-skill"]}
        }}, "mcp": {"servers": {"native-server": {"enabled": True}}}}
        sessions = {}
        patches = []

    before = copy.deepcopy(SyntheticGateway.config)
    monkeypatch.setattr(openclaw_adapter, "ScopeGateway", SyntheticGateway)
    spec = AcpAgentSpec(command=("fixture-openclaw", "acp"), cwd=str(tmp_path / "original"),
        inherit_env=False, env={"KAUS_SKILL_STATE_ROOT": str(tmp_path / "state")})
    driver = OpenClawAcpDriver(spec, backend_key="openclaw")
    binding = AgentBinding.create(project="intake", backend="openclaw", runtime_config={
        "workspace_root": spec.cwd, "approval_mode": approval_mode,
    })
    driver.register_binding(binding)
    effective = EffectiveCapabilities(project_id=binding.project_id)
    plans = []
    for name in ("first", "second"):
        workspace = tmp_path / name
        workspace.mkdir()
        conversation = Conversation.create(
            project_id=binding.project_id, agent_binding_id=binding.id, title=name
        )
        projection = await driver.workspace_session_projection(
            conversation, effective, workspace_root=str(workspace)
        )
        plan = driver._skill_plans[conversation.id]
        plans.append(plan)
        assert projection.options.workspace_root == str(workspace)
        assert projection.options.metadata["_meta"]["sessionKey"] == plan.session_key
        assert projection.options.metadata.get("mcpServers", []) == []
        assert plan.agent_config["workspace"] == str(workspace.resolve())
        assert plan.agent_config["cwd"] == str(workspace.resolve())
        assert "skills" not in plan.agent_config
        assert not plan.manage_skills and not plan.mcp_config
        assert SyntheticGateway.config["agents"]["entries"][plan.agent_id] == plan.agent_config
        await openclaw_adapter.validate_turn(plan, str(workspace))
    assert plans[0].agent_id != plans[1].agent_id
    assert plans[0].session_key != plans[1].session_key
    assert SyntheticGateway.config["agents"]["entries"]["user"] == before["agents"]["entries"]["user"]
    assert SyntheticGateway.config["mcp"] == before["mcp"]
    assert driver._approval_mode_for(binding.id) == approval_mode
    assert driver.agent_spec.cwd == spec.cwd
