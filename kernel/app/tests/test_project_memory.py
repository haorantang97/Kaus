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
from app.projects.memory_injection import MEMORY_MCP_ID, MEMORY_SECTION_ID, with_project_memory  # noqa: E402
from app.projects.models import Project  # noqa: E402
from drivers.launch_delivery import plan_launch  # noqa: E402

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


def test_every_session_gets_the_index_and_memory_server_but_group_members_do_not(world):
    async def scenario():
        await world.memory.add(world.child.id, "设计", "首页主色是墨绿")
        effective = await with_project_memory(_effective(world.child.id), _binding(world.child.id), world.repos,
                                              token_file=Path("/tmp/token"), url="http://127.0.0.1:8877", python="/usr/bin/python3")
        section = next(e for e in effective.entries if e.capability_id == MEMORY_SECTION_ID)
        assert "首页主色是墨绿" in section.config["body"] and section.config["auxiliary"] and section.config["title"] == "项目记忆"
        server = next(e for e in effective.entries if e.capability_id == MEMORY_MCP_ID).config["value"]
        assert server["command"] == "/usr/bin/python3" and server["args"][1:4] == ["--project", world.child.id, "--agent"]
        member = await with_project_memory(_effective(world.child.id), _binding(world.child.id, group_execution=True),
                                           world.repos, token_file=Path("/tmp/token"), url="x")
        assert member.entries == ()
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
        return await with_project_memory(_effective(world.child.id, [rule]), _binding(world.child.id), world.repos,
                                         token_file=Path("/tmp/token"), url="x")
    effective = asyncio.run(scenario())
    work = tmp_path / "work"
    work.mkdir()
    plan_launch(effective, adapter=None, instructions_file="AGENTS.md", workspace_root=str(work), project_id=world.child.id)
    written = (work / "AGENTS.md").read_text(encoding="utf-8")
    assert "先给结论" in written and "首页主色是墨绿" in written


def test_memory_server_speaks_mcp_and_forwards_to_kaus(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("kaus_memory_mcp", Path(__file__).resolve().parents[3] / "mcp_servers" / "kaus_memory_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.parse_args(["--project", "project:site", "--agent", "backend:codex", "--token-file", "/nonexistent"])
    calls = []
    monkeypatch.setattr(client, "http", lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True})
    init = module.handle_request(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "kaus-memory" and "kaus_memory_write" in init["result"]["instructions"]
    tools = module.handle_request(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"kaus_memory_search", "kaus_memory_write"}
    module.handle_request(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kaus_memory_write",
                          "arguments": {"action": "add", "topic": "t", "body": "b"}}})
    module.handle_request(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kaus_memory_search",
                          "arguments": {"query": "部署"}}})
    assert calls[0] == ("POST", "/api/projects/project%3Asite/memories/write", {"action": "add", "topic": "t", "body": "b", "actor": "backend:codex"})
    assert calls[1][0] == "GET" and calls[1][1].startswith("/api/projects/project%3Asite/memories/search?q=")
    missing = module.Client("http://127.0.0.1:1", Path("/nonexistent"), "p", None).call("kaus_memory_search", {"query": "x"})
    assert missing["isError"] and "token" in missing["content"][0]["text"]


def test_auxiliary_memory_capabilities_are_never_reported_as_unapplied(world):
    from drivers.instructions_projection import without_auxiliary
    from drivers.projection_gaps import unapplied_kinds

    async def scenario():
        return await with_project_memory(_effective(world.child.id), _binding(world.child.id), world.repos,
                                         token_file=Path("/tmp/token"), url="x")
    effective = asyncio.run(scenario())
    assert unapplied_kinds(effective) == ()
    assert without_auxiliary(effective).entries == ()
