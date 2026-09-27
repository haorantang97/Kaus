"""批次十九第 3 件：``GET /api/bindings/{id}/status`` 与 bindings 列表的并入。

覆盖
----
1. 端点回 ``{bindingId, auth, nativeSessionCount, probeState, probeMessage}``，
   且鉴权是必须的；
2. ``GET /api/projects/{id}/bindings`` 的每行并上 ``auth`` /
   ``nativeSessionCount``——**登记了取数面才有**，没登记时这两个键整个不出现；
3. Driver 没注册 / 抛异常时退化成 ``null``，不是 500，也不是编一个 ``0``；
4. 红线：响应里不得出现任何凭据值（本文件用假 Driver，凭据侧的取证在
   ``drivers/hermes/tests/test_batch19_auth.py``）。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 与几个只为本文件存在的替身。
一行都不碰真实引擎家目录。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import binding_status as status_mod  # noqa: E402
from app.api.binding_router import build_binding_write_router  # noqa: E402
from app.api.router import build_domain_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import AuthState  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402

TEST_TOKEN = "test-token-0123456789"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.app = fastapi.FastAPI()
        # 顺序照抄宿主：只读领域路由先挂，写端点后挂。
        self.app.include_router(build_domain_router(self.repositories))
        self.app.include_router(
            build_binding_write_router(
                self.repositories,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                registry=self.registry,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(
            Backend.create(
                key="mock",
                driver_kind="mock",
                probe_state="available",
                probe_message="假引擎就绪",
            )
        )
        project = await repos.projects.save(Project.create(slug="workbench"))
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(project=project, backend="mock", is_default=True)
        )
        # 计数有个可数的东西：建两条原生会话。
        for _ in range(2):
            await self.driver.create_native_session(self.binding, _options())

    def register_provider(self) -> None:
        async def provider(binding):
            return await status_mod.read_binding_status(
                binding, registry=self.registry
            )

        status_mod.set_status_provider(provider)


def _options():
    from drivers.base import CreateSessionOptions

    return CreateSessionOptions()


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        # 取数面是进程级状态：每个用例结束必须摘干净，否则用例之间会互相污染。
        status_mod.clear_status_provider()
        built.unit_of_work.close()


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #


def test_status_endpoint_returns_auth_count_and_probe(client, harness):
    """一次请求给齐三件事——引擎卡与会话页页头因此不必各拉一次后端。"""
    response = client.get(f"/api/bindings/{harness.binding.id}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bindingId"] == harness.binding.id
    assert body["nativeSessionCount"] == 2
    assert body["probeState"] == "available"
    assert body["probeMessage"] == "假引擎就绪"
    auth = body["auth"]
    assert auth["state"] == "signed_in"
    assert auth["model"] == "managed-credential"
    # 账号一律脱敏后才上 wire（v1.0 §16.6）。
    assert auth["account"] == "m***@example.test"
    assert "checkedAt" in auth


def test_status_endpoint_requires_auth(harness):
    """AD-72 的免鉴权口径只给只读领域端点；这条问的是本机凭据配置，必须鉴权。"""
    with TestClient(harness.app) as anonymous:
        response = anonymous.get(f"/api/bindings/{harness.binding.id}/status")
    assert response.status_code == 401


def test_status_endpoint_404_for_unknown_binding(client):
    response = client.get("/api/bindings/binding:nope/status")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "binding_not_found"


def test_status_degrades_to_null_when_the_driver_is_not_registered(
    harness, tmp_path
):
    """Registry 里没有这个 backend：auth 与计数是 null，不是 500，也不是 0。"""
    empty = fastapi.FastAPI()
    empty.include_router(
        build_binding_write_router(
            harness.repositories,
            auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
            registry=BackendDriverRegistry([]),
        )
    )
    with TestClient(
        empty, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ) as test_client:
        body = test_client.get(f"/api/bindings/{harness.binding.id}/status").json()

    assert body["auth"] is None
    assert body["nativeSessionCount"] is None


def test_status_degrades_to_null_when_the_driver_raises(client, harness):
    """Driver 炸了也只是「这次问不出来」——一整页引擎卡不该因此变成 500。"""

    async def boom(_binding):
        raise RuntimeError("引擎那边出事了")

    harness.driver.read_auth_state = boom  # type: ignore[method-assign]
    harness.driver.count_native_sessions = boom  # type: ignore[method-assign]

    response = client.get(f"/api/bindings/{harness.binding.id}/status")

    assert response.status_code == 200, response.text
    assert response.json()["auth"] is None
    assert response.json()["nativeSessionCount"] is None


# --------------------------------------------------------------------------- #
# 并入 bindings 列表
# --------------------------------------------------------------------------- #


def test_bindings_list_carries_auth_and_count_when_a_provider_is_registered(
    client, harness
):
    harness.register_provider()

    rows = client.get(f"/api/projects/{harness.project_id}/bindings").json()["bindings"]

    assert len(rows) == 1
    assert rows[0]["nativeSessionCount"] == 2
    assert rows[0]["auth"]["state"] == "signed_in"
    # 并入的是**这一行自己**的状态，不该把取数面返回的 bindingId 也塞进来
    # （行上本来就有 id，两个来源的同名字段迟早对不上）。
    assert rows[0]["id"] == harness.binding.id


def test_bindings_list_omits_the_keys_entirely_without_a_provider(client, harness):
    """没有取数面时两个键**整个不出现**（不是 null）。

    AD-71：前端按「有没有这个键」决定渲不渲染那两行；给一个 null 等于要前端去猜
    「是没有，还是没读到」。
    """
    row = client.get(f"/api/projects/{harness.project_id}/bindings").json()["bindings"][0]

    assert "auth" not in row
    assert "nativeSessionCount" not in row


def test_bindings_list_survives_a_provider_that_raises(client, harness):
    """一条读不出来不该拖垮整页。"""

    async def boom(_binding):
        raise RuntimeError("nope")

    status_mod.set_status_provider(boom)

    response = client.get(f"/api/projects/{harness.project_id}/bindings")

    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    assert "auth" not in response.json()["bindings"][0]


# --------------------------------------------------------------------------- #
# wire 形状
# --------------------------------------------------------------------------- #


def test_auth_state_to_wire_omits_empty_optional_keys():
    """``provider`` / ``account`` / ``hint`` 为空时不出现这个键（不是 null）。"""
    payload = status_mod.auth_state_to_wire(
        AuthState(state="unknown", model="own-auth")
    )

    assert payload == {"state": "unknown", "model": "own-auth"}
