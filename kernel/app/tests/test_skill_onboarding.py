"""First-connect invitations reuse the real tree and never touch native files."""
import asyncio

from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import Project
from drivers.skill_inventory import NativeSkillRoot, discover_skills


def setup_tree(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "app.db")
    for slug, parent in (("default", None), ("work", "project:default")):
        asyncio.run(uow.repositories.projects.save(Project.create(
            slug=slug, display_name=slug, parent_project_id=parent)))
    root = NativeSkillRoot(tmp_path / "alpha", "alpha", "Alpha", "project:default")
    return uow, root


def install(root):
    path = root.path / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: Review\n---\nSynthetic skill\n")
    return path


def test_ordinary_launch_does_not_scan_or_enroll_existing_skills(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        svc = SkillTreeService(uow.database, discover=lambda: (_ for _ in ()).throw(AssertionError("scan")))
        assert svc.pending_onboarding() == {"items": [], "waiting": False}
        install(root)
        svc.discover = lambda: discover_skills([root])
        svc.snapshot()
        assert svc.pending_onboarding() == {"items": [], "waiting": False}
    finally:
        uow.close()


def test_retired_scope_keeps_invitation_and_skills_without_reopening_guide(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        install(root)
        active = SkillTreeService(uow.database, discover=lambda: discover_skills([root]))
        active.begin_onboarding('alpha', 'Alpha')
        invitation = active.pending_onboarding()['items'][0]
        snapshot = active.snapshot()
        retired = SkillTreeService(uow.database, discover=lambda: (_ for _ in ()).throw(AssertionError('scan')),
                                   onboarding_enabled=lambda _: False)
        retired.begin_onboarding('beta', 'Beta')
        assert retired.pending_onboarding() == {'items': [], 'waiting': False}
        assert active.pending_onboarding()['items'][0]['id'] == invitation['id']
        assert active.snapshot()['assignments'] == snapshot['assignments']
    finally:
        uow.close()


def test_first_connection_waits_for_inventory_and_seeds_root_only_once(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        svc = SkillTreeService(uow.database, discover=lambda: discover_skills([root]))
        svc.begin_onboarding("alpha", "Alpha")
        assert svc.pending_onboarding() == {"items": [], "waiting": True}
        path = install(root)
        original = path.read_bytes(), path.stat().st_mtime_ns
        invitation = svc.pending_onboarding()["items"][0]
        assert invitation["rootProjectId"] == "project:default"
        assert invitation["skillCount"] == 1
        snapshot = svc.snapshot()
        skill_id = snapshot["skills"][0]["id"]
        assert snapshot["assignments"] == [{"projectId": "project:default", "skillId": skill_id, "mode": "local", "order": 100000}]
        svc.act({"revision": snapshot["revision"], "action": "move", "skillId": skill_id,
                 "sourceProjectId": "project:default", "targetProjectId": "project:work"})
        svc.begin_onboarding("alpha", "Alpha")
        assert svc.pending_onboarding()["items"][0]["id"] == invitation["id"]
        assert svc.snapshot()["assignments"][0]["projectId"] == "project:work"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    finally:
        uow.close()


def test_skip_survives_restart_inventory_changes_and_reconnection(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        install(root)
        svc = SkillTreeService(uow.database, discover=lambda: discover_skills([root]))
        svc.begin_onboarding("alpha", "Alpha")
        invitation = svc.pending_onboarding()["items"][0]
        assert svc.finish_onboarding(invitation["id"], "skipped") == {"completed": True}
        assert svc.finish_onboarding(invitation["id"], "done") == {"completed": True}
        restarted = SkillTreeService(uow.database, discover=lambda: discover_skills([root]))
        restarted.begin_onboarding("alpha", "Renamed Alpha")
        assert restarted.pending_onboarding() == {"items": [], "waiting": False}
        assert restarted.snapshot()["assignments"][0]["projectId"] == "project:default"
        restarted.begin_onboarding("beta", "Beta")
        assert restarted.pending_onboarding()["waiting"] is True
    finally:
        uow.close()


def test_invitation_waits_for_running_inventory_even_when_some_skills_exist(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        install(root)
        ready = False
        svc = SkillTreeService(uow.database, discover=lambda: discover_skills([root]),
                               inventory_ready=lambda _: ready)
        svc.begin_onboarding("alpha", "Alpha")
        assert svc.pending_onboarding() == {"items": [], "waiting": True}
        ready = True
        assert svc.pending_onboarding()["items"][0]["skillCount"] == 1
    finally:
        uow.close()


# --- Native MCP capabilities and the connect-flow step (2026-11-03) ---------

import json

from app.capabilities.effective import resolve_effective_for_binding, resolve_effective_for_project
from app.projects.models import AgentBinding
from drivers.mcp_inventory import discover_mcp, preset_sources

SECRET = "sk-live-0123456789abcdefABCDEF0123"


def mcp_sources(tmp_path):
    return preset_sources("codex", agent_key="codex", agent_label="Codex",
                          project_id="project:default", home=tmp_path / "home", env={})


def write_codex_mcp(tmp_path, names=("fs",)):
    path = tmp_path / "home" / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'[mcp_servers.{name}]\ncommand = "uvx"\nenv = {{ TOKEN = "{SECRET}" }}\n' for name in names))
    return path


def test_native_mcp_seeds_root_agent_group_and_moves_survive_rediscovery(tmp_path):
    uow, _ = setup_tree(tmp_path)
    try:
        write_codex_mcp(tmp_path)
        svc = SkillTreeService(uow.database, discover=lambda: discover_mcp(mcp_sources(tmp_path)))
        snapshot = svc.snapshot()
        (record,) = snapshot["skills"]
        assert record["kind"] == "mcp" and record["agentKey"] == "codex" and record["name"] == "fs"
        assert snapshot["assignments"] == [{"projectId": "project:default", "skillId": record["id"], "mode": "local", "order": 100000}]
        moved = svc.act({"revision": snapshot["revision"], "action": "move", "skillId": record["id"],
                         "sourceProjectId": "project:default", "targetProjectId": "project:work"})
        assert [a["projectId"] for a in moved["assignments"]] == ["project:work"]
        # Arranging MCP never switches the Agent's Skills to a managed selection.
        managed = uow.database.query_all("SELECT value_json FROM app_settings WHERE key = 'skill_tree.managed_projects.v1'")
        assert [json.loads(row[0]) for row in managed] in ([], [{}])
        path = write_codex_mcp(tmp_path, ("fs", "web"))
        original = path.read_bytes(), path.stat().st_mtime_ns
        again = svc.snapshot()
        placed = {next(s["name"] for s in again["skills"] if s["id"] == a["skillId"]): a["projectId"] for a in again["assignments"]}
        assert placed == {"fs": "project:work", "web": "project:default"}
        stored = uow.database.query_all("SELECT value_json FROM app_settings")
        assert SECRET not in json.dumps([dict(row) for row in stored])
        assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    finally:
        uow.close()


def test_native_mcp_is_never_projected_to_the_binding(tmp_path):
    uow, _ = setup_tree(tmp_path)
    try:
        write_codex_mcp(tmp_path)
        svc = SkillTreeService(uow.database, discover=lambda: discover_mcp(mcp_sources(tmp_path)))
        svc.snapshot()
        repos = uow.repositories
        project = asyncio.run(repos.projects.get("project:work"))
        binding = AgentBinding.create(project=project, backend="codex")
        asyncio.run(repos.bindings.save(binding))
        visible = asyncio.run(resolve_effective_for_project(repos, project.id, backend_key="codex"))
        assert [e.capability_type for e in visible.entries] == ["codex:mcp"]
        projected = asyncio.run(resolve_effective_for_binding(repos, binding))
        assert not [e for e in projected.entries if e.type_ref.name == "mcp"]
        # 2026-11-09：记录挪到 native_mcp，驱动据此算「自带但未分配」的排除名单。
        assert [e.capability_type for e in projected.native_mcp] == ["codex:mcp"]
    finally:
        uow.close()


def test_connect_step_status_waits_counts_and_finishes_once(tmp_path):
    uow, root = setup_tree(tmp_path)
    try:
        ready = False
        svc = SkillTreeService(uow.database, inventory_ready=lambda _: ready,
                               discover=lambda: [*discover_skills([root]), *discover_mcp(mcp_sources(tmp_path))])
        invitation = svc.begin_onboarding("codex", "Codex")
        assert invitation["agentKey"] == "codex"
        assert svc.begin_onboarding("codex", "Codex")["id"] == invitation["id"]
        status = svc.onboarding_status(invitation["id"])
        assert (status["status"], status["ready"], status["skillCount"], status["mcpCount"]) == ("pending", False, 0, 0)
        ready = True
        assert (svc.onboarding_status(invitation["id"])["mcpCount"]) == 0  # nothing found: the UI skips silently
        write_codex_mcp(tmp_path)
        status = svc.onboarding_status(invitation["id"])
        assert (status["ready"], status["mcpCount"], status["rootProjectId"]) == (True, 1, "project:default")
        svc.finish_onboarding(invitation["id"], "done")
        assert svc.onboarding_status(invitation["id"])["status"] == "done"
        assert svc.begin_onboarding("codex", "Codex") is None
        assert svc.pending_onboarding() == {"items": [], "waiting": False}
    finally:
        uow.close()


def test_skipped_connect_step_keeps_root_defaults(tmp_path):
    uow, _ = setup_tree(tmp_path)
    try:
        write_codex_mcp(tmp_path)
        svc = SkillTreeService(uow.database, discover=lambda: discover_mcp(mcp_sources(tmp_path)))
        invitation = svc.begin_onboarding("codex", "Codex")
        svc.finish_onboarding(invitation["id"], "skipped")
        assert svc.onboarding_status(invitation["id"])["status"] == "skipped"
        assert [a["projectId"] for a in svc.snapshot()["assignments"]] == ["project:default"]
        retired = SkillTreeService(uow.database, onboarding_enabled=lambda _: False)
        assert retired.begin_onboarding("gamma", "Gamma") is None
    finally:
        uow.close()
