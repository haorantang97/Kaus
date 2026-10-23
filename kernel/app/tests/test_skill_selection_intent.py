"""A catalogue is not an instruction to disable another engine's native skills."""
import asyncio
from types import SimpleNamespace

from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import Project
from drivers.skill_inventory import NativeSkillRoot, discover_skills


def test_last_native_skill_move_keeps_empty_scope_and_undo_restores_intent(tmp_path):
    async def check():
        uow = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        try:
            for slug, parent in (("root", None), ("child", "project:root")):
                await uow.repositories.projects.save(Project.create(slug=slug, display_name=slug, parent_project_id=parent))
            skill = tmp_path / "native/skills/review/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: Review\n---\nReview this.")
            service = SkillTreeService(uow.database, discover=lambda: discover_skills([
                NativeSkillRoot(skill.parent.parent, "alpha", "Alpha", "project:root")]))
            view = service.snapshot()
            async def selection(project, backend="alpha", config=None):
                effective = await resolve_effective_for_binding(uow.repositories, SimpleNamespace(
                    project_id=project, backend_key=backend, runtime_config=config or {}))
                return next((entry.config for entry in effective.entries if entry.capability_type == "skill-selection"), None)
            assert await selection("project:root", "beta") is None
            assert len((await selection("project:root"))["assets"]) == 1
            moved = service.act({"revision": view["revision"], "action": "move", "skillId": view["skills"][0]["id"],
                                  "sourceProjectId": "project:root", "targetProjectId": "project:child"})
            assert await selection("project:root") == {"mode": "managed", "assets": []}
            assert len((await selection("project:child"))["assets"]) == 1
            assert await selection("project:root", "beta") is None
            assert await selection("project:child", config={"skill_mode": "native"}) is None
            restored = service.act({"revision": moved["revision"], "action": "undo", "undoToken": moved["undoToken"]})
            assert restored["revision"] == view["revision"]
            assert await uow.repositories.settings.get("skill_tree.managed_projects.v1") == {}
            removed = service.act({"revision": restored["revision"], "action": "remove", "skillId": view["skills"][0]["id"],
                                    "targetProjectId": "project:root"})
            assert removed["entries"] == []
            assert await selection("project:child") == {"mode": "managed", "assets": []}
        finally:
            uow.close()
    asyncio.run(check())


def test_managed_empty_is_explicit_even_without_catalogue(tmp_path):
    async def check():
        uow = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        try:
            await uow.repositories.projects.save(Project.create(slug="root", display_name="Root"))
            binding = SimpleNamespace(project_id="project:root", backend_key="alpha", runtime_config={})
            assert not (await resolve_effective_for_binding(uow.repositories, binding)).entries
            binding.runtime_config = {"skill_mode": "managed"}
            assert (await resolve_effective_for_binding(uow.repositories, binding)).entries[0].config == {"mode": "managed", "assets": []}
        finally:
            uow.close()
    asyncio.run(check())


def test_declared_transport_alias_shares_skills_but_not_private_runtime_config(tmp_path):
    from app.capabilities.models import ProjectCapability
    async def check():
        uow = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        try:
            await uow.repositories.projects.save(Project.create(slug="root", display_name="Root"))
            await uow.repositories.settings.save("skill_tree.backend_scopes.v1", {"alpha-transport": "alpha"})
            await uow.repositories.settings.save("skill_tree.catalog.v1", {})
            await uow.repositories.capabilities.save(ProjectCapability.create(project_id="project:root", capability_type="alpha:skills", capability_id="review", config={"path": "/fixture/SKILL.md"}))
            await uow.repositories.capabilities.save(ProjectCapability.create(project_id="project:root", capability_type="alpha:runtime-config", capability_id="native", config={"private": "native-format"}))
            binding = SimpleNamespace(project_id="project:root", backend_key="alpha-transport", runtime_config={})
            effective = await resolve_effective_for_binding(uow.repositories, binding)
            assert {entry.capability_type for entry in effective.entries} == {"alpha:skills", "skill-selection"}
            selection = effective.get("skill-selection", "default")
            assert selection.config["scopeKey"] == "alpha" and len(selection.config["assets"]) == 1
        finally:
            uow.close()
    asyncio.run(check())
