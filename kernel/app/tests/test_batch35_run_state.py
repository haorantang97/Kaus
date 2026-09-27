"""批次三十五（AD-159）第二件：``conversation.state`` 与 ``runState`` 不许打架。

真机（VERIFY-BATCH-31 / VERIFY-BATCH-34）上 ``GET /api/conversations/{id}`` 同时
给出 ``state="running-card"`` 与 ``runState="idle"``。旧字段保留（老客户端还在
读），但它从此是 ``runState`` 的投影：读的时候导出，跑完的时候落库。

另一件（进程拉起失败的人话）在
``app/tests/test_batch35_spawn_hint_api.py``。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 与本文件里的桩，不起任何真
引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.api.session_views import derive_conversation_state  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.mock.fixtures import text_stream_script  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_envelope import RunStarted  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch35-state"

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
# 第二件：state 与 runState 是同一个事实
# --------------------------------------------------------------------------- #


@pytest.fixture()
def harness(tmp_path):
    # 默认剧本停在一次待批准的交互上（那一轮永远不会自己收敛）；这里要的是
    # 「跑完一轮」，所以换成纯文本剧本。
    built = Harness(tmp_path, driver=MockDriver(script_factory=text_stream_script))
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


def test_state_is_idle_after_a_run_completed(harness) -> None:
    """跑完一轮之后：``runState=idle``，旧字段也必须是 ``idle``（真机那条小尾巴）。"""
    conversation = asyncio.run(harness.new_conversation())
    with _client(harness) as client:
        sent = client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "ping"}
        )
        assert sent.status_code == 202
        body = _poll_until_idle(client, conversation.id)
    assert body["runState"] == "idle"
    assert body["conversation"]["state"] == "idle"
    # 落库的那一半也跟上了：下一个进程、侧栏刷新读到的是同一个事实。
    stored = asyncio.run(harness.repositories.conversations.get(conversation.id))
    assert stored.state == "idle"


async def test_state_is_running_card_while_the_run_is_running(tmp_path) -> None:
    """反过来也要成立：真的在跑的时候，旧字段是 ``running-card``。"""
    built = Harness(tmp_path, driver=_DeafDriver())
    await built.seed()
    try:
        conversation = await built.new_conversation()
        await built.host.start_runtime(conversation)
        await built.host.send_message(conversation.id, "跑起来")
        for _ in range(200):
            if built.host.timeline(conversation.id).run_state == "running":
                break
            await asyncio.sleep(0.01)
        assert await built.host.run_state_of(conversation) == "running"
        latest = await built.repositories.conversations.get(conversation.id)
        assert latest.state == "running-card"
        assert derive_conversation_state(latest.state, "running") == "running-card"
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


def test_state_follows_run_state_after_a_lost_terminal_is_reconciled(harness) -> None:
    """自愈之后不许留下矛盾：库里写着 ``running-card``、``runState`` 是 idle。

    这正是真机 AD-136 那种失配的残留形状：上一个进程带着 runtime 没了，库里那一行
    还停在 ``running-card``。读一次会话应该同时收敛两边。
    """
    conversation = asyncio.run(harness.new_conversation())
    asyncio.run(
        harness.host.emit_conversation_event(
            conversation, RunStarted(run_id="run-lost"), run_id="run-lost"
        )
    )
    stuck = asyncio.run(
        harness.repositories.conversations.save(
            conversation.evolve(state="running-card")
        )
    )
    assert stuck.state == "running-card"
    with _client(harness) as client:
        body = client.get(f"/api/conversations/{conversation.id}").json()
    assert body["runState"] == "idle"
    assert body["conversation"]["state"] == "idle"


def test_non_card_states_are_left_alone() -> None:
    """``runState`` 只管 Card 这一档：终端持有写权、暂停、结束、出错都不归它。"""
    for state in ("running-external", "paused", "ended", "error"):
        assert derive_conversation_state(state, "idle") == state
        assert derive_conversation_state(state, "running") == state
    # 问不出来就不改（N §13.1）。
    assert derive_conversation_state("running-card", None) == "running-card"


class _DeafDriver(MockDriver):
    """点了停止什么都不做的引擎——让一轮**停在** running 的最小办法。"""

    async def interrupt(self, runtime):  # type: ignore[override]
        return None


def _poll_until_idle(client, conversation_id: str) -> dict:
    """轮询到这一轮真的收敛。

    每次之间要 ``sleep``：TestClient 是同步的，不让出线程的话后台那条事件泵
    根本没有机会跑完这一轮。
    """
    body: dict = {}
    for _ in range(300):
        body = client.get(f"/api/conversations/{conversation_id}").json()
        timeline = body["timeline"]
        if (
            body["runState"] == "idle"
            and timeline is not None
            and timeline["itemCounts"].get("message", 0) >= 1
        ):
            return body
        time.sleep(0.02)
    return body
