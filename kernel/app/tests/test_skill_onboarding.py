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
