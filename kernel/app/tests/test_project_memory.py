"""Project 记忆：领域规则、接口、会话注入与记忆 MCP（全部用临时库与假数据）。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api.project_memory import build_project_memory_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.memory import INDEX_LINE_LIMIT, MemoryError_, ProjectMemory  # noqa: E402
from app.projects.builtin_mcp import BUILTIN_MCP_ID, MEMORY_SECTION_ID, SCREEN_SECTION_ID, with_builtin_mcp  # noqa: E402
from app.projects.models import Project  # noqa: E402
from drivers.launch_delivery import plan_launch, project_memory_id  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def world(tmp_path):
    unit = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repos = unit.repositories

    async def seed():
        root = await repos.projects.save(Project.create(slug="root", display_name="Root"))
        child = await repos.projects.save(Project.create(slug="site", display_name="官网", parent_project_id=root.id,
                                                         metadata={"description": "公司官网改版"}))
        return root, child
    root, child = asyncio.run(seed())
    try:
        yield SimpleNamespace(repos=repos, root=root, child=child, memory=ProjectMemory(repos), tmp=tmp_path)
    finally:
        unit.close()


def test_child_reads_parent_but_edits_only_its_own_and_every_change_can_be_undone(world):
    memory, root, child = world.memory, world.root, world.child

    async def scenario():
        rule = await memory.add(root.id, "包管理", "统一用 pnpm", actor="backend:codex")
        own = await memory.add(child.id, "部署", "预览环境在 Vercel", actor="backend:claude-code")
        visible = {e["id"] for e in await memory.visible(child.id)}
        assert {rule["id"], own["id"]} <= visible
        with pytest.raises(MemoryError_) as inherited:
            await memory.update(child.id, rule["id"], body="改")
        assert inherited.value.code == "memory_inherited"
        await memory.update(child.id, own["id"], body="预览环境改到 Netlify")
        await memory.invalidate(child.id, own["id"])
        assert own["id"] not in {e["id"] for e in await memory.visible(child.id)}
        assert (await memory.visible(child.id, include_inactive=True))[-1]["status"] == "invalidated"
        latest = (await memory.history(child.id))[0]
        assert latest["action"] == "invalidate"
        restored = await memory.undo(child.id, latest["id"])
        assert restored["status"] == "active" and restored["body"] == "预览环境改到 Netlify"
        assert [h["action"] for h in await memory.history(child.id)][:2] == ["undo", "invalidate"]
    asyncio.run(scenario())


def test_search_handles_chinese_and_index_puts_the_overview_first(world):
    memory, root, child = world.memory, world.root, world.child

    async def scenario():
        await memory.add(root.id, "包管理", "统一用 pnpm，不要用 npm")
        await memory.add(child.id, "设计", "首页主色是墨绿")
        lines, overview = await memory.index(child.id)
        assert overview is None and lines[0].startswith("- 项目概述（新建时的描述") and "公司官网改版" in lines[0]
        assert any("（Root）" in line and "pnpm" in line for line in lines)
        await memory.add(child.id, "项目概述", "官网改版，目标月底上线", kind="overview")
        await memory.add(child.id, "项目概述", "官网改版，目标下月上线", kind="overview")  # 只有一条，第二次是更新
        lines, overview = await memory.index(child.id)
        assert overview["body"] == "官网改版，目标下月上线" and "项目概述" in lines[0] and len(lines) == 3
        assert [e["topic"] for e in await memory.search(child.id, "首页颜色主色")] == ["设计"]
        assert [e["topic"] for e in await memory.search(child.id, "pnpm")] == ["包管理"]
    asyncio.run(scenario())


def test_agent_write_asks_to_choose_when_similar_and_reports_the_index_limit(world):
    app = fastapi.FastAPI()
    app.include_router(build_project_memory_router(world.repos, auth_policy=SessionAuthPolicy(lambda: TOKEN)))
    base = f"/api/projects/{world.child.id}/memories"
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        first = client.post(f"{base}/write", json={"action": "add", "topic": "部署", "body": "预览环境在 Vercel", "actor": "backend:codex"}).json()
        assert first["action"] == "add" and first["memory"]["source"] == "backend:codex" and not first["overLimit"]
        similar = client.post(f"{base}/write", json={"action": "add", "topic": "部署", "body": "预览环境改到 Netlify"}).json()
        assert similar["action"] == "choose" and similar["similar"][0]["id"] == first["memory"]["id"] and "confirm" in similar["next"]
        updated = client.post(f"{base}/write", json={"action": "update", "memory_id": first["memory"]["id"], "body": "预览环境改到 Netlify"}).json()
        assert updated["memory"]["body"] == "预览环境改到 Netlify"
        assert client.post(f"{base}/write", json={"action": "keep"}).json()["action"] == "keep"
        assert client.post(f"{base}/write", json={"action": "drop"}).status_code == 400
        for n in range(INDEX_LINE_LIMIT + 1):
            client.post(f"{base}/write", json={"action": "add", "topic": f"条目{n}", "body": f"内容 {n} zz{n}", "confirm": True})
        over = client.post(f"{base}/write", json={"action": "add", "topic": "再来", "body": "xyz", "confirm": True}).json()
        assert over["overLimit"] and "合并" in over["next"]
        listing = client.get(base).json()
        assert listing["overLimit"] and len(listing["memories"]) > INDEX_LINE_LIMIT
        assert client.delete(f"{base}/{first['memory']['id']}").json()["status"] == "deleted"
        assert client.get(f"{base}?includeInactive=true").status_code == 200
        assert client.get(base, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/projects/project:missing/memories").status_code == 404


def _effective(project_id, entries=()):
    return EffectiveCapabilities(project_id=project_id, entries=tuple(entries))


def _binding(project_id, **runtime):
    return SimpleNamespace(project_id=project_id, backend_id="backend:opencode", runtime_config=runtime)


def test_every_session_gets_the_builtin_server_and_group_members_get_only_the_screen(world):
    async def scenario():
        await world.memory.add(world.child.id, "设计", "首页主色是墨绿")
        effective = await with_builtin_mcp(_effective(world.child.id), _binding(world.child.id), world.repos,
                                           token_file=Path("/tmp/token"), url="http://127.0.0.1:8877", python="/usr/bin/python3")
        section = next(e for e in effective.entries if e.capability_id == MEMORY_SECTION_ID)
        assert "首页主色是墨绿" in section.config["body"] and section.config["auxiliary"] and section.config["title"] == "项目记忆"
        screen = next(e for e in effective.entries if e.capability_id == SCREEN_SECTION_ID)
        assert "kaus_screen_occupy" in screen.config["body"] and screen.config["auxiliary"]
        builtin = next(e for e in effective.entries if e.capability_id == BUILTIN_MCP_ID)
        server = builtin.config["value"]
        assert builtin.config["memory"] is True and server["args"][0].endswith("kaus_builtin_mcp.py")
        assert server["command"] == "/usr/bin/python3" and server["args"][1:4] == ["--project", world.child.id, "--agent"]
        assert "--no-memory" not in server["args"]
        member = await with_builtin_mcp(_effective(world.child.id), _binding(world.child.id, group_execution=True),
                                        world.repos, token_file=Path("/tmp/token"), url="x")
        assert {e.capability_id for e in member.entries} == {SCREEN_SECTION_ID, BUILTIN_MCP_ID}
        member_server = next(e for e in member.entries if e.capability_id == BUILTIN_MCP_ID)
        assert "--no-memory" in member_server.config["value"]["args"] and "memory" not in member_server.config
        # 组员的内置 MCP 不带记忆，送达层不据此关掉 Agent 自带记忆。
        assert project_memory_id(member) is None and project_memory_id(effective) == BUILTIN_MCP_ID
        return effective
    effective = asyncio.run(scenario())
    # 只有记忆一节：读文件的 Agent 没有工作目录也不拦、不写文件、不报未生效。
    plan = plan_launch(effective, adapter=None, instructions_file="AGENTS.md", workspace_root=None, project_id=world.child.id)
    assert plan.unapplied == () or not plan.unapplied
    assert not (world.tmp / "AGENTS.md").exists()


def test_with_project_instructions_the_index_travels_along(world, tmp_path):
    async def scenario():
        await world.memory.add(world.child.id, "设计", "首页主色是墨绿")
        rule = EffectiveCapability(capability_type="instructions", capability_id="section-a", config={"body": "先给结论", "title": "沟通"},
                                   source_project_id=world.child.id, inherited=False, contributing_project_ids=(world.child.id,))
        return await with_builtin_mcp(_effective(world.child.id, [rule]), _binding(world.child.id), world.repos,
                                         token_file=Path("/tmp/token"), url="x")
    effective = asyncio.run(scenario())
    work = tmp_path / "work"
    work.mkdir()
    plan_launch(effective, adapter=None, instructions_file="AGENTS.md", workspace_root=str(work), project_id=world.child.id)
    written = (work / "AGENTS.md").read_text(encoding="utf-8")
    assert "先给结论" in written and "首页主色是墨绿" in written


def _builtin_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kaus_builtin_mcp", Path(__file__).resolve().parents[3] / "mcp_servers" / "kaus_builtin_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call(module, client, rid, name, arguments=None):
    result = module.handle_request(client, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                            "params": {"name": name, "arguments": arguments or {}}})["result"]
    return result if result.get("isError") else json.loads(result["content"][0]["text"])


def test_builtin_server_speaks_mcp_and_forwards_memory_to_kaus(monkeypatch):
    module = _builtin_module()
    client = module.parse_args(["--project", "project:site", "--agent", "backend:codex", "--token-file", "/nonexistent"])
    calls = []
    monkeypatch.setattr(client, "http", lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True})
    init = module.handle_request(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "kaus"
    assert "kaus_memory_write" in init["result"]["instructions"] and "kaus_screen_occupy" in init["result"]["instructions"]
    tools = module.handle_request(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"kaus_memory_search", "kaus_memory_write", "kaus_screen_occupy", "kaus_screen_release"}
    module.handle_request(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kaus_memory_write",
                          "arguments": {"action": "add", "topic": "t", "body": "b"}}})
    module.handle_request(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kaus_memory_search",
                          "arguments": {"query": "部署"}}})
    assert calls[0] == ("POST", "/api/projects/project%3Asite/memories/write", {"action": "add", "topic": "t", "body": "b", "actor": "backend:codex"})
    assert calls[1][0] == "GET" and calls[1][1].startswith("/api/projects/project%3Asite/memories/search?q=")
    missing = module.Client("http://127.0.0.1:1", Path("/nonexistent"), "p", None).call("kaus_memory_search", {"query": "x"})
    assert missing["isError"] and "token" in missing["content"][0]["text"]


def test_group_members_get_only_the_screen_tools():
    module = _builtin_module()
    client = module.parse_args(["--project", "project:site", "--agent", "backend:codex", "--no-memory"])
    tools = module.handle_request(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"kaus_screen_occupy", "kaus_screen_release"}
    assert _call(module, client, 2, "kaus_memory_write", {"action": "keep"})["isError"]


def test_screen_occupy_waits_first_come_first_served_and_release_hands_over(tmp_path):
    module = _builtin_module()
    first = module.Client("x", Path("/nonexistent"), "project:a", "backend:codex",
                          screen=module.Screen({"agent": "backend:codex", "project": "project:a"}, root=tmp_path))
    second = module.Client("x", Path("/nonexistent"), "project:b", "backend:claude",
                           screen=module.Screen({"agent": "backend:claude", "project": "project:b"}, root=tmp_path))
    occupied = _call(module, first, 1, "kaus_screen_occupy", {"minutes": 5, "wait_seconds": 0})
    assert occupied["status"] == "occupied" and occupied["expiresAt"]
    waiting = _call(module, second, 2, "kaus_screen_occupy", {"wait_seconds": 0})
    assert waiting["status"] == "waiting" and waiting["position"] == 1
    assert waiting["holder"]["agent"] == "backend:codex" and waiting["holder"]["project"] == "project:a"
    # Renewing while holding keeps the screen.
    assert _call(module, first, 3, "kaus_screen_occupy", {"wait_seconds": 0})["status"] == "occupied"
    assert _call(module, first, 4, "kaus_screen_release")["status"] == "released"
    assert _call(module, first, 5, "kaus_screen_release")["status"] == "not_held"
    assert _call(module, second, 6, "kaus_screen_occupy", {"wait_seconds": 0})["status"] == "occupied"
    second.screen.release()
    assert _call(module, first, 7, "kaus_screen_occupy", {"wait_seconds": "soon"})["isError"]


def test_auxiliary_memory_capabilities_are_never_reported_as_unapplied(world):
    from drivers.instructions_projection import without_auxiliary
    from drivers.projection_gaps import unapplied_kinds

    async def scenario():
        return await with_builtin_mcp(_effective(world.child.id), _binding(world.child.id), world.repos,
                                         token_file=Path("/tmp/token"), url="x")
    effective = asyncio.run(scenario())
    assert unapplied_kinds(effective) == ()
    assert without_auxiliary(effective).entries == ()


def test_new_project_overview_is_the_memory_overview_entry(tmp_path):
    # 新建项目窗口选填的一句概述（2026-11-11）：草稿 Project 刚建好就能写，写的是唯一那条概述。
    from app.projects.tree_store import ProjectTreeStore
    unit = SqliteUnitOfWork(tmp_path / "draft.sqlite3")
    try:
        ProjectTreeStore(unit.database).create("site", display_name="官网", description="公司官网改版", draft=True)
        memory = ProjectMemory(unit.repositories)
        first = asyncio.run(memory.add("project:site", "项目概述", "公司官网改版", kind="overview", actor="user"))
        again = asyncio.run(memory.add("project:site", "项目概述", "官网改版，先做首页", kind="overview", actor="backend:codex"))
        assert again["id"] == first["id"] and again["body"] == "官网改版，先做首页"
        lines, overview = asyncio.run(memory.index("project:site"))
        assert overview["id"] == first["id"] and "新建时的描述" not in "\n".join(lines)
    finally:
        unit.close()
