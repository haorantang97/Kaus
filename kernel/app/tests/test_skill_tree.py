"""Configuration acceptance only; native loading/isolation acceptance is deferred."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.api.api_errors import ApiError
from app.capabilities.effective import resolve_effective_for_project
from app.capabilities.models import ProjectCapability
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import Project
from drivers.skill_inventory import NativeSkillRoot, configured_roots, discover_skills


@pytest.fixture
def tree(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    for slug, parent in (("default", None), ("work", "default"), ("leaf", "work"), ("other", "default")):
        asyncio.run(uow.repositories.projects.save(Project.create(
            slug=slug, display_name=slug, parent_project_id=f"project:{parent}" if parent else None)))
    roots = []
    for agent in ("alpha", "beta"):
        base = tmp_path / agent / "skills"
        for skill in ("review", "evolve"):
            path = base / skill / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {skill}\ndescription: {agent} {skill}\n---\nSynthetic skill\n")
        roots.append(NativeSkillRoot(base, agent, agent.title(), "project:default"))
    generic = ProjectCapability.create(project_id="project:default", capability_type="skills",
                                       capability_id="writing", config={"name": "Writing", "instruction": "Keep sources"}, version="v1")
    asyncio.run(uow.repositories.capabilities.save(generic))
    svc = SkillTreeService(uow.database, discover=lambda: discover_skills(roots))
    yield svc, uow, roots
    uow.close()


def one(snapshot, agent="alpha", name="review"):
    return next(s["id"] for s in snapshot["skills"] if s["agentKey"] == agent and s["name"] == name)


def act(svc, snapshot, action, skill_id=None, **kwargs):
    return svc.act({"revision": snapshot["revision"], "action": action, "skillId": skill_id, **kwargs})


def entries(snapshot, project, skill):
    return [e for e in snapshot["entries"] if e["projectId"] == f"project:{project}" and e["skillId"] == skill]


def test_native_names_ownership_and_resolver_are_shared(tree):
    svc, uow, _ = tree
    view = svc.snapshot()
    alpha, beta = one(view), one(view, "beta")
    assert alpha != beta
    assert entries(view, "leaf", alpha)[0]["sourceProjectId"] == "project:default"
    resolved = asyncio.run(resolve_effective_for_project(uow.repositories, "project:leaf", backend_key="alpha"))
    assert {e.capability_type for e in resolved.entries} == {"skills", "alpha:skills"}
    for row in svc.db.query_all("SELECT config_json FROM project_capabilities WHERE capability_type='alpha:skills'"):
        assert json.loads(row[0]) == {}  # Source paths never reach runtime config.


def test_inventory_context_discovers_project_and_binding_workspaces(tree, tmp_path):
    from app.projects.models import AgentBinding, Backend
    svc, uow, _ = tree
    project = asyncio.run(uow.repositories.projects.get("project:work"))
    project = asyncio.run(uow.repositories.projects.save(project.evolve(
        workspace_root=str(tmp_path / "project-workspace"))))
    asyncio.run(uow.repositories.backends.save(Backend.create(
        key="codex", display_name="Codex", driver_kind="acp")))
    asyncio.run(uow.repositories.bindings.save(AgentBinding.create(
        project=project, backend="codex",
        runtime_config={"workspace_root": str(tmp_path / "binding-workspace")})))
    for workspace, name in (("project-workspace", "project-skill"),
                            ("binding-workspace", "binding-skill")):
        path = tmp_path / workspace / ".agents/skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\nSynthetic workspace skill")
    svc.discover = lambda: discover_skills(configured_roots(
        None, *svc.inventory_context(), home=tmp_path, env={}))
    view = svc.snapshot()
    ids = {s["id"] for s in view["skills"] if s["name"] in {"project-skill", "binding-skill"}}
    assert len(ids) == 2
    assert all(e["sourceProjectId"] == project.id for e in view["entries"] if e["skillId"] in ids)


def test_move_persists_across_refresh_and_restart_without_native_writes(tree):
    svc, uow, roots = tree
    paths = [path for root in roots for path in root.path.rglob("*") if path.is_file()]
    before_files = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    view = svc.snapshot()
    skill = one(view)
    moved = act(svc, view, "move", skill, sourceProjectId="project:default", targetProjectId="project:work")
    assert not entries(moved, "default", skill)
    assert not entries(moved, "other", skill)
    assert entries(moved, "leaf", skill)[0]["inherited"]
    restarted = SkillTreeService(uow.database, discover=svc.discover).snapshot()
    assert restarted["revision"] == moved["revision"]
    assert not entries(restarted, "default", skill)
    assert before_files == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}


def test_copy_remove_undo_preserves_generic_payload(tree):
    svc, uow, _ = tree
    view = svc.snapshot()
    skill = one(view, None, "Writing")
    copied = act(svc, view, "copy", skill, sourceProjectId="project:default", targetProjectId="project:work")
    rows = asyncio.run(uow.repositories.capabilities.list_for_project("project:work"))
    assert rows[0].config == {"name": "Writing", "instruction": "Keep sources"}
    assert rows[0].version == "v1"
    removed = act(svc, copied, "remove", skill, targetProjectId="project:work")
    assert entries(removed, "work", skill)[0]["sourceProjectId"] == "project:default"
    restored = act(svc, removed, "undo", undoToken=removed["undoToken"])
    assert restored["revision"] == copied["revision"]


def test_inherited_block_restore_and_descendant_scope(tree):
    svc, _, _ = tree
    view = svc.snapshot()
    skill = one(view)
    blocked = act(svc, view, "block", skill, targetProjectId="project:work")
    assert entries(blocked, "leaf", skill)[0]["blocked"]
    assert entries(blocked, "leaf", skill)[0]["sourceProjectId"] == "project:work"
    assert not entries(blocked, "other", skill)[0]["blocked"]
    restored = act(svc, blocked, "restore", skill, targetProjectId="project:work")
    assert not entries(restored, "leaf", skill)[0]["blocked"]
    local_blocked = act(svc, restored, "block", skill, targetProjectId="project:default")
    assert entries(local_blocked, "leaf", skill)[0]["blocked"]
    local_restored = act(svc, local_blocked, "restore", skill, targetProjectId="project:default")
    assert not entries(local_restored, "leaf", skill)[0]["blocked"]


def test_local_block_restore_preserves_payload_and_undo(tree):
    svc, uow, _ = tree
    view = svc.snapshot()
    skill = one(view, None, "Writing")
    original = asyncio.run(uow.repositories.capabilities.list_for_project("project:default"))
    blocked = act(svc, view, "block", skill, targetProjectId="project:default")
    restored = act(svc, blocked, "restore", skill, targetProjectId="project:default")
    assert asyncio.run(uow.repositories.capabilities.list_for_project("project:default")) == original
    reblocked = act(svc, restored, "undo", undoToken=restored["undoToken"])
    assert reblocked["revision"] == blocked["revision"]
    again = act(svc, reblocked, "restore", skill, targetProjectId="project:default")
    assert entries(again, "leaf", skill)[0]["inherited"]


def test_reorder_and_undo_keep_full_order(tree):
    svc, _, _ = tree
    view = svc.snapshot()
    review, evolve = one(view), one(view, name="evolve")
    ordered = act(svc, view, "reorder", review, targetProjectId="project:work", beforeSkillId=evolve)
    assert entries(ordered, "work", review)[0]["order"] < entries(ordered, "work", evolve)[0]["order"]
    appended = act(svc, ordered, "reorder", review, targetProjectId="project:work")
    assert entries(appended, "work", review)[0]["order"] > entries(appended, "work", evolve)[0]["order"]
    undone = act(svc, appended, "undo", undoToken=appended["undoToken"])
    assert undone["revision"] == ordered["revision"]


def test_conflicts_and_invalid_group_are_atomic(tree):
    svc, uow, _ = tree
    view = svc.snapshot()
    skill = one(view)
    with pytest.raises(ApiError, match="同一分组"):
        act(svc, view, "move", skill, sourceProjectId="project:default", targetProjectId="project:work", beforeSkillId=one(view, "beta"))
    assert svc.snapshot()["revision"] == view["revision"]
    moved = act(svc, view, "move", skill, sourceProjectId="project:default", targetProjectId="project:work")
    with pytest.raises(ApiError, match="刷新"):
        act(svc, view, "remove", skill, targetProjectId="project:work")
    # Another capability writer invalidates the undo revision too.
    asyncio.run(uow.repositories.capabilities.save(ProjectCapability.create(
        project_id="project:work", capability_type="skills", capability_id="new", config={"name": "New"})))
    current = svc.snapshot()
    with pytest.raises(ApiError, match="无法撤销"):
        act(svc, current, "undo", undoToken=moved["undoToken"])


def test_removed_native_skill_is_not_reimported(tree):
    svc, _, _ = tree
    view = svc.snapshot()
    skill = one(view)
    removed = act(svc, view, "remove", skill, targetProjectId="project:default")
    assert not [e for e in removed["entries"] if e["skillId"] == skill]
    assert svc.snapshot()["revision"] == removed["revision"]


def test_symlink_inventory_keeps_origin_and_never_creates_folders(tmp_path):
    real = tmp_path / "native" / "review"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: Review\n---\nBody")
    root = tmp_path / "linked"
    root.mkdir()
    (root / "review").symlink_to(real, target_is_directory=True)
    roots = [NativeSkillRoot(real.parent, "alpha", "Alpha", "project:default"),
             NativeSkillRoot(root, "alpha", "Alpha", "project:work")]
    assert len(discover_skills(roots)) == 1
    assert discover_skills(roots)[0]["defaultProjectId"] == "project:default"
    missing = tmp_path / "missing"
    assert discover_skills([NativeSkillRoot(missing, "alpha", "Alpha", "project:default")]) == []
    assert not missing.exists()


def test_category_symlinks_cycles_and_sibling_allocations(tree, tmp_path):
    svc, _, roots = tree
    category = tmp_path / "categories"
    category.mkdir()
    (category / "linked-category").symlink_to(roots[0].path, target_is_directory=True)
    (category / "loop").symlink_to(category, target_is_directory=True)
    discovered = discover_skills([
        NativeSkillRoot(category, "alpha", "Alpha", "project:work"),
        NativeSkillRoot(roots[0].path, "alpha", "Alpha", "project:leaf"),
        NativeSkillRoot(roots[0].path, "alpha", "Alpha", "project:other"),
    ])
    assert len(discovered) == 2
    svc.discover = lambda: discovered
    view = svc.snapshot()
    skill = one(view)
    assert not entries(view, "default", skill)
    assigned = [a["projectId"] for a in view["assignments"] if a["skillId"] == skill]
    assert set(assigned) == {"project:work", "project:other"}
    assert entries(view, "leaf", skill)[0]["inherited"]


def test_nested_native_packages_skip_support_and_archive(tmp_path):
    for relative in ("bundle", "bundle/modules/child", "bundle/.agents/skills/child", "bundle/references/example", ".archive/old"):
        path = tmp_path / relative / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: " + relative + "\n---\n")
    roots = [NativeSkillRoot(tmp_path, "alpha", "Alpha", "project:default", nested_skills=True)]
    assert {s["name"] for s in discover_skills(roots)} == {"bundle", "bundle/modules/child", "bundle/.agents/skills/child"}


def test_api_auth_contract_rejects_ownership_mutation(tree):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.session_auth import SessionAuthPolicy
    from app.api.skill_tree import build_skill_tree_router
    svc, _, _ = tree
    app = FastAPI()
    app.include_router(build_skill_tree_router(svc, auth_policy=SessionAuthPolicy(lambda: "skill-tree-test")))
    with TestClient(app) as client:
        assert client.get("/api/skill-tree").status_code == 401
        headers = {"Authorization": "Bearer skill-tree-test"}
        response = client.get("/api/skill-tree", headers=headers)
        assert response.status_code == 200
        view = response.json()
        bad = client.post("/api/skill-tree/actions", headers=headers, json={"revision": view["revision"],
                          "action": "move", "skillId": one(view), "sourceProjectId": "project:default",
                          "targetProjectId": "project:work", "agentKey": "beta"})
        assert bad.status_code == 422
        moved = client.post("/api/skill-tree/actions", headers=headers, json={"revision": view["revision"],
                            "action": "move", "skillId": one(view), "sourceProjectId": "project:default",
                            "targetProjectId": "project:work"})
        assert moved.status_code == 200
        assert moved.json()["undoToken"]


def test_generated_skill_versions_remain_pending_until_explicit_nonconflicting_assignment(tree, tmp_path):
    svc, uow, roots = tree
    initial = svc.snapshot()
    original = one(initial)
    for name, conversation in (('version-a', 'conversation:a'), ('version-b', 'conversation:b')):
        path = tmp_path / name / 'SKILL.md'
        path.parent.mkdir()
        path.write_text('---\nname: review\n---\nGenerated version')
        roots.append(NativeSkillRoot(path.parent, 'alpha', 'Alpha', 'project:work',
            generated=True, source_conversation_id=conversation, replaces_id=original))
    view = svc.snapshot()
    generated = [item for item in view['skills'] if item.get('generated')]
    assert len(generated) == 2
    assert all(item['pendingAssignment'] and item['originProjectId'] == 'project:work'
               and item['replacesId'] == original for item in generated)
    assert not any(e['skillId'] in {s['id'] for s in generated} for e in view['entries'])
    replacement = generated[0]['id']
    with pytest.raises(ApiError) as failure:
        act(svc, view, 'assign', replacement, targetProjectId='project:work')
    assert failure.value.code == 'skill_name_conflict'
    view = act(svc, view, 'block', original, targetProjectId='project:work')
    view = act(svc, view, 'assign', replacement, targetProjectId='project:work')
    assert not next(s for s in view['skills'] if s['id'] == replacement)['pendingAssignment']
    assert entries(view, 'leaf', replacement)[0]['inherited']
    refreshed = svc.snapshot()
    assert refreshed['revision'] == view['revision']
    with pytest.raises(ApiError):
        act(svc, refreshed, 'assign', generated[1]['id'], targetProjectId='project:work')
    restored = act(svc, refreshed, 'undo', undoToken=view['undoToken'])
    assert next(s for s in restored['skills'] if s['id'] == replacement)['pendingAssignment']
    assert not entries(restored, 'work', replacement)
