"""MCP 单个工具开关（2026-11-11）：工具清单（假的 stdio MCP 服务器）、缓存、开关存储与按次送达。

不启动任何真实 MCP 或 Agent。
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from app.api.api_errors import ApiError
from app.capabilities.mcp_tools import McpTools
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import Project
from drivers.launch_delivery import REASON_NATIVE_UNSUPPORTED, apply_launch_plan, disabled_mcp_tools, plan_launch
from drivers.mcp_tool_list import McpListError, list_tools
from drivers.skill_execution import claude, codex, gemini

PROJECT = "project:default"

FAKE_SERVER = r'''
import json, sys
TOOLS = [{"name": "read_page", "description": "Read a page", "inputSchema": {"type": "object"}},
         {"name": "delete_page", "description": "Delete a page", "inputSchema": {"type": "object"}}]
for line in sys.stdin:
    request = json.loads(line)
    method, rid = request.get("method"), request.get("id")
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif rid is None:
        continue
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()
'''


def test_tools_are_listed_from_a_stdio_server_through_the_official_client(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)
    tools = list_tools({"name": "docs", "command": sys.executable, "args": [str(script)], "env": []})
    assert tools == [{"name": "read_page", "description": "Read a page"},
                     {"name": "delete_page", "description": "Delete a page"}]


def test_a_server_that_cannot_start_is_reported_not_raised(tmp_path):
    with pytest.raises(McpListError) as failure:
        list_tools({"name": "x", "command": str(tmp_path / "missing"), "args": [], "env": []}, timeout=5)
    assert failure.value.code == "failed"


@pytest.fixture()
def world(tmp_path):
    unit = SqliteUnitOfWork(tmp_path / "db.sqlite3")
    asyncio.run(unit.repositories.projects.save(Project.create(slug="default", display_name="Root")))
    asyncio.run(unit.repositories.projects.save(Project.create(slug="site", display_name="Site", parent_project_id=PROJECT)))
    record = {"id": "native-mcp-docs", "kind": "mcp", "name": "docs", "description": "", "agentKey": "claude-code",
              "agentLabel": "Claude Code", "available": True, "sourcePath": str(tmp_path / "claude.json"),
              "sourceFormat": "json", "sourceKey": "mcpServers", "sourceShape": "standard",
              "defaultProjectId": PROJECT, "defaultProjectIds": [PROJECT], "defaultBlockedProjectIds": [],
              "capabilityType": "claude-code:mcp", "capabilityId": "native-mcp-docs", "fingerprint": "v1",
              "server": {"command": "docs-mcp"}}
    state = SimpleNamespace(record=record)
    tree = SkillTreeService(unit.database, discover=lambda: [dict(state.record)])
    try:
        yield SimpleNamespace(unit=unit, tree=tree, state=state, tmp=tmp_path)
    finally:
        unit.close()


def _catalog_with(world, calls, result=None):
    def lister(server):
        calls.append(server)
        if isinstance(result, Exception):
            raise result
        return result or [{"name": "read_page", "description": "Read"}, {"name": "delete_page", "description": "Delete"}]
    return McpTools(world.tree, lister=lister)


def test_tool_lists_are_cached_until_the_definition_changes(world, monkeypatch):
    (world.tmp / "claude.json").write_text(json.dumps({"mcpServers": {"docs": {"command": "docs-mcp"}}}))
    world.tree.snapshot()
    calls = []
    tools = _catalog_with(world, calls)
    first = tools.tools("native-mcp-docs")
    assert first["status"] == "ok" and [t["name"] for t in first["tools"]] == ["read_page", "delete_page"]
    assert calls[0]["name"] == "docs" and calls[0]["command"] == "docs-mcp"
    tools.tools("native-mcp-docs")
    assert len(calls) == 1  # cached
    world.state.record = {**world.state.record, "fingerprint": "v2"}
    world.tree.snapshot()
    tools.tools("native-mcp-docs")
    assert len(calls) == 2  # definition changed: fetched again
    tools.tools("native-mcp-docs", refresh=True)
    assert len(calls) == 3


def test_missing_keys_or_an_auth_failure_leave_only_the_whole_switch(world, monkeypatch):
    # The Agent's own definition names a variable this process does not have: listing needs the key first.
    monkeypatch.delenv("DOCS_TOKEN", raising=False)
    (world.tmp / "claude.json").write_text(json.dumps({"mcpServers": {"docs": {
        "command": "docs-mcp", "env": {"DOCS_TOKEN": "${DOCS_TOKEN}"}}}}))
    world.tree.snapshot()
    calls = []
    result = _catalog_with(world, calls).tools("native-mcp-docs")
    assert result["status"] == "auth_required" and result["tools"] == [] and calls == []
    monkeypatch.setenv("DOCS_TOKEN", "test-token")
    assert _catalog_with(world, calls).tools("native-mcp-docs", refresh=True)["status"] == "ok"
    assert len(calls) == 1
    # A remote server that answers 401: also only the whole switch.
    (world.tmp / "claude.json").write_text(json.dumps({"mcpServers": {"docs": {"url": "https://example.invalid/mcp"}}}))
    denied = _catalog_with(world, [], McpListError("auth_required", "需要登录或密钥才能列出工具")).tools("native-mcp-docs", refresh=True)
    assert denied["status"] == "auth_required" and denied["tools"] == []
    # Gone from the Agent's configuration: failed, nothing started.
    (world.tmp / "claude.json").write_text(json.dumps({"mcpServers": {}}))
    gone = _catalog_with(world, calls).tools("native-mcp-docs", refresh=True)
    assert gone["status"] == "failed" and len(calls) == 1


def test_switches_are_set_once_on_the_mcp_and_reach_every_project(world):
    world.tree.snapshot()
    tools = _catalog_with(world, [])
    after = tools.set_disabled("native-mcp-docs", ["delete_page"])
    [skill] = [s for s in after["skills"] if s["id"] == "native-mcp-docs"]
    assert skill["disabledTools"] == ["delete_page"]
    from app.capabilities.effective import with_mcp_tool_switches
    with world.unit.database.transaction() as conn:
        from app.capabilities.skill_tree import CATALOG_KEY
        catalog = world.tree._setting(conn, CATALOG_KEY)
    for project in (PROJECT, "project:site"):
        row = EffectiveCapability(capability_type="claude-code:mcp", capability_id="native-mcp-docs", config={},
                                  source_project_id=PROJECT, inherited=project != PROJECT)
        hydrated = with_mcp_tool_switches(EffectiveCapabilities(project_id=project, entries=(row,)), catalog,
                                          {"native-mcp-docs": ["delete_page"]})
        assert hydrated.entries[0].config["disabledTools"] == ["delete_page"]
    after = tools.set_disabled("native-mcp-docs", [])
    assert "disabledTools" not in next(s for s in after["skills"] if s["id"] == "native-mcp-docs")
    with pytest.raises(ApiError):
        tools.set_disabled("native-mcp-docs", ["", 3])
    with pytest.raises(ApiError):
        tools.set_disabled("missing", [])


def test_a_cached_list_is_fetched_again_after_a_day(world):
    (world.tmp / "claude.json").write_text(json.dumps({"mcpServers": {"docs": {"command": "docs-mcp"}}}))
    world.tree.snapshot()
    calls = []
    tools = _catalog_with(world, calls)
    tools.tools("native-mcp-docs")
    from app.capabilities.mcp_tools import CACHE_KEY
    with world.unit.database.transaction() as conn:
        cache = world.tree._setting(conn, CACHE_KEY)
        cache["native-mcp-docs"]["fetchedAt"] = "2026-01-01T00:00:00+00:00"
        world.tree._save_setting(conn, CACHE_KEY, cache)
    tools.tools("native-mcp-docs")
    assert len(calls) == 2


def _effective_with_disabled():
    native = EffectiveCapability(capability_type="claude-code:mcp", capability_id="native-mcp-docs",
                                 config={"disabledTools": ["delete_page"]}, source_project_id=PROJECT, inherited=False)
    project_mcp = EffectiveCapability(capability_type="mcp", capability_id="search",
                                      config={"value": {"name": "search", "command": "search-mcp"},
                                              "disabledTools": ["purge"]}, source_project_id=PROJECT, inherited=False)
    return EffectiveCapabilities(project_id=PROJECT, entries=(project_mcp,), native_mcp=(native,))


INVENTORY = ({"capabilityId": "native-mcp-docs", "name": "docs"},)


def test_disabled_tools_are_named_by_the_server_the_agent_sees():
    assert disabled_mcp_tools(_effective_with_disabled(), INVENTORY) == (
        {"server": "search", "tool": "purge"}, {"server": "docs", "tool": "delete_page"})


def test_claude_and_codex_leave_switched_off_tools_out_of_this_launch():
    plan = plan_launch(_effective_with_disabled(), adapter=claude, instructions_file=None, workspace_root=None,
                       project_id=PROJECT, native_inventory=INVENTORY)
    assert plan.summary["mcpToolsDisabled"] == ["search/purge", "docs/delete_page"]
    _spec, metadata, _w = apply_launch_plan(plan, object(), {}, adapter=claude, cwd=None)
    assert metadata["_meta"]["claudeCode"]["options"]["disallowedTools"] == ["mcp__search__purge", "mcp__docs__delete_page"]

    class Spec(SimpleNamespace):
        def build_env(self):
            return dict(self.env)

        def with_env(self, **values):
            return Spec(env={**self.env, **values})
    spec = Spec(env={"CODEX_CONFIG": json.dumps({"mcp_servers": {"docs": {"enabled": True}}})})
    plan = plan_launch(_effective_with_disabled(), adapter=codex, instructions_file=None, workspace_root=None,
                       project_id=PROJECT, native_inventory=INVENTORY)
    spec, _m, _w = apply_launch_plan(plan, spec, {}, adapter=codex, cwd=None)
    servers = json.loads(spec.env["CODEX_CONFIG"])["mcp_servers"]
    assert servers["docs"] == {"enabled": True, "disabled_tools": ["delete_page"]}
    assert servers["search"] == {"disabled_tools": ["purge"]}


def test_agents_without_a_tool_switch_report_native_unsupported():
    plan = plan_launch(_effective_with_disabled(), adapter=gemini, instructions_file=None, workspace_root=None,
                       project_id=PROJECT, native_inventory=INVENTORY)
    [gap] = [g for g in plan.unapplied if g.kind == "mcp"]
    assert gap.reason == REASON_NATIVE_UNSUPPORTED and gap.names == ("search/purge", "docs/delete_page")
    assert plan.mcp_tools_excluded == ()
