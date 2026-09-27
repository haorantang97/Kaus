"""``GET /api/conversations/{id}`` 的 ``projection``（批次四十三第 3 件）。

「让人看得见」：这条会话建起来的时候，到底有哪些项目 MCP 真的被送进了引擎。
**只有名字**——命令行、URL、环境变量的值一个字都不上 wire（HANDOFF §2 红线）。

``null`` 与 ``{}`` 不是一回事：前者是「这条路没走」（没装能力投影器、这台引擎没有
随会话送能力这条路、或这是一条续接会话），后者是「走了，一条都没有」。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver，不起任何真引擎、不读凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.capabilities.effective import resolve_effective_for_binding  # noqa: E402
from app.capabilities.models import ProjectCapability  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch43"
SECRET_COMMAND = "a-command-that-must-not-reach-the-wire"


class Harness:
    def __init__(self, tmp_path: Path, *, wire_projector: bool) -> None:
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
            idle_timeout=timedelta(minutes=15),
            capability_projector=(
                (
                    lambda binding: resolve_effective_for_binding(
                        self.repositories, binding
                    )
                )
                if wire_projector
                else None
            ),
        )
        self.token = TEST_TOKEN
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: self.token),
                run_id_timeout=5.0,
            )
        )

    async def seed(self, *, with_mcp: bool) -> None:
        repos = self.repositories
        key = self.driver.backend_id.split(":", 1)[1]
        await repos.backends.save(
            Backend.create(
                key=key, display_name="Mock", driver_kind=self.driver.driver_kind
            )
        )
        project = await repos.projects.save(
            Project.create(slug=SLUG, display_name="批次四十三")
        )
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend=key, display_name="绑定", is_default=True
            )
        )
        if with_mcp:
            await repos.capabilities.save(
                ProjectCapability.create(
                    project_id=project.id,
                    capability_type="mcp",
                    capability_id="files",
                    config={"value": {"command": SECRET_COMMAND}},
                )
            )

    async def new_conversation(self) -> Conversation:
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
            )
        )

    def close(self) -> None:
        asyncio.run(self.host.aclose())
        self.unit_of_work.close()


def _client(built: Harness) -> TestClient:
    return TestClient(built.app, headers={"Authorization": f"Bearer {built.token}"})


def _build(tmp_path: Path, *, wire_projector: bool, with_mcp: bool) -> Harness:
    built = Harness(tmp_path, wire_projector=wire_projector)
    asyncio.run(built.seed(with_mcp=with_mcp))
    return built


def test_projection_is_null_before_the_runtime_ever_started(tmp_path) -> None:
    """还没建过 runtime = 这条路没走过，如实回 null。"""
    built = _build(tmp_path, wire_projector=True, with_mcp=True)
    try:
        conversation = asyncio.run(built.new_conversation())
        with _client(built) as client:
            body = client.get(f"/api/conversations/{conversation.id}").json()
        assert body["projection"] is None
    finally:
        built.close()


def test_projection_lists_only_the_names(tmp_path) -> None:
    """建过 runtime 之后回得到名字——且**只有**名字。"""
    built = _build(tmp_path, wire_projector=True, with_mcp=True)
    try:
        conversation = asyncio.run(built.new_conversation())
        asyncio.run(built.host.start_runtime(conversation))
        with _client(built) as client:
            response = client.get(f"/api/conversations/{conversation.id}")
        body = response.json()
        assert body["projection"] == {"mcpServers": ["files"]}
        assert SECRET_COMMAND not in response.text
    finally:
        built.close()


def test_projection_stays_null_without_a_projector(tmp_path) -> None:
    """没装能力投影器 = 与本批之前一样：一个字都没送，如实回 null。"""
    built = _build(tmp_path, wire_projector=False, with_mcp=True)
    try:
        conversation = asyncio.run(built.new_conversation())
        asyncio.run(built.host.start_runtime(conversation))
        with _client(built) as client:
            body = client.get(f"/api/conversations/{conversation.id}").json()
        assert body["projection"] is None
    finally:
        built.close()


def test_projection_is_null_when_the_project_defines_no_mcp(tmp_path) -> None:
    """装了投影器但项目一条 MCP 都没定：没什么可送，仍然是 null。"""
    built = _build(tmp_path, wire_projector=True, with_mcp=False)
    try:
        conversation = asyncio.run(built.new_conversation())
        asyncio.run(built.host.start_runtime(conversation))
        with _client(built) as client:
            body = client.get(f"/api/conversations/{conversation.id}").json()
        assert body["projection"] is None
    finally:
        built.close()
