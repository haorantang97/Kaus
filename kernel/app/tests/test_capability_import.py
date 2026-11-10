"""Explicit capability import (docs/product/capability-import-20261110.md): temp dirs and synthetic Agents only."""
import asyncio

from app.capabilities.capability_import import CapabilityImports
from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.acp.mcp_projection import project_mcp_servers
from drivers.capability_binding import annotate
from drivers.mcp_inventory import discover_mcp, preset_sources
from drivers.skill_inventory import NativeSkillRoot, discover_skills

SECRET = "sk-test-0123456789abcdefABCDEF"


def world(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "app.db")
    for slug, parent in (("default", None), ("work", "project:default")):
        asyncio.run(uow.repositories.projects.save(Project.create(slug=slug, display_name=slug, parent_project_id=parent)))
    for key in ("alpha", "beta", "codex"):
        asyncio.run(uow.repositories.backends.save(Backend.create(key=key, display_name=key, driver_kind="acp")))
    return uow


def skill(folder, name, body="Synthetic skill"):
    path = folder / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} helper\n---\n{body}\n")
    return path


def roots(tmp_path):
    return [NativeSkillRoot(tmp_path / "alpha", "alpha", "Alpha", "project:default"),
            NativeSkillRoot(tmp_path / "shared", "alpha", "Alpha", "project:default"),
            NativeSkillRoot(tmp_path / "shared", "beta", "Beta", "project:default")]


def service(uow, tmp_path, extra=lambda: []):
    tree = SkillTreeService(uow.database, discover=lambda: annotate([*discover_skills(roots(tmp_path)), *extra()]))
    return tree, CapabilityImports(tree, tmp_path / "library")


def ids_by_name(imports, agent):
    return {item["name"]: item for item in imports.candidates(agent)["items"]}


def test_bound_capabilities_are_kept_and_never_offered(tmp_path):
    uow = world(tmp_path)
    try:
        skill(tmp_path / "alpha", "pdf")
        skill(tmp_path / "alpha", "alpha-agent")  # not a known Agent name: an ordinary Skill
        tree, imports = service(uow, tmp_path)
        tree.begin_onboarding("alpha", "Alpha")
        catalog_ids = ids_by_name(imports, "alpha")
        assert {name: item["state"] for name, item in catalog_ids.items()} == {"pdf": "available", "alpha-agent": "available"}
        # A record the driver marked bound is greyed and kept for its Agent without asking.
        tree.discover = lambda: [{**r, **({"bound": True, "boundReason": "agent_self"} if r["name"] == "alpha-agent" else {})}
                                 for r in annotate(discover_skills(roots(tmp_path)))]
        items = ids_by_name(imports, "alpha")
        assert items["alpha-agent"]["state"] == "bound" and items["alpha-agent"]["boundReason"] == "agent_self"
        snapshot = imports.skip("alpha") and tree.snapshot()
        assigned = {a["skillId"] for a in snapshot["assignments"]}
        assert assigned == {items["alpha-agent"]["id"]}  # blank slate except what is bound
    finally:
        uow.close()


def test_shared_folder_skill_is_merged_and_usable_by_every_agent_in_the_project(tmp_path):
    uow = world(tmp_path)
    try:
        skill(tmp_path / "shared", "notes")
        tree, imports = service(uow, tmp_path)
        tree.begin_onboarding("alpha", "Alpha")
        alpha_notes = ids_by_name(imports, "alpha")["notes"]["id"]
        imports.import_selected("alpha", [alpha_notes])
        tree.begin_onboarding("beta", "Beta")
        beta = ids_by_name(imports, "beta")["notes"]
        assert beta["state"] == "inTree"
        snapshot = imports.import_selected("beta", [beta["id"]])
        assert [s["id"] for s in snapshot["skills"] if s["name"] == "notes"] == [alpha_notes]  # one entry

        def assets(backend):
            project = asyncio.run(uow.repositories.projects.get("project:work"))
            binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend=backend)))
            effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
            assert effective.tree_managed
            return [(a["name"], a.get("shared", False)) for a in effective.get("skill-selection", "default").config["assets"]]
        assert assets("alpha") == [("notes", False)]
        assert assets("beta") == [("notes", True)]  # Alpha's import, from the library copy
    finally:
        uow.close()


def test_first_import_for_an_earlier_agent_drops_what_is_not_selected(tmp_path):
    uow = world(tmp_path)
    try:
        skill(tmp_path / "alpha", "pdf")
        skill(tmp_path / "alpha", "music")
        tree, imports = service(uow, tmp_path)
        assert len(tree.snapshot()["assignments"]) == 2  # arranged before the rule: seeded at root
        items = ids_by_name(imports, "alpha")
        assert items["pdf"]["assigned"] and items["music"]["assigned"]  # preselected in the list
        snapshot = imports.import_selected("alpha", [items["pdf"]["id"]])
        assert {a["skillId"] for a in snapshot["assignments"]} == {items["pdf"]["id"]}
        assert ids_by_name(imports, "alpha")["music"]["state"] == "available"  # still importable later
    finally:
        uow.close()


def test_changes_are_reported_per_agent_and_cleared_when_seen(tmp_path):
    uow = world(tmp_path)
    try:
        origin = skill(tmp_path / "alpha", "pdf")
        tree, imports = service(uow, tmp_path)
        tree.begin_onboarding("alpha", "Alpha")
        pdf = ids_by_name(imports, "alpha")["pdf"]["id"]
        imports.import_selected("alpha", [pdf])
        assert imports.changes() == {"agents": [], "unseen": False}
        skill(tmp_path / "alpha", "music")
        origin.write_text("---\nname: pdf\n---\nNew version\n")
        report = imports.changes()
        alpha = report["agents"][0]
        assert report["unseen"] and [x["name"] for x in alpha["added"]] == ["music"]
        assert [x["name"] for x in alpha["updated"]] == ["pdf"]
        imports.mark_seen()
        assert imports.changes()["unseen"] is False and imports.changes()["agents"]  # still listed, no marker
        imports.ignore("alpha", [alpha["added"][0]["id"]])
        imports.sync(pdf)
        assert imports.changes()["agents"] == []
        assert (tmp_path / "library/skills" / pdf / "SKILL.md").read_text().endswith("New version\n")
        origin.unlink()
        removed = imports.changes()["agents"][0]["removed"]
        assert [x["name"] for x in removed] == ["pdf"]
        assert tree.snapshot()["skills"][0]["available"] is True  # the library copy keeps working
    finally:
        uow.close()


def test_imported_native_mcp_reaches_other_agents_with_its_secret_only_in_memory(tmp_path):
    uow = world(tmp_path)
    try:
        config = tmp_path / "home/.codex/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'[mcp_servers.fs]\ncommand = "uvx"\nargs = ["fs-mcp"]\nenv = {{ TOKEN = "{SECRET}" }}\n')
        sources = preset_sources("codex", agent_key="codex", agent_label="Codex", project_id="project:default",
                                 home=tmp_path / "home", env={})
        tree, imports = service(uow, tmp_path, extra=lambda: discover_mcp(sources))
        tree.begin_onboarding("codex", "Codex")
        fs = ids_by_name(imports, "codex")["fs"]
        assert fs["kind"] == "mcp" and fs["state"] == "available"
        imports.import_selected("codex", [fs["id"]])
        project = asyncio.run(uow.repositories.projects.get("project:work"))
        binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend="alpha")))
        effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
        entry = next(e for e in effective.entries if e.capability_type == "mcp")
        assert SECRET not in str(entry.config) and entry.config["value"]["nativeRef"]["name"] == "fs"
        servers = project_mcp_servers(effective).servers
        assert servers == ({"name": "fs", "command": "uvx", "args": ["fs-mcp"], "env": [{"name": "TOKEN", "value": SECRET}]},)
        own = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend="codex")))
        own_effective = asyncio.run(resolve_effective_for_binding(uow.repositories, own))
        assert not own_effective.by_type("mcp") and [e.capability_id for e in own_effective.native_mcp] == [fs["id"]]
    finally:
        uow.close()


def test_import_entry_lists_agents_with_what_is_still_importable(tmp_path):
    uow = world(tmp_path)
    try:
        skill(tmp_path / "alpha", "pdf")
        skill(tmp_path / "shared", "notes")
        tree, imports = service(uow, tmp_path)
        tree.begin_onboarding("alpha", "Alpha")
        imports.skip("alpha")
        rows = {r["agentKey"]: r for r in imports.agents()["agents"]}
        assert rows["alpha"]["status"] == "skipped" and rows["alpha"]["available"] == 2
        assert rows["beta"]["status"] is None  # arranged before the rule; can still be imported
    finally:
        uow.close()


def test_capabilities_the_native_agent_cannot_isolate_are_locked_in_the_tree(tmp_path):
    import pytest
    from app.api.api_errors import ApiError
    uow = world(tmp_path)
    try:
        skill(tmp_path / "alpha", "core")
        tree = SkillTreeService(uow.database, discover=lambda: annotate(discover_skills(roots(tmp_path)), supported={"alpha": ("mcp",)}))
        view = tree.snapshot()
        core = next(s for s in view["skills"] if s["name"] == "core")
        assert core["boundReason"] == "not_isolable"
        with pytest.raises(ApiError) as failure:
            tree.act({"revision": view["revision"], "action": "block", "skillId": core["id"], "targetProjectId": "project:work"})
        assert failure.value.code == "capability_not_isolable"
    finally:
        uow.close()
