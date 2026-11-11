"""MCP 管理（docs/product/mcp-management-design-20261111.md）：temp dirs, synthetic Agents and a fake tool lister."""
import asyncio
import json

import pytest

from app.api.api_errors import ApiError
from app.capabilities.capability_import import CapabilityImports
from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.mcp_manager import McpManager
from app.capabilities.mcp_tools import McpTools
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.acp.mcp_projection import project_mcp_servers
from drivers.capability_binding import annotate
from drivers.mcp_inventory import discover_mcp, preset_sources
from drivers.mcp_tool_list import McpListError

SECRET = "sk-test-0123456789abcdefABCDEF"


def world(tmp_path, codex_config=""):
    uow = SqliteUnitOfWork(tmp_path / "app.db")
    for slug, parent in (("default", None), ("work", "project:default")):
        asyncio.run(uow.repositories.projects.save(Project.create(slug=slug, display_name=slug, parent_project_id=parent)))
    for key in ("alpha", "codex"):
        asyncio.run(uow.repositories.backends.save(Backend.create(key=key, display_name=key, driver_kind="acp")))
    config = tmp_path / "home/.codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(codex_config)
    sources = preset_sources("codex", agent_key="codex", agent_label="Codex", project_id="project:default",
                             home=tmp_path / "home", env={})
    calls = []

    def lister(server, **_):
        calls.append(server)
        if server.get("name") == "broken":
            raise McpListError("failed", "exited with code 1")
        return [{"name": "list_notes", "description": ""}, {"name": "delete_note", "description": ""}]

    tree = SkillTreeService(uow.database)
    manager = McpManager(tree, tmp_path / "library", tools=McpTools(tree, lister=lister))
    tree.discover = lambda: [*annotate(discover_mcp(sources)), *manager.discover()]
    manager.imports = CapabilityImports(tree, tmp_path / "library")
    return uow, tree, manager, calls


def by_name(manager):
    return {row["name"]: row for row in manager.list()["items"]}


def test_added_mcp_keeps_its_key_encrypted_and_reaches_every_agent_where_it_is_assigned(tmp_path):
    uow, tree, manager, calls = world(tmp_path)
    try:
        added = manager.add({"name": "notes", "transport": "stdio", "command": "python3", "args": ["notes.py"],
                             "env": {"NOTES_TOKEN": SECRET}})
        assert added["origin"] == "kaus" and added["group"] == "available" and added["status"] == "unchecked"
        assert added["definition"]["env"] == ["NOTES_TOKEN"] and added["definition"]["valuesSet"] == ["NOTES_TOKEN"]
        assert SECRET not in json.dumps(added)
        stored = "".join(p.read_text() for p in (tmp_path / "library/mcp").glob("*") if p.is_file() and p.name != ".key")
        assert SECRET not in stored  # values are encrypted at rest
        snapshot = tree.snapshot()
        row = next(s for s in snapshot["skills"] if s["id"] == added["id"])
        assert row["kind"] == "mcp" and row["pendingAssignment"] is True

        tree.act({"revision": snapshot["revision"], "action": "assign", "skillId": added["id"], "targetProjectId": "project:work"})
        project = asyncio.run(uow.repositories.projects.get("project:work"))
        binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend="alpha")))
        effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
        entry = next(e for e in effective.entries if e.capability_type == "mcp" and e.capability_id == added["id"])
        assert SECRET not in str(entry.config)
        assert project_mcp_servers(effective).servers == (
            {"name": "notes", "command": "python3", "args": ["notes.py"], "env": [{"name": "NOTES_TOKEN", "value": SECRET}]},)

        listed = manager.check()["items"]
        assert listed[0]["status"] == "ok" and listed[0]["toolCount"] == 2
        assert listed[0]["projects"] == [{"id": "project:work", "name": "work"}]
        # Editing with an empty value keeps the stored key; the changed definition is listed again.
        manager.update(added["id"], {"name": "notes", "command": "python3", "args": ["notes.py", "--v2"], "env": {"NOTES_TOKEN": ""}})
        assert by_name(manager)["notes"]["status"] == "unchecked"
        manager.check()
        assert calls[-1]["args"] == ["notes.py", "--v2"] and calls[-1]["env"] == [{"name": "NOTES_TOKEN", "value": SECRET}]

        manager.remove(added["id"])
        assert manager.list()["items"] == [] and not list((tmp_path / "library/mcp").glob("lib-mcp-*.json"))
        assert all(s["id"] != added["id"] for s in tree.snapshot()["skills"])
    finally:
        uow.close()


def test_a_missing_key_needs_attention_and_a_failing_server_keeps_its_diagnostics(tmp_path):
    uow, tree, manager, _ = world(tmp_path)
    try:
        manager.add({"name": "github", "url": "https://example.test/mcp", "headers": {"Authorization": ""}})
        manager.add({"name": "broken", "command": "python3", "args": ["broken.py"]})
        rows = {row["name"]: row for row in manager.check()["items"]}
        assert rows["github"]["status"] == "auth_required" and rows["github"]["group"] == "attention"
        assert rows["broken"]["status"] == "failed" and rows["broken"]["error"] == "exited with code 1"
        with pytest.raises(ApiError) as invalid:
            manager.add({"name": "x", "url": "ftp://example.test"})
        assert invalid.value.code == "mcp_invalid"
    finally:
        uow.close()


def test_agent_mcp_groups_import_bind_and_unbind(tmp_path):
    uow, tree, manager, _ = world(tmp_path, '[mcp_servers.fs]\ncommand = "uvx"\nargs = ["fs-mcp"]\n'
                                             '[mcp_servers.repl]\ncommand = "repl"\n')
    try:
        tree.begin_onboarding("codex", "Codex")
        rows = by_name(manager)
        assert rows["fs"]["group"] == "notImported" and rows["fs"]["origin"] == "agent" and rows["fs"]["agentLabel"] == "Codex"
        manager.import_one(rows["fs"]["id"])
        assert by_name(manager)["fs"]["group"] == "available"

        bound = manager.bind(rows["repl"]["id"])
        assert bound["group"] == "bound" and bound["boundReason"] == "user_marked"
        assert bound["projects"] == [{"id": "project:default", "name": "default"}]
        released = manager.unbind(rows["repl"]["id"])
        assert released["group"] == "available" and "boundReason" not in released
        # A rule-based binding released by the user stays released after the next discovery.
        tree.discover = lambda d=tree.discover: [{**r, **({"bound": True, "boundReason": "agent_owned"} if r.get("name") == "repl" else {})} for r in d()]
        assert by_name(manager)["repl"]["group"] == "available"

        with pytest.raises(ApiError) as editing:
            manager.update(rows["fs"]["id"], {"name": "fs", "command": "x"})
        assert editing.value.code == "mcp_not_editable"
        manager.remove(rows["fs"]["id"])  # back to not imported; the Agent's config stays
        assert by_name(manager)["fs"]["group"] == "notImported"
    finally:
        uow.close()


def test_not_isolable_binding_cannot_be_released(tmp_path):
    uow, tree, manager, _ = world(tmp_path, '[mcp_servers.fs]\ncommand = "uvx"\n')
    try:
        base = tree.discover
        tree.discover = lambda: [{**r, "bound": True, "boundReason": "not_isolable"} if r.get("name") == "fs" else r for r in base()]
        row = by_name(manager)["fs"]
        assert row["group"] == "bound" and row["boundReason"] == "not_isolable"
        with pytest.raises(ApiError) as locked:
            manager.unbind(row["id"])
        assert locked.value.code == "capability_not_isolable"
    finally:
        uow.close()
