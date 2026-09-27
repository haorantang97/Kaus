"""批次三十五（AD-159）第一件在接入层的落法：进程没拉起来要说人话。

Driver 抛
:class:`~drivers.base.AgentSpawnError` 时，``POST /conversations/{id}/messages``
回 503 ``agent_spawn_failed`` 并把修法放进 ``detail.hint``——而不是把它和「网关
不可达」一起压成 ``runtime_start_failed``。真机上这个混淆的代价是一整夜：仪表盘
只显示一个空目录，用户不知道要去终端手动跑一次那条命令。

另一件（``state`` 与 ``runState`` 的单一真源）在
``app/tests/test_batch35_run_state.py``。

隔离：SQLite 建在 ``tmp_path``，Driver 是本文件里的桩或 MockDriver，不起任何真
引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.group_views import DELIVERY_REASONS  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import (  # noqa: E402
    AGENT_SPAWN_FAILED,
    AgentSpawnError,
    FailureHint,
)
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch35"

SPAWN_MESSAGE = "引擎进程没能拉起来：/nowhere/fake-agent — ENOENT (2)：这个可执行文件不存在"
SPAWN_HINT = "在终端手动执行同一条命令看它能否启动"


class UnspawnableDriver(MockDriver):
    """一台「命令根本执行不了」的引擎。

    只覆盖 ``start_runtime``：本文件测的是**接入层怎么翻译这个异常**，
    Driver 怎么认出它在 ``drivers/acp/tests/test_batch35_spawn_hint.py``。
    """

    async def start_runtime(self, conversation, surface="card"):  # noqa: ANN001
        raise AgentSpawnError(
            SPAWN_MESSAGE,
            failure=FailureHint(
                code=AGENT_SPAWN_FAILED, message=SPAWN_MESSAGE, hint=SPAWN_HINT
            ),
        )


class Harness:
    def __init__(self, tmp_path: Path, *, driver=None) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = driver or MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
            interrupt_confirm_timeout=0.2,
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

    async def seed(self) -> None:
        repos = self.repositories
        key = self.driver.backend_id.split(":", 1)[1]
        await repos.backends.save(
            Backend.create(
                key=key, display_name="Mock", driver_kind=self.driver.driver_kind
            )
        )
        project = await repos.projects.save(
            Project.create(slug=SLUG, display_name="批次三十五")
        )
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend=key, display_name="绑定", is_default=True
            )
        )

    async def new_conversation(self, **fields) -> Conversation:
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
                **fields,
            )
        )

    def close(self) -> None:
        asyncio.run(self.host.aclose())
        self.unit_of_work.close()


def _client(built: Harness) -> TestClient:
    return TestClient(built.app, headers={"Authorization": f"Bearer {built.token}"})


# --------------------------------------------------------------------------- #
# 第一件：503 agent_spawn_failed
# --------------------------------------------------------------------------- #


@pytest.fixture()
def spawn_harness(tmp_path):
    built = Harness(tmp_path, driver=UnspawnableDriver())
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


def test_send_message_returns_503_agent_spawn_failed_with_the_hint(
    spawn_harness,
) -> None:
    """不是笼统的 ``runtime_start_failed``：码、人话、修法三样齐全。"""
    conversation = asyncio.run(spawn_harness.new_conversation())
    with _client(spawn_harness) as client:
        response = client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "你好"}
        )
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == AGENT_SPAWN_FAILED
    assert body["message"] == SPAWN_MESSAGE
    assert body["detail"]["cause"] == AGENT_SPAWN_FAILED
    assert body["detail"]["hint"] == SPAWN_HINT
    # 批次十一第 2 件的老约定还在：这条会话至今是空的，前端可以直接把空壳删掉。
    assert body["emptyConversation"] is True


def test_agent_spawn_failed_is_a_known_group_delivery_reason() -> None:
    """组投递那一行也要能显示这个可修的状态，而不是压回「引擎没能起来」。"""
    assert AGENT_SPAWN_FAILED in DELIVERY_REASONS
