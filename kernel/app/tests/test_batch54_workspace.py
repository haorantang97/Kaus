"""「工作目录」只有一把尺子（批次五十四 PJ-01 / AD-175）。

真机上 K5 不过的根因：用户在**项目**上设了工作目录、Binding 上没设，于是起会话
那条路（只读 Binding）拿到 ``None``，引擎在后端进程的启动目录里起来，读了**本仓
自己的** ``AGENTS.md`` 并照着回答——不是报错，是静默走错。

修在接入层：AD-58 的那一次 ``register_binding`` 灌入之前，先用项目的
``workspace_root`` 把 Binding 快照上空着的那一格补上
（:func:`~app.projects.binding_snapshot.register_binding_snapshot`）。
Driver 读 ``runtime_config.workspace_root`` 的契约一个字不改。

这一份覆盖三层：
1. 纯函数口径（Binding 优先、项目回落、两边都没有就是没有）；
2. 会话路由（``POST /conversations/{id}/messages`` 那条灌入点）；
3. 组路由（``POST /groups/{id}/spawn`` 的首句那条灌入点）。

「起会话真的用上了那个目录」那一条在
``drivers/acp/tests/test_batch54_session_cwd.py``（端到端，真子进程）。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 的子类，不起任何真引擎、
不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.api.group_router import build_group_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.binding_snapshot import (  # noqa: E402
    binding_with_project_workspace,
    register_binding_snapshot,
)
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch54"
PROJECT_WORKSPACE = "/tmp/batch54-project-workspace"
BINDING_WORKSPACE = "/tmp/batch54-binding-workspace"


# --------------------------------------------------------------------------- #
# 1. 口径本身（纯函数）
# --------------------------------------------------------------------------- #


def _project(workspace_root: str | None) -> Project:
    return Project.create(
        slug=SLUG, display_name="批次五十四", workspace_root=workspace_root
    )


def _binding(project: Project, runtime_config: dict) -> AgentBinding:
    return AgentBinding.create(
        project=project, backend="mock", display_name="绑定", runtime_config=runtime_config
    )


def test_the_project_workspace_fills_an_empty_binding() -> None:
    project = _project(PROJECT_WORKSPACE)
    binding = _binding(project, {"approval_mode": "ask"})
    snapshot = binding_with_project_workspace(binding, project)
    assert snapshot.runtime_config["workspace_root"] == PROJECT_WORKSPACE
    # 别的键原样带着走，原实例不被改动。
    assert snapshot.runtime_config["approval_mode"] == "ask"
    assert "workspace_root" not in binding.runtime_config


def test_the_binding_wins_when_both_are_set() -> None:
    """AD-152：Binding 上那一个是「用户授权了哪个目录」，更具体，优先。"""
    project = _project(PROJECT_WORKSPACE)
    binding = _binding(project, {"workspace_root": BINDING_WORKSPACE})
    snapshot = binding_with_project_workspace(binding, project)
    assert snapshot.runtime_config["workspace_root"] == BINDING_WORKSPACE


def test_neither_side_set_stays_none() -> None:
    """两边都没有就**仍然是没有**——不拿进程 cwd 顶替（那正是 K5 的坑）。"""
    project = _project(None)
    binding = _binding(project, {})
    snapshot = binding_with_project_workspace(binding, project)
    assert "workspace_root" not in snapshot.runtime_config
    assert snapshot is binding


def test_a_blank_binding_value_counts_as_unset() -> None:
    project = _project(PROJECT_WORKSPACE)
    binding = _binding(project, {"workspace_root": "   "})
    snapshot = binding_with_project_workspace(binding, project)
    assert snapshot.runtime_config["workspace_root"] == PROJECT_WORKSPACE


def test_a_missing_project_is_not_an_error() -> None:
    project = _project(PROJECT_WORKSPACE)
    binding = _binding(project, {})
    assert binding_with_project_workspace(binding, None) is binding


async def test_a_driver_without_register_binding_is_left_alone() -> None:
    """Mock Driver 没有这个方法：算出快照、什么都不灌，不抛。"""

    class Projects:
        async def get(self, _project_id: str) -> Project:
            return _project(PROJECT_WORKSPACE)

    project = _project(PROJECT_WORKSPACE)
    binding = _binding(project, {})
    snapshot = await register_binding_snapshot(object(), binding, Projects())
    assert snapshot.runtime_config["workspace_root"] == PROJECT_WORKSPACE


# --------------------------------------------------------------------------- #
# 2. 两条路由真的灌了补过的那一份
# --------------------------------------------------------------------------- #


class RecordingDriver(MockDriver):
    """MockDriver + ``register_binding``：把接入层灌进来的那一份原样留下。

    真机上的 ACP Driver 正是从这里读 ``runtime_config.workspace_root``
    （``AcpDriver._workspace_root_for``），所以「灌进去的是哪一份」就等价于
    「引擎会在哪个目录里起来」。
    """

    def __init__(self) -> None:
        super().__init__()
        self.registered: list[AgentBinding] = []

    def register_binding(self, binding: AgentBinding) -> None:
        self.registered.append(binding)

    @property
    def last_workspace_root(self) -> str | None:
        assert self.registered, "接入层一次都没灌"
        raw = self.registered[-1].runtime_config.get("workspace_root")
        return raw if isinstance(raw, str) else None


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = RecordingDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.auth_policy = SessionAuthPolicy(lambda: TEST_TOKEN)
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=self.auth_policy,
                run_id_timeout=5.0,
            )
        )
        self.app.include_router(
            build_group_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=self.auth_policy,
                keepalive_interval=0.2,
                run_id_timeout=5.0,
            )
        )

    async def seed(
        self, *, project_workspace: str | None, binding_workspace: str | None
    ) -> None:
        repos = self.repositories
        key = self.driver.backend_id.split(":", 1)[1]
        await repos.backends.save(
            Backend.create(
                key=key, display_name="Mock", driver_kind=self.driver.driver_kind
            )
        )
        project = await repos.projects.save(
            Project.create(
                slug=SLUG, display_name="批次五十四", workspace_root=project_workspace
            )
        )
        self.project_id = project.id
        runtime_config: dict[str, object] = {"approval_mode": "ask"}
        if binding_workspace is not None:
            runtime_config["workspace_root"] = binding_workspace
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend=key,
                display_name="绑定",
                is_default=True,
                runtime_config=runtime_config,
            )
        )

    async def conversation(self) -> Conversation:
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
            )
        )

    async def aclose(self) -> None:
        await self.host.aclose()
        self.unit_of_work.close()


def _api(built: Harness):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built.app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )


async def _built(
    tmp_path: Path, *, project_workspace: str | None, binding_workspace: str | None
) -> Harness:
    built = Harness(tmp_path)
    await built.seed(
        project_workspace=project_workspace, binding_workspace=binding_workspace
    )
    return built


async def test_session_router_hands_the_driver_the_project_workspace(tmp_path) -> None:
    """K5 的接入层等价物：项目设了、Binding 没设 → Driver 拿到项目那一个。"""
    built = await _built(
        tmp_path, project_workspace=PROJECT_WORKSPACE, binding_workspace=None
    )
    try:
        conversation = await built.conversation()
        async with _api(built) as api:
            response = await api.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "第一句"},
            )
            assert response.status_code in (200, 201, 202), response.text
        assert built.driver.last_workspace_root == PROJECT_WORKSPACE
    finally:
        await built.aclose()


async def test_session_router_keeps_the_binding_value(tmp_path) -> None:
    built = await _built(
        tmp_path,
        project_workspace=PROJECT_WORKSPACE,
        binding_workspace=BINDING_WORKSPACE,
    )
    try:
        conversation = await built.conversation()
        async with _api(built) as api:
            await api.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "第一句"},
            )
        assert built.driver.last_workspace_root == BINDING_WORKSPACE
    finally:
        await built.aclose()


async def test_session_router_leaves_it_unset_when_nobody_set_it(tmp_path) -> None:
    built = await _built(tmp_path, project_workspace=None, binding_workspace=None)
    try:
        conversation = await built.conversation()
        async with _api(built) as api:
            await api.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "第一句"},
            )
        assert built.driver.last_workspace_root is None
    finally:
        await built.aclose()


async def test_group_router_hands_the_driver_the_project_workspace(tmp_path) -> None:
    """组里替用户按的那一次「发送」走的是同一个函数。"""
    built = await _built(
        tmp_path, project_workspace=PROJECT_WORKSPACE, binding_workspace=None
    )
    try:
        async with _api(built) as api:
            group = (await api.post("/api/groups", json={"title": "组"})).json()
            response = await api.post(
                f"/api/groups/{group['id']}/spawn",
                json={
                    "projectId": built.project_id,
                    "bindingId": built.binding.id,
                    "initialMessage": "先说第一句",
                },
            )
            assert response.status_code == 201, response.text
        assert built.driver.last_workspace_root == PROJECT_WORKSPACE
    finally:
        await built.aclose()
