"""Binding 写端点（批次八第 2 件）。

覆盖
----
1. ``PATCH``：改 ``defaultModelId``、合并 ``runtimeConfig`` 的通用键、
   拒绝非通用键、空 body 400、**不投影到引擎**；
2. ``POST /bindings/{id}/make-default``：旧默认被降级，且「一个 Project 一个
   默认」的不变量守住；重复调用是 no-op；
3. ``POST /projects/{id}/bindings``：建出来的是**非默认**、``origin=domain``；
   重复 id 409；未知 Project / Backend 404；
4. ``DELETE``：默认 Binding 409、有活跃 Runtime 409、其余删掉；
5. 对账：``origin=domain`` 的行进 ``bindings.domainOwned``，**不**进
   ``unexpectedInDb``，``inSync`` 不受影响；
6. 鉴权：没 token 一律 401。

隔离：SQLite 建在 `tmp_path`，Driver 是 MockDriver。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.binding_router import build_binding_write_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.views import build_domain_diff  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.registry = BackendDriverRegistry([MockDriver()])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_binding_write_router(
                self.repositories,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                session_host=self.host,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", driver_kind="mock"))
        await repos.backends.save(Backend.create(key="other", driver_kind="native"))
        project = await repos.projects.save(Project.create(slug="workbench"))
        self.project_id = project.id
        self.default_binding = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend="mock",
                display_name="默认",
                is_default=True,
                runtime_config={"twin_mode": "分身"},
            )
        )
        self.spare = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend="mock",
                discriminator="spare",
                display_name="备用",
            )
        )


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        asyncio.run(built.host.aclose())
        built.unit_of_work.close()


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# PATCH
# --------------------------------------------------------------------------- #


def test_patch_sets_the_default_model(client, harness):
    response = client.patch(
        f"/api/bindings/{harness.spare.id}", json={"defaultModelId": "mock-large"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["defaultModelId"] == "mock-large"
    # 只改库里那一行，不投影到引擎（投影是 Phase 5）。
    assert response.json()["projectedToBackend"] is False


def test_patch_merges_runtime_config_and_keeps_importer_keys(client, harness):
    """合并而不是替换：导入器写的 `twin_mode` 不能被一次改推理强度顺手抹掉。"""
    response = client.patch(
        f"/api/bindings/{harness.default_binding.id}",
        json={"runtimeConfig": {"reasoning_effort": "high"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["runtimeConfig"] == {
        "twin_mode": "分身",
        "reasoning_effort": "high",
    }


def test_patch_can_clear_one_generic_key_with_null(client, harness):
    client.patch(
        f"/api/bindings/{harness.spare.id}",
        json={"runtimeConfig": {"reasoning_effort": "high"}},
    )
    response = client.patch(
        f"/api/bindings/{harness.spare.id}",
        json={"runtimeConfig": {"reasoning_effort": None}},
    )

    assert response.json()["runtimeConfig"] == {}


def test_patch_rejects_engine_private_runtime_config_keys(client, harness):
    response = client.patch(
        f"/api/bindings/{harness.spare.id}",
        json={"runtimeConfig": {"twin_mode": "别的"}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "runtime_config_key_not_generic"
    assert body["error"]["rejectedKeys"] == ["twin_mode"]


def test_empty_patch_is_a_coded_400(client, harness):
    response = client.patch(f"/api/bindings/{harness.spare.id}", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_patch"


def test_patch_unknown_binding_is_404(client):
    response = client.patch(
        "/api/bindings/binding:workbench:mock:nope", json={"defaultModelId": "x"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "binding_not_found"


# --------------------------------------------------------------------------- #
# make-default
# --------------------------------------------------------------------------- #


def test_make_default_demotes_the_previous_one(client, harness):
    response = client.post(f"/api/bindings/{harness.spare.id}/make-default")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isDefault"] is True and body["changed"] is True
    assert body["previousDefaultBindingId"] == harness.default_binding.id

    async def read_default():
        return await harness.repositories.bindings.get_default_for_project(
            harness.project_id
        )

    # 「一个 Project 一个默认」守住了：旧的那条确实被降级。
    assert asyncio.run(read_default()).id == harness.spare.id


def test_make_default_twice_is_a_no_op(client, harness):
    client.post(f"/api/bindings/{harness.spare.id}/make-default")
    again = client.post(f"/api/bindings/{harness.spare.id}/make-default")

    assert again.json()["changed"] is False


# --------------------------------------------------------------------------- #
# 新建
# --------------------------------------------------------------------------- #


def test_create_makes_a_non_default_domain_owned_binding(client, harness):
    response = client.post(
        f"/api/projects/{harness.project_id}/bindings",
        json={"backendId": "other", "discriminator": "second", "displayName": "第二个"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "binding:workbench:other:second"
    assert body["backendId"] == "backend:other"
    assert body["displayName"] == "第二个"
    assert body["isDefault"] is False
    assert body["origin"] == "domain"


def test_create_accepts_a_bare_backend_key_and_a_bare_project_slug(client):
    response = client.post(
        "/api/projects/workbench/bindings", json={"backendId": "backend:other"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["id"] == "binding:workbench:other"


def test_create_duplicate_is_409(client, harness):
    client.post(
        f"/api/projects/{harness.project_id}/bindings", json={"backendId": "other"}
    )
    again = client.post(
        f"/api/projects/{harness.project_id}/bindings", json={"backendId": "other"}
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "binding_already_exists"


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("/api/projects/nope/bindings", {"backendId": "mock"}, "project_not_found"),
        ("/api/projects/workbench/bindings", {"backendId": "ghost"}, "backend_not_found"),
    ],
)
def test_create_unknown_refs_are_404(client, path, body, code):
    response = client.post(path, json=body)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == code


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


def test_delete_removes_only_the_domain_row(client, harness):
    response = client.delete(f"/api/bindings/{harness.spare.id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"bindingId": harness.spare.id, "deleted": True}
    assert asyncio.run(harness.repositories.bindings.get(harness.spare.id)) is None


def test_default_binding_cannot_be_deleted(client, harness):
    response = client.delete(f"/api/bindings/{harness.default_binding.id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "default_binding_not_deletable"


def test_active_runtime_blocks_deletion(client, harness):
    async def start() -> str:
        conversation = await harness.repositories.conversations.save(
            Conversation.create(
                project_id=harness.project_id,
                agent_binding_id=harness.spare.id,
                title="在跑",
            )
        )
        await harness.host.start_runtime(conversation)
        return conversation.id

    conversation_id = asyncio.run(start())
    response = client.delete(f"/api/bindings/{harness.spare.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "binding_has_active_runtime"
    assert body["error"]["activeConversationIds"] == [conversation_id]
    # 挡住之后那一行还在。
    assert asyncio.run(harness.repositories.bindings.get(harness.spare.id)) is not None


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #


def test_domain_owned_bindings_are_not_unexpected_in_the_diff(client, harness):
    """写端点建的行不是漂移——它单独列在 `domainOwned`，`inSync` 不受影响。"""
    created = client.post(
        f"/api/projects/{harness.project_id}/bindings", json={"backendId": "other"}
    ).json()

    async def all_bindings():
        return list(
            await harness.repositories.bindings.list_for_project(harness.project_id)
        )

    actual = asyncio.run(all_bindings())
    # 期望侧 = 导入器算出来的那两条（写端点建的那条它不知道）。
    expected = [
        {
            "id": binding.id,
            "project_id": binding.project_id,
            "backend_id": binding.backend_id,
            "display_name": binding.display_name,
            "native_scope_ref": binding.native_scope_ref,
            "enabled": binding.enabled,
            "is_default": binding.is_default,
            "runtime_config": binding.runtime_config,
        }
        for binding in actual
        if binding.origin == "imported"
    ]

    diff = build_domain_diff(
        expected_projects=[],
        expected_bindings=expected,
        actual_projects=[],
        actual_bindings=actual,
    ).to_wire()

    assert diff["bindings"]["domainOwned"] == [created["id"]]
    assert diff["bindings"]["unexpectedInDb"] == []
    assert diff["bindings"]["inSync"] is True


def test_a_real_drift_is_still_unexpected(harness):
    """兜底：origin=imported 的多余行照样报 unexpected，这条闸没被放松。"""
    stray = AgentBinding.create(project="workbench", backend="other")

    diff = build_domain_diff(
        expected_projects=[],
        expected_bindings=[],
        actual_projects=[],
        actual_bindings=[stray],
    ).to_wire()

    assert diff["bindings"]["unexpectedInDb"] == [stray.id]
    assert diff["bindings"]["inSync"] is False


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("patch", "/api/bindings/binding:workbench:mock"),
        ("post", "/api/bindings/binding:workbench:mock/make-default"),
        ("post", "/api/projects/project:workbench/bindings"),
        ("delete", "/api/bindings/binding:workbench:mock"),
    ],
)
def test_every_write_endpoint_requires_a_token(harness, method, path):
    with TestClient(harness.app) as anonymous:
        # `TestClient.delete` 不收 json=，用 request() 统一走一条路。
        response = anonymous.request(method.upper(), path, json={})
        assert response.status_code == 401, response.text
