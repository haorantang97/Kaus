"""Project 记忆的种类、有效期与重复合并（2026-11-11）：临时库与假数据，不调用模型。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api.project_memory import build_project_memory_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.memory import (  # noqa: E402
    EXPIRED, FACT, MemoryError_, ProjectMemory, TEMPORARY, near_duplicate,
)
from app.projects.models import Project  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def world(tmp_path):
    unit = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repos = unit.repositories

    async def seed():
        root = await repos.projects.save(Project.create(slug="root", display_name="Root"))
        child = await repos.projects.save(Project.create(slug="site", display_name="官网", parent_project_id=root.id))
        return root, child
    root, child = asyncio.run(seed())
    try:
        yield SimpleNamespace(repos=repos, root=root, child=child, memory=ProjectMemory(repos))
    finally:
        unit.close()


def _age(repos, entry, **delta):
    """把一条记忆的到期时间挪到过去（模拟时间流逝）。"""
    past = (datetime.now(tz=timezone.utc) - timedelta(**delta)).isoformat()
    asyncio.run(repos.memories.save({**entry, "expiresAt": past}))


def test_facts_last_and_temporary_entries_expire_into_history(world):
    memory, repos, root = world.memory, world.repos, world.root
    fact = asyncio.run(memory.add(root.id, "包管理", "统一用 pnpm"))
    status = asyncio.run(memory.add(root.id, "本周", "正在迁移登录页", memory_type=TEMPORARY, expires_days=7))
    assert fact["type"] == FACT and fact["expiresAt"] is None
    assert status["type"] == TEMPORARY
    assert 6 <= (datetime.fromisoformat(status["expiresAt"]) - datetime.now(tz=timezone.utc)).days <= 7
    lines, _ = asyncio.run(memory.index(root.id))
    assert any("（临时，至 " in line and "本周" in line for line in lines)

    _age(repos, status, minutes=1)
    assert [e["id"] for e in asyncio.run(memory.visible(root.id))] == [fact["id"]]
    assert asyncio.run(memory.search(root.id, "登录页")) == []
    collapsed = [e for e in asyncio.run(memory.visible(root.id, include_inactive=True)) if e["id"] == status["id"]]
    assert collapsed[0]["status"] == EXPIRED
    latest = asyncio.run(memory.history(root.id))[0]
    assert latest["action"] == "expire" and latest["actor"] == "kaus"
    restored = asyncio.run(memory.undo(root.id, latest["id"]))
    assert restored["status"] == "active" and restored["type"] == FACT and restored["expiresAt"] is None
    assert status["id"] in {e["id"] for e in asyncio.run(memory.visible(root.id))}  # 撤销后留着，转为长期有效


def test_children_see_inherited_temporary_entries_collapse_too(world):
    memory, repos = world.memory, world.repos
    status = asyncio.run(memory.add(world.root.id, "冻结", "本周不发版", memory_type=TEMPORARY))
    assert status["id"] in {e["id"] for e in asyncio.run(memory.visible(world.child.id))}
    _age(repos, status, seconds=1)
    assert status["id"] not in {e["id"] for e in asyncio.run(memory.visible(world.child.id))}


def test_invalid_type_or_days_are_rejected(world):
    with pytest.raises(MemoryError_):
        asyncio.run(world.memory.add(world.root.id, "x", "y", memory_type="forever"))
    for days in (0, 91, "7", True):
        with pytest.raises(MemoryError_):
            asyncio.run(world.memory.add(world.root.id, "x", f"y{days}", memory_type=TEMPORARY, expires_days=days))


def test_identical_or_nearly_identical_entries_merge_into_one(world):
    memory, root = world.memory, world.root
    first = asyncio.run(memory.add(root.id, "测试", "提交前先跑三套测试。", actor="backend:codex"))
    same = asyncio.run(memory.add(root.id, "约定", "提交前先跑三套测试", actor="backend:claude-code"))
    assert same["id"] == first["id"] and same["topic"] == "测试"
    longer = "Run the full kernel, host and web checks before every commit, then record the result"
    english = asyncio.run(memory.add(root.id, "checks", longer.replace(", then record the result", ", then record results")))
    merged = asyncio.run(memory.add(root.id, "checks", longer))
    assert merged["id"] == english["id"] and merged["body"] == longer  # 留较长的正文
    different = asyncio.run(memory.add(root.id, "部署", "预览环境在 Vercel"))
    assert different["id"] not in (first["id"], english["id"])
    assert len(asyncio.run(memory.visible(root.id))) == 3
    assert asyncio.run(memory.history(root.id))[1]["action"] == "merge"


def test_merge_keeps_a_fact_and_the_later_expiry(world):
    memory, root = world.memory, world.root
    short = asyncio.run(memory.add(root.id, "冻结", "周五前不发版", memory_type=TEMPORARY, expires_days=2))
    long = asyncio.run(memory.add(root.id, "冻结", "周五前不发版", memory_type=TEMPORARY, expires_days=9))
    assert long["id"] == short["id"] and long["expiresAt"] > short["expiresAt"]
    fact = asyncio.run(memory.add(root.id, "冻结", "周五前不发版", memory_type=FACT))
    assert fact["id"] == short["id"] and fact["type"] == FACT and fact["expiresAt"] is None


def test_parent_entries_and_the_overview_never_merge(world):
    memory = world.memory
    parent = asyncio.run(memory.add(world.root.id, "包管理", "统一用 pnpm"))
    own = asyncio.run(memory.add(world.child.id, "包管理", "统一用 pnpm"))
    assert own["id"] != parent["id"] and own["projectId"] == world.child.id
    overview = asyncio.run(memory.add(world.child.id, "项目概述", "统一用 pnpm", kind="overview"))
    assert overview["kind"] == "overview" and overview["id"] != own["id"]


def test_near_duplicate_rules():
    assert near_duplicate("用 pnpm 管理依赖", "用 pnpm 管理依赖。")
    assert near_duplicate("Use PNPM", "use pnpm!")
    assert not near_duplicate("用户偏好中文回复", "用户偏好用中文回复")  # 改了意思的不并
    assert not near_duplicate("预览环境在 Vercel", "预览环境改到 Netlify")
    assert not near_duplicate("", "x")


def test_agent_write_reports_merged_and_accepts_lifetime(world):
    app = fastapi.FastAPI()
    app.include_router(build_project_memory_router(world.repos, auth_policy=SessionAuthPolicy(lambda: TOKEN)))
    base = f"/api/projects/{world.root.id}/memories"
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        first = client.post(f"{base}/write", json={"action": "add", "topic": "本周", "body": "在迁移登录页",
                                                   "type": "temporary", "expires_days": 3}).json()
        assert first["action"] == "add" and first["memory"]["type"] == "temporary"
        again = client.post(f"{base}/write", json={"action": "add", "topic": "本周", "body": "在迁移登录页。"}).json()
        assert again["action"] == "merged" and again["memory"]["id"] == first["memory"]["id"]
        assert again["memory"]["type"] == "fact"  # 无 type 即事实，并入后长期有效
        assert client.post(f"{base}/write", json={"action": "add", "topic": "x", "body": "y", "type": "forever"}).status_code == 400
        updated = client.post(f"{base}/write", json={"action": "update", "memory_id": first["memory"]["id"],
                                                     "type": "temporary", "expires_days": 1}).json()
        assert updated["memory"]["type"] == "temporary" and updated["memory"]["expiresAt"]
        created = client.post(base, json={"topic": "冻结", "body": "周五前不发版", "type": "temporary", "expiresDays": 5}).json()
        assert created["type"] == "temporary"
