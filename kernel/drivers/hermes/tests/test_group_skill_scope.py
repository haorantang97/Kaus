"""The Group path must materialize the frozen selection before ACP startup."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.base import CreateSessionOptions, SessionProjection, UnsupportedCapabilityError
from drivers.hermes import isolated_runtime, project_scope
from drivers.hermes.driver import HermesDriver
from runtime.capability_matrix import BackendCapabilities
from .test_skill_projection import asset, package, selection


@pytest.mark.parametrize("role", ["planning", "review", "respond"])
async def test_group_from_native_home_gets_frozen_skills_in_its_own_home(tmp_path, monkeypatch, role):
    native = tmp_path / "native"
    native.mkdir()
    (native / "config.yaml").write_text("{}\n")
    (native / "state.db").write_bytes(b"original-private-history")
    allowed = package(native / "skills", "allowed")
    blocked = package(native / "skills", "blocked")
    owner = HermesDriver(hermes_root=native)
    owner._capabilities = BackendCapabilities()
    binding = AgentBinding.create(project="group-test", backend="hermes",
                                  runtime_config={"group_execution": role})
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    protocol = SessionProjection(options=CreateSessionOptions(metadata={"mcpServers": [{"name": "synthetic"}]}),
                                 summary={"mcpServers": ["synthetic"]})
    delegate = SimpleNamespace(session_options_for=AsyncMock(return_value=protocol))

    def create_delegate(_, current):
        home = owner.projection_scope(current)
        # Startup must see a finished policy, never the original skills symlink.
        assert (home / ".kaus-skills.json").is_file()
        assert not (home / "skills").is_symlink()
        return delegate

    monkeypatch.setattr(isolated_runtime, "create_driver", create_delegate)
    result = await owner.session_options_for(conversation, selection(binding, [asset(allowed)]))
    home = owner.projection_scope(binding)
    assert home != native and project_scope.enabled(binding)
    assert owner.requires_project_projection(binding)
    assert result.options.metadata["mcpServers"] == protocol.options.metadata["mcpServers"]
    assert Path(result.options.metadata["kaus_skill_scope"]).is_relative_to(home)
    assert result.summary["mcpServers"] == ["synthetic"]
    assert "default" in result.summary["projectSettings"]
    files = list((home / "skills").rglob("SKILL.md"))
    assert len(files) == 1 and "name: allowed" in files[0].read_text()
    assert json.loads((home / ".kaus-scope.json").read_text())["skillPolicyRequired"]
    assert not (home / "state.db").exists()
    assert blocked.is_file() and (native / "state.db").read_bytes() == b"original-private-history"


@pytest.mark.parametrize('group', [False, True])
async def test_missing_selected_skill_stops_startup_instead_of_partial_policy(tmp_path, monkeypatch, group):
    native = tmp_path / 'native'; native.mkdir()
    (native / 'config.yaml').write_text('{}\n')
    owner = HermesDriver(hermes_root=native)
    binding = AgentBinding.create(project='incomplete', backend='hermes', runtime_config={
        'project_execution': 'isolated', **({'group_execution': 'planning'} if group else {})})
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='synthetic')
    missing = asset(package(native / 'skills', 'missing'))
    Path(missing['sourcePath']).unlink()
    monkeypatch.setattr(isolated_runtime, 'create_driver', lambda *_: pytest.fail('incomplete policy started'))
    with pytest.raises(UnsupportedCapabilityError, match='技能未完整应用'):
        await owner.session_options_for(conversation, selection(binding, [missing]))
    assert not (owner.projection_scope(binding) / '.kaus-skill-sessions').exists()



async def test_group_projection_failure_never_reaches_protocol(tmp_path, monkeypatch):
    owner = HermesDriver(hermes_root=tmp_path)
    binding = AgentBinding.create(project="group-test", backend="hermes",
                                  runtime_config={"group_execution": "planning"})
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    materialize = AsyncMock(side_effect=OSError("synthetic disk failure"))
    monkeypatch.setattr(owner, "materialize_project_capabilities", materialize)
    def unexpected(*_):
        pytest.fail("protocol startup after failed policy")
    monkeypatch.setattr(isolated_runtime, "create_driver", unexpected)
    with pytest.raises(OSError, match="synthetic"):
        await owner.session_options_for(conversation, selection(binding, []))
    assert owner.requires_project_projection(binding)
