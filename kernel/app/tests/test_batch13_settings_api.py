"""批次十三：有效设置、审批档通用键、目录诚实标记、项目工作目录写端点。

覆盖
----
1. ``GET /api/bindings/{id}/effective-settings`` 的三种来源：``binding``
   （Binding 行自己写了）/ ``engine``（Kaus 没写、引擎那边有）/ ``none``
   （哪儿都没有 → ``value: null``，前端按 AD-71 不渲染）；
2. ``approval_mode``：合法值写得进、非法值 400、``reasoning_effort`` 按目录里
   该模型的 ``reasoning_levels`` 校验；
3. Model Catalog 的 wire 带 ``degraded`` 与 ``diagnostics``；
4. ``PATCH /api/projects/{id}``：合法 / 目录不存在 / 相对路径三例，
   外加 ``GET`` 一定带 ``workspaceRoot``。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver（引擎侧设置由一个只改
``read_engine_settings`` 的子类给出——公共层的解析逻辑不该依赖任何真引擎）。
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
from app.api.router import build_domain_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import EngineSettings  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"


class EngineAwareMockDriver(MockDriver):
    """引擎那边**有**设置的 Mock：只覆盖 ``read_engine_settings``。

    这正是「引擎自己的账」那一层在公共层的样子——公共层不认识任何一家引擎的
    配置文件，它只认识这个方法的返回值。
    """

    engine_settings: EngineSettings | None = None

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings:
        self._assert_binding(binding)
        if self.engine_settings is None:
            return await super().read_engine_settings(binding)
        return self.engine_settings.model_copy(update={"binding_id": binding.id})


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = EngineAwareMockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        auth_policy = SessionAuthPolicy(lambda: TEST_TOKEN)
        self.app = fastapi.FastAPI()
        self.app.include_router(build_domain_router(self.repositories))
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=auth_policy,
            )
        )
        self.app.include_router(
            build_binding_write_router(
                self.repositories,
                auth_policy=auth_policy,
                session_host=self.host,
                registry=self.registry,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", driver_kind="mock"))
        project = await repos.projects.save(Project.create(slug="workbench"))
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="mock", display_name="默认", is_default=True
            )
        )


@pytest.fixture()
def rig(tmp_path: Path):
    harness = Harness(tmp_path)
    asyncio.run(harness.seed())
    with TestClient(harness.app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        yield harness, client


def _settings(client: TestClient, binding_id: str) -> dict:
    response = client.get(f"/api/bindings/{binding_id}/effective-settings")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. 有效设置的三种来源
# --------------------------------------------------------------------------- #


def test_settings_from_binding_win_over_everything(rig) -> None:
    """Binding 行自己写了 → ``source="binding"``，引擎那边的值不参与。"""
    harness, client = rig
    harness.driver.engine_settings = EngineSettings(
        binding_id=harness.binding.id,
        model_id="mock-large",
        reasoning_effort="low",
        approval_mode="auto",
    )
    client.patch(
        f"/api/bindings/{harness.binding.id}",
        json={
            "defaultModelId": "mock-small",
            "runtimeConfig": {"reasoning_effort": "high", "approval_mode": "ask"},
        },
    ).raise_for_status()

    payload = _settings(client, harness.binding.id)
    assert payload["model"] == {"value": "mock-small", "source": "binding"}
    assert payload["reasoningEffort"]["value"] == "high"
    assert payload["reasoningEffort"]["source"] == "binding"
    assert payload["approvalMode"]["value"] == "ask"
    assert payload["approvalMode"]["source"] == "binding"
    # 下拉的选项与档位一起给出，前端不用自己写死词表。
    assert payload["approvalMode"]["options"] == ["ask", "auto", "deny"]
    assert payload["reasoningEffort"]["levels"] == ["low", "medium", "high"]


def test_settings_fall_back_to_the_engines_own_config(rig) -> None:
    """Binding 行是空的、引擎那边有值 → ``source="engine"``（真机上 Media 的现象）。"""
    harness, client = rig
    harness.driver.engine_settings = EngineSettings(
        binding_id=harness.binding.id,
        model_id="mock-large",
        reasoning_effort="xhigh",
        approval_mode="deny",
    )
    payload = _settings(client, harness.binding.id)
    assert payload["model"] == {"value": "mock-large", "source": "engine"}
    assert payload["reasoningEffort"]["value"] == "xhigh"
    assert payload["reasoningEffort"]["source"] == "engine"
    assert payload["approvalMode"]["value"] == "deny"
    assert payload["approvalMode"]["source"] == "engine"
    # levels 跟着**解析出来的**模型走，不是跟着 Binding 上那个空值走。
    assert payload["reasoningEffort"]["levels"] == ["low", "medium", "high", "xhigh"]


def test_missing_settings_are_null_with_source_none(rig) -> None:
    """哪儿都没有 → ``value: null, source: "none"``，**不编造默认值**（N §13.1）。"""
    harness, client = rig
    payload = _settings(client, harness.binding.id)
    assert payload["reasoningEffort"]["value"] is None
    assert payload["reasoningEffort"]["source"] == "none"
    assert payload["approvalMode"]["value"] is None
    assert payload["approvalMode"]["source"] == "none"
    assert payload["workspaceRoot"] == {"value": None, "source": "none"}
    # model 仍能从目录默认里得到一个值——那是第三来源，标 catalog。
    assert payload["model"]["source"] == "catalog"


def test_workspace_root_comes_from_the_project(rig, tmp_path: Path) -> None:
    harness, client = rig
    workspace = tmp_path / "media"
    workspace.mkdir()
    client.patch(
        f"/api/projects/{harness.project_id}", json={"workspaceRoot": str(workspace)}
    ).raise_for_status()
    payload = _settings(client, harness.binding.id)
    assert payload["workspaceRoot"] == {"value": str(workspace), "source": "project"}


def test_unknown_binding_is_404(rig) -> None:
    _, client = rig
    response = client.get("/api/bindings/binding:nope:mock/effective-settings")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "binding_not_found"


# --------------------------------------------------------------------------- #
# 2. approval_mode 与 reasoning_effort 的取值校验
# --------------------------------------------------------------------------- #


def test_approval_mode_is_a_generic_runtime_config_key(rig) -> None:
    harness, client = rig
    response = client.patch(
        f"/api/bindings/{harness.binding.id}",
        json={"runtimeConfig": {"approval_mode": "auto"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["runtimeConfig"]["approval_mode"] == "auto"


@pytest.mark.parametrize("value", ["manual", "smart", "off", "ALLOW", ""])
def test_engine_native_approval_values_are_rejected(rig, value: str) -> None:
    """只认通用词：引擎自己的取值（``manual`` 之流）由 Driver 映射，不从 wire 进。"""
    harness, client = rig
    response = client.patch(
        f"/api/bindings/{harness.binding.id}",
        json={"runtimeConfig": {"approval_mode": value}},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_approval_mode"
    assert body["allowedValues"] == ["ask", "auto", "deny"]


def test_reasoning_effort_is_validated_against_the_catalog(rig) -> None:
    """合法值 = 目录里**该模型**的 reasoning_levels。"""
    harness, client = rig
    # mock-small 只有 low/medium/high。
    assert (
        client.patch(
            f"/api/bindings/{harness.binding.id}",
            json={
                "defaultModelId": "mock-small",
                "runtimeConfig": {"reasoning_effort": "xhigh"},
            },
        ).status_code
        == 400
    )
    # 同一次 PATCH 里换到 mock-large，xhigh 就是合法的——校验看的是「写完之后
    # 库里会是什么」，不是库里现在是什么。
    ok = client.patch(
        f"/api/bindings/{harness.binding.id}",
        json={
            "defaultModelId": "mock-large",
            "runtimeConfig": {"reasoning_effort": "xhigh"},
        },
    )
    assert ok.status_code == 200, ok.text


def test_reasoning_effort_is_not_validated_without_a_catalog(tmp_path: Path) -> None:
    """没给 Registry（拿不到目录）→ 不校验，而不是一律拒绝。"""
    harness = Harness(tmp_path)
    asyncio.run(harness.seed())
    app = fastapi.FastAPI()
    app.include_router(
        build_binding_write_router(
            harness.repositories,
            auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
        )
    )
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        response = client.patch(
            f"/api/bindings/{harness.binding.id}",
            json={"runtimeConfig": {"reasoning_effort": "自定义档"}},
        )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 3. 目录的诚实标记上 wire
# --------------------------------------------------------------------------- #


def test_model_catalog_wire_carries_degraded_and_diagnostics(rig) -> None:
    harness, client = rig
    response = client.get(
        f"/api/backends/mock/models?binding={harness.binding.id}"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is False
    assert payload["diagnostics"] == []


# --------------------------------------------------------------------------- #
# 4. PATCH /api/projects/{id}
# --------------------------------------------------------------------------- #


def test_patch_project_sets_workspace_root(rig, tmp_path: Path) -> None:
    harness, client = rig
    workspace = tmp_path / "media"
    workspace.mkdir()
    response = client.patch(
        f"/api/projects/{harness.project_id}", json={"workspaceRoot": str(workspace)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["workspaceRoot"] == str(workspace)
    # 只读端点读得到同一个值（GET 一直带 workspaceRoot 这个键）。
    fetched = client.get(f"/api/projects/{harness.project_id}").json()
    assert fetched["workspaceRoot"] == str(workspace)


def test_patch_project_rejects_a_missing_directory(rig, tmp_path: Path) -> None:
    harness, client = rig
    response = client.patch(
        f"/api/projects/{harness.project_id}",
        json={"workspaceRoot": str(tmp_path / "nope")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "workspace_root_not_found"


def test_patch_project_rejects_a_relative_path(rig) -> None:
    harness, client = rig
    response = client.patch(
        f"/api/projects/{harness.project_id}", json={"workspaceRoot": "./media"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "workspace_root_not_absolute"


def test_patch_project_rejects_a_file_and_other_fields(rig, tmp_path: Path) -> None:
    harness, client = rig
    a_file = tmp_path / "a.txt"
    a_file.write_text("x", encoding="utf-8")
    assert (
        client.patch(
            f"/api/projects/{harness.project_id}", json={"workspaceRoot": str(a_file)}
        ).json()["error"]["code"]
        == "workspace_root_not_a_directory"
    )
    # 只接受 workspaceRoot：多给一个字段是 422，不被静默忽略。
    assert (
        client.patch(
            f"/api/projects/{harness.project_id}",
            json={"workspaceRoot": str(tmp_path), "displayName": "改名"},
        ).status_code
        == 422
    )


def test_patch_unknown_project_is_404(rig, tmp_path: Path) -> None:
    _, client = rig
    response = client.patch(
        "/api/projects/nope", json={"workspaceRoot": str(tmp_path)}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_project_write_endpoint_requires_auth(rig, tmp_path: Path) -> None:
    harness, client = rig
    response = client.patch(
        f"/api/projects/{harness.project_id}",
        json={"workspaceRoot": str(tmp_path)},
        headers={"Authorization": ""},
    )
    assert response.status_code == 401
