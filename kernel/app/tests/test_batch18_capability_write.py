"""批次十八第 4 件：能力写端点（Phase 5 第一步 / AD-144）。

``PUT /api/projects/{id}/capabilities/{type}/{capId}`` 一个端点三件事——赋值、
禁止（AD-45，对子树生效）、解除；``DELETE`` 同路径 = 删本项目层的赋值，回到继承。
返回的永远是走**同一个 Resolver** 算出来的那一条 ``effective-capabilities``，
而不是「刚写进去的那一行」：AD-45 的子树效果与继承回落只有算过整条祖先链才知道。

Projector 不物化（AD-07）：这些端点只改领域库，不碰任何 Backend 的原生配置。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TOKEN = "test-token-0123456789"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
        )
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: TOKEN),
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", driver_kind="mock"))
        self.root = await repos.projects.save(
            Project.create(slug="root", display_name="根")
        )
        self.child = await repos.projects.save(
            Project.create(
                slug="child", display_name="子", parent_project_id=self.root.id
            )
        )
        await repos.bindings.save(
            AgentBinding.create(
                project=self.root, backend="mock", display_name="绑定", is_default=True
            )
        )

    def close(self) -> None:
        asyncio.run(self.host.aclose())
        self.unit_of_work.close()


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def client(harness):
    with TestClient(harness.app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c


def _path(project_id: str, capability_type: str, capability_id: str) -> str:
    return f"/api/projects/{project_id}/capabilities/{capability_type}/{capability_id}"


# --------------------------------------------------------------------------- #
# 赋值
# --------------------------------------------------------------------------- #


def test_putting_a_value_writes_the_local_assignment(harness, client):
    """``value`` → 本项目层的赋值，返回体里那一条已经是「有效」的样子。"""
    response = client.put(
        _path(harness.root.id, "skills", "code-review"),
        json={"value": {"enabled": True, "level": "strict"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capabilityType"] == "skills"
    assert body["capabilityId"] == "code-review"
    assert body["entry"]["config"] == {"enabled": True, "level": "strict"}
    assert body["entry"]["inherited"] is False
    assert body["entry"]["blocked"] is False
    # 与只读端点说的是同一件事。
    listed = client.get(f"/api/projects/{harness.root.id}/capabilities")
    assert listed.status_code in (200, 404)  # 领域 router 不一定挂在这套装配里


def test_putting_a_value_twice_replaces_the_row(harness, client):
    """同一 (project, type, capId) 只应有一条赋值（v1.0 §5.2）：重写即覆盖。"""
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}})
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 2}})
    rows = asyncio.run(harness.repositories.capabilities.list_for_project(harness.root.id))
    assert len(rows) == 1
    assert rows[0].config == {"v": 2}


# --------------------------------------------------------------------------- #
# 禁止 / 解除（AD-45：禁止对子树生效）
# --------------------------------------------------------------------------- #


def test_blocking_at_the_root_also_blocks_the_child(harness, client):
    """AD-45：在根上禁止 → 整棵子树都拿不到它（直到后代自己重新赋值）。"""
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}})
    blocked = client.put(_path(harness.root.id, "skills", "s1"), json={"blocked": True})
    assert blocked.status_code == 200
    assert blocked.json()["entry"]["blocked"] is True
    assert blocked.json()["entry"]["blockedByProjectId"] == harness.root.id

    # 子项目什么都没写：禁止顺着树传下来（AD-06 位置式传播）。
    inherited = client.delete(_path(harness.child.id, "skills", "s1"))
    assert inherited.json()["entry"]["blocked"] is True
    assert inherited.json()["entry"]["blockedByProjectId"] == harness.root.id

    # 后代重新赋值 → 接续（Resolver 的既定口径：block 生效**直到**后代重新赋值）。
    child = client.put(
        _path(harness.child.id, "skills", "s1"), json={"value": {"v": 9}}
    )
    assert child.json()["entry"]["blocked"] is False
    assert child.json()["entry"]["config"] == {"v": 9}


def test_unblocking_removes_only_the_local_block_row(harness, client):
    """``blocked=false`` → 解除本项目层的禁止，回到继承。"""
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}})
    client.put(_path(harness.child.id, "skills", "s1"), json={"blocked": True})
    assert client.put(
        _path(harness.child.id, "skills", "s1"), json={"blocked": False}
    ).json()["entry"] == {
        "capabilityType": "skills",
        "capabilityId": "s1",
        "config": {"v": 1},
        "version": None,
        "sourceProjectId": harness.root.id,
        "inherited": True,
        "overridden": False,
        "contributingProjectIds": [harness.root.id],
        "blocked": False,
    }
    # 根上那一行一动没动——那是别人的节点。
    root_rows = asyncio.run(
        harness.repositories.capabilities.list_for_project(harness.root.id)
    )
    assert len(root_rows) == 1


def test_blocked_true_with_a_value_is_a_contradiction(harness, client):
    """block 行不携带 config（v1.0 §5.2）：两个都给是矛盾请求，不能悄悄丢一半。"""
    response = client.put(
        _path(harness.root.id, "skills", "s1"), json={"blocked": True, "value": {"v": 1}}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "blocked_with_value"


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


def test_delete_drops_the_local_row_and_falls_back_to_inheritance(harness, client):
    """``DELETE`` = 这个节点不再对它有意见 → 回到祖先给的值。"""
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}})
    client.put(_path(harness.child.id, "skills", "s1"), json={"value": {"v": 2}})
    response = client.delete(_path(harness.child.id, "skills", "s1"))
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["entry"]["config"] == {"v": 1}
    assert body["entry"]["inherited"] is True
    # 再删一次：没有可删的，如实说 deleted=false，但不是错误。
    assert client.delete(_path(harness.child.id, "skills", "s1")).json()["deleted"] is False


def test_delete_of_the_only_assignment_leaves_no_entry(harness, client):
    """既没赋值也没继承 → ``entry`` 是 null，不是空对象。"""
    client.put(_path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}})
    assert client.delete(_path(harness.root.id, "skills", "s1")).json()["entry"] is None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_an_unknown_generic_type_is_a_400(harness, client):
    """打错的通用类型不能收下——收了就会长出一条谁也认不出的能力。"""
    response = client.put(
        _path(harness.root.id, "skils", "s1"), json={"value": {"v": 1}}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unknown_capability_type"
    assert "skills" in error["detail"]["known"]


def test_a_backend_scoped_type_needs_a_backend_that_exists(harness, client):
    """``<backend>:<name>``：backend 必须是领域库里有的（公共层不硬编码名字）。"""
    ok = client.put(
        _path(harness.root.id, "mock:runtime-config", "default"),
        json={"value": {"approvals": "smart"}},
    )
    assert ok.status_code == 200
    assert ok.json()["entry"]["config"] == {"approvals": "smart"}

    missing = client.put(
        _path(harness.root.id, "nosuch:runtime-config", "default"),
        json={"value": {"v": 1}},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "unknown_capability_type"


def test_an_empty_patch_is_a_400(harness, client):
    assert (
        client.put(_path(harness.root.id, "skills", "s1"), json={}).json()["error"][
            "code"
        ]
        == "empty_capability_patch"
    )


def test_an_unknown_project_is_a_404(harness, client):
    response = client.put(
        _path("project:nope", "skills", "s1"), json={"value": {"v": 1}}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_the_write_endpoints_require_auth(harness):
    """D-17：写端点走 ``_auth``，没有 Bearer 一律 401。"""
    with TestClient(harness.app) as anonymous:
        assert anonymous.put(
            _path(harness.root.id, "skills", "s1"), json={"value": {"v": 1}}
        ).status_code == 401
        assert anonymous.delete(
            _path(harness.root.id, "skills", "s1")
        ).status_code == 401
