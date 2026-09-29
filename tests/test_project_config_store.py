from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "kernel"):
    sys.path.insert(0, str(path))

import project_config_store as store
import project_config_sync as sync
import domain_bootstrap
from app.persistence.memory import in_memory_repository_set
from app.projects.models import Project, AgentBinding
from app.capabilities.models import ProjectCapability


def host(tmp_path):
    state = tmp_path / "dashboard"
    state.mkdir()
    home = tmp_path / "native"
    home.mkdir()
    (home / "profiles").mkdir()
    (home / "config.yaml").write_text("model:\n  default: native-model\n")
    for name, value in {"hierarchy": {}, "labels": {"default": "X"}, "drafts": [],
                        "pinned": [], "killed": [], "skill_inherit_off": []}.items():
        (state / (name + ".json")).write_text(json.dumps(value))
    return SimpleNamespace(HERE=state, HERMES_HOME=home, PROFILES_DIR=home / "profiles",
        _profile_dir=lambda name: store.directory(state, name) if store.contains(state, name)
            else home if name == "default" else home / "profiles" / name)


def test_standalone_project_has_no_native_profile_or_automatic_engine(tmp_path):
    server = host(tmp_path)
    store.create(server.HERE, "design", description="test")
    (server.HERE / "hierarchy.json").write_text('{"design": "default"}')
    plan = domain_bootstrap.build_plan(state_root=server.HERE, hermes_home=server.HERMES_HOME)
    assert any(p["id"] == "project:design" for p in plan["projects"])
    assert all(b["project_id"] != "project:design" for b in plan["bindings"])
    assert all(row["ok"] for row in plan["meta"]["invariants"])
    assert not (server.PROFILES_DIR / "design").exists()
    assert store.names(server.HERE) == ["design"]


def test_clone_only_copies_configuration_and_archive_is_recoverable(tmp_path):
    server = host(tmp_path)
    original = store.create(server.HERE, "one")
    (original / "SOUL.md").write_text("Project identity")
    (original / "skills" / "skill.md").write_text("Example skill")
    (original / "state.db").write_bytes(b"private history")
    (original / ".env").write_text("FAKE_TEST_SECRET=example")
    (original / "config.yaml").write_text("model:\n  default: native-only\napprovals:\n  mode: manual\nmcp_servers:\n  example:\n    command: example-mcp\n")
    (original / "memories").mkdir()
    clone = store.create(server.HERE, "two", source=original)
    assert (clone / "SOUL.md").read_text() == "Project identity"
    assert (clone / "skills" / "skill.md").read_text() == "Example skill"
    assert not any((clone / name).exists() for name in ("state.db", ".env", "memories"))
    import yaml
    copied = yaml.safe_load((clone / "config.yaml").read_text())
    assert "model" not in copied
    assert copied["mcp_servers"]["example"]["command"] == "example-mcp"
    archived = store.archive(server.HERE, "two")
    assert archived.is_dir() and not clone.exists()


def test_tree_edits_keep_workspace_engine_selection_and_domain_capabilities(tmp_path):
    asyncio.run(check_tree_edits(tmp_path))


async def check_tree_edits(tmp_path):
    server = host(tmp_path)
    repos = in_memory_repository_set()
    before = sync.snapshot(server)
    root = await repos.projects.save(Project.create(slug="default", workspace_root=str(tmp_path)))
    binding = await repos.bindings.save(AgentBinding.create(project=root, backend="custom",
        runtime_config={"reasoning_effort": "high"}, default_model_id="selected"))
    capability = await repos.capabilities.save(ProjectCapability.create(project_id=root.id,
        capability_type="mcp", capability_id="user-owned", config={"command": "echo"}))
    store.create(server.HERE, "design")
    (server.HERE / "hierarchy.json").write_text('{"design": "default"}')
    (server.HERE / "labels.json").write_text('{"default": "Renamed", "design": "Design"}')
    await sync.apply_delta(repos, before, sync.snapshot(server))
    assert (await repos.projects.get(root.id)).workspace_root == str(tmp_path)
    assert (await repos.projects.get(root.id)).display_name == "Renamed"
    assert await repos.bindings.get(binding.id) == binding
    assert await repos.capabilities.get(capability.id) == capability
    assert await repos.bindings.list_for_project("project:design") == ()
    before = sync.snapshot(server)
    (store.directory(server.HERE, "design") / "SOUL.md").write_text("Project persona")
    await sync.apply_delta(repos, before, sync.snapshot(server))
    rows = await repos.capabilities.list_for_project("project:design")
    assert next(r for r in rows if r.capability_type == "instructions").config == {"body": "Project persona"}
    before = sync.snapshot(server)
    store.archive(server.HERE, "design")
    await sync.apply_delta(repos, before, sync.snapshot(server))
    assert (await repos.projects.get("project:design")).status == "disabled"


def test_create_endpoint_does_not_invoke_native_engine(tmp_path, monkeypatch):
    import capability_import
    os.environ.setdefault("HERMES_HOME", "/nonexistent/kaus-tests")
    os.environ.setdefault("DASH_FEATURE_SESSION_HOST_V1", "0")
    os.environ.setdefault("DASH_FEATURE_PROJECT_DOMAIN_V1", "0")
    server = capability_import.load_server_module()
    fixture = host(tmp_path)
    monkeypatch.setattr(server, "HERE", fixture.HERE)
    with capability_import.bound_server(server, state_root=fixture.HERE, hermes_home=fixture.HERMES_HOME):
        monkeypatch.setattr(server, "run_hermes", lambda *a, **k: pytest.fail("new project invoked Hermes"))
        result = server.api_create_empty(server.CreateEmptyReq(name="design", display="Design"))
        assert result["ok"]
        assert store.contains(fixture.HERE, "design")
        assert not (fixture.PROFILES_DIR / "design").exists()


def test_project_creation_is_visible_to_domain_immediately(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    fixture = host(tmp_path)
    repos = in_memory_repository_set()
    asyncio.run(repos.projects.save(Project.create(slug="default")))
    app = FastAPI()

    @app.post('/api/agent/create-empty')
    async def create():
        store.create(fixture.HERE, 'fresh')
        return {'ok': True}

    sync.install(app, fixture, repos)
    with TestClient(app) as client:
        response = client.post('/api/agent/create-empty')
    assert response.status_code == 200
    assert asyncio.run(repos.projects.get('project:fresh')) is not None
    assert asyncio.run(repos.bindings.list_for_project('project:fresh')) == ()


def test_standalone_project_cannot_be_dispatched_as_native_hermes(tmp_path, monkeypatch):
    import capability_import
    from fastapi import HTTPException
    server = capability_import.load_server_module()
    fixture = host(tmp_path)
    store.create(fixture.HERE, "design")
    with capability_import.bound_server(server, state_root=fixture.HERE, hermes_home=fixture.HERMES_HOME):
        monkeypatch.setattr(server, "_kanban", lambda *a, **k: pytest.fail("dispatched standalone project"))
        with pytest.raises(HTTPException, match="Hermes") as failure:
            server.api_kanban_create(server.KanbanCreateReq(title="Task", assignee="design"))
        assert failure.value.status_code == 409
        with pytest.raises(HTTPException, match="Hermes"):
            server.api_kanban_assign("task-1", server.KanbanAssignReq(assignee="design"))
        assert server.api_profile_subtasks("design")["supported"] is False
        config_path = store.directory(fixture.HERE, "design") / "config.yaml"
        before = config_path.read_bytes()
        assert server._materialize_config("design", {}, dry_run=False) is None
        assert config_path.read_bytes() == before
