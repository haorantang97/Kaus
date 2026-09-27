"""根 Project（`project:default`）的存在性保证（批次八第 3 件）。

复现的现象
----------
侧栏报告：`GET /api/projects` 能看到 `project:default`，但
`/api/projects/project:default/bindings` 与 `/conversations` 回「未知 Project」。

排查结论
--------
不是 id 解析的问题。`normalize_project_id` 与 sqlite `projects.get` 在这个 id 上
没有任何特殊分支——`kernel/app/tests/test_domain_router_errors.py` 把「两种写法 ×
全部端点都解析成 `project:default`」固化成了回归用例，只要那一行**在库里**，
每个端点都正常。

真正的缺口是那一行可能根本不存在：产出它的唯一路径是导入器，而导入器只在能
枚举到根目录时才把根列进节点集合（`scripts/migrate_profiles_readonly.py` 的
`scan_profiles`：目录不存在 → `hermes_home_missing` 告警 + 节点集合里没有它）。
端到端测试用的是全新的空 sqlite，冒烟环境也没有那个目录，于是
`project:default` 只活在前端的「默认去向」出厂值与计划的 `meta.root_project_id`
里。修法：装配领域库时保证根 Project 存在（幂等）。

本文件不碰用户真实主目录，全部用 `tmp_path`。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import domain_bootstrap  # noqa: E402
import session_bootstrap  # noqa: E402

EMPTY_STATE = {
    "hierarchy.json": {},
    "labels.json": {},
    "pinned.json": [],
    "killed.json": [],
    "drafts.json": [],
    "skill_inherit_off.json": [],
}


def make_state(tmp_path: Path) -> Path:
    """一份「什么都没有」的旧状态目录：连根目录都枚举不到的最坏情形。"""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for name, value in EMPTY_STATE.items():
        (state / name).write_text(json.dumps(value), encoding="utf-8")
    return state


def open_runtime(tmp_path: Path, db_name: str = "domain.sqlite3"):
    return domain_bootstrap.open_domain_runtime(
        state_root=make_state(tmp_path),
        # 刻意指向一个不存在的目录：导入器会报 `hermes_home_missing` 并且**不**
        # 产出根 Project，这正是要兜住的情形。
        hermes_home=tmp_path / "does-not-exist",
        db_path=tmp_path / db_name,
        capability_reconciliation=False,
    )


# --------------------------------------------------------------------------- #
# 领域库侧
# --------------------------------------------------------------------------- #


def test_root_project_exists_even_when_the_importer_produced_nothing(tmp_path):
    runtime = open_runtime(tmp_path)
    try:
        projects = asyncio.run(runtime.unit_of_work.repositories.projects.list_all())
        assert [p.id for p in projects] == ["project:default"]
        assert runtime.root_project_created is True
    finally:
        runtime.close()


def test_ensure_root_project_is_idempotent(tmp_path):
    """第二次启动是一次纯读：不重建、不改行。"""
    first = open_runtime(tmp_path)
    first.close()
    second = open_runtime(tmp_path)
    try:
        assert second.root_project_created is False
        projects = asyncio.run(second.unit_of_work.repositories.projects.list_all())
        assert [p.id for p in projects] == ["project:default"]
    finally:
        second.close()


def test_ensure_root_project_keeps_a_user_renamed_root(tmp_path):
    """R-05 只锁 slug，显示名归用户——补根绝不能把改过的名字改回去。"""
    runtime = open_runtime(tmp_path)
    repositories = runtime.unit_of_work.repositories
    root = asyncio.run(repositories.projects.get("project:default"))
    asyncio.run(repositories.projects.save(root.rename("我的根")))
    runtime.close()

    again = open_runtime(tmp_path)
    try:
        assert again.root_project_created is False
        root = asyncio.run(again.unit_of_work.repositories.projects.get("project:default"))
        assert root.display_name == "我的根"
    finally:
        again.close()


def test_root_project_answers_every_read_endpoint(tmp_path):
    """报告里 404 的那两个端点之一（`/bindings`）现在有答案了。"""
    runtime = open_runtime(tmp_path)
    try:
        app = fastapi.FastAPI()
        app.include_router(runtime.router)
        client = TestClient(app)

        assert client.get("/api/projects").json()["roots"] == ["project:default"]
        for path in (
            "/api/projects/project:default",
            "/api/projects/project:default/bindings",
            "/api/projects/project:default/capabilities",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
    finally:
        runtime.close()


# --------------------------------------------------------------------------- #
# 会话库侧（会话 flag 单开，领域 flag 关）
# --------------------------------------------------------------------------- #


def test_session_runtime_opening_its_own_db_also_gets_a_root(tmp_path):
    """`session_host_v1` 单开时这条库是 session_bootstrap 自己开的，也要有根。"""
    runtime = session_bootstrap.open_session_runtime(
        config={"backends": []},
        db_path=tmp_path / "session.sqlite3",
        token_path=tmp_path / "session_api.token",
    )
    try:
        projects = asyncio.run(runtime.repositories.projects.list_all())
        assert [p.id for p in projects] == ["project:default"]
    finally:
        asyncio.run(runtime.aclose())
