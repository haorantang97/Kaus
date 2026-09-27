"""只读领域端点的错误信封与 ``project:default`` 解析（批次八第 3、5 件）。

第 5 件——**一个宿主只能有一种错误体**
--------------------------------------
会话端点一直回 ``{"error": {"code", "message"}}``，而 `app/api/router.py` 的
只读端点回的是 web 框架默认的 ``{"detail": "..."}``。前端为此写了两条解析分支。
这里逐个端点断言新形状，并断言状态码一个没变。

第 3 件——**根 Project 的 id 在所有端点上是同一个东西**
-------------------------------------------------------
前端把 ``project:default`` 写死成「默认去向」（`web/src/lib/shellPrefs.ts`），
所以这个 id 必须在**每一个**接受 project 参数的端点上解析成同一行。
下面的用例把 ``project:default`` 与裸 ``default`` 两种写法喂给全部四个端点
（含会话路由的 ``/conversations``），要求它们给出同一个 ``projectId``。

注意这里**没有**复现出「id 规范化在 default 上有特殊分支」——
:func:`normalize_project_id` 与 sqlite ``projects.get`` 在这个 id 上没有任何
分歧（下面的用例就是这条结论的固化）。真正的缺口是「这一行可能根本不存在」，
补在接入层：`domain_bootstrap.ensure_root_project`。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.router import build_domain_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"

#: 根 Project 的 slug 与显示名（IA 叫法表：slug `default` = 界面上的 X）。
ROOT_SLUG = "default"


def _client(tmp_path: Path) -> TestClient:
    """一套「库 + 只读领域路由 + 会话路由」，两套路由挂在同一个 app 上。

    两套一起挂才是生产形态——`server.py` 就是这么挂的，而报告里出问题的
    ``/bindings``（领域路由）与 ``/conversations``（会话路由）恰好分属两边。
    """
    unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repositories = unit_of_work.repositories

    async def seed() -> None:
        await repositories.projects.save(
            Project.create(slug=ROOT_SLUG, display_name="X")
        )
        await repositories.backends.save(Backend.create(key="mock", driver_kind="mock"))
        await repositories.bindings.save(
            AgentBinding.create(
                project=ROOT_SLUG, backend="mock", display_name="Mock", is_default=True
            )
        )

    asyncio.run(seed())

    registry = BackendDriverRegistry([MockDriver()])
    host = SessionHost(
        registry=registry,
        bindings=repositories.bindings,
        event_store=EventStore(repositories.events),
        lease_manager=LeaseManager(repositories.leases),
        conversations=repositories.conversations,
        idle_timeout=timedelta(minutes=15),
    )
    app = fastapi.FastAPI()
    app.include_router(build_domain_router(repositories))
    app.include_router(
        build_session_router(
            session_host=host,
            repositories=repositories,
            registry=registry,
            auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
        )
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
    return client


# --------------------------------------------------------------------------- #
# 第 5 件：错误信封
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/api/projects/nope", "project_not_found"),
        ("/api/projects/nope/bindings", "project_not_found"),
        ("/api/projects/nope/capabilities", "project_not_found"),
        ("/api/projects/nope/effective-capabilities", "project_not_found"),
        ("/api/backends/nope", "backend_not_found"),
        ("/api/bindings/binding:nope:mock", "binding_not_found"),
    ],
)
def test_not_found_uses_the_error_envelope(tmp_path: Path, path: str, code: str) -> None:
    response = _client(tmp_path).get(path)

    assert response.status_code == 404
    body = response.json()
    assert "detail" not in body, "旧的 {'detail': ...} 形状必须彻底消失"
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_reconciliation_without_a_source_is_a_coded_503(tmp_path: Path) -> None:
    """503 也换了形状，但**状态码没变**——前端的重试分支不受影响。"""
    response = _client(tmp_path).get("/api/_domain/diff")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "reconciliation_unavailable"


def test_success_bodies_are_untouched(tmp_path: Path) -> None:
    """只改错误形状，正常返回体一个字段都不动。"""
    response = _client(tmp_path).get("/api/projects")

    assert response.status_code == 200
    assert response.json()["roots"] == ["project:default"]


# --------------------------------------------------------------------------- #
# 第 3 件：project:default 在每个端点上解析成同一行
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("written_as", ["project:default", "default"])
@pytest.mark.parametrize(
    "suffix", ["", "/bindings", "/capabilities", "/effective-capabilities", "/conversations"]
)
def test_root_project_resolves_on_every_endpoint(
    tmp_path: Path, written_as: str, suffix: str
) -> None:
    response = _client(tmp_path).get(f"/api/projects/{written_as}{suffix}")

    assert response.status_code == 200, response.text
    body = response.json()
    # 单个 Project 端点返回的是 Project 本身（键是 `id`），其余是列表包装（`projectId`）。
    assert body.get("projectId", body.get("id")) == "project:default"
