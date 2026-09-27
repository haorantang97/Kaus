"""批次十七（接入层 + Session Host）：运行态自愈 / interrupt / 兜底 500 / runState。

对应任务书：

- 第 1 件：AD-136 三处自愈（进程启动、``GET /conversations/{id}``、SSE 附着）；
- 第 2 件：``POST /interrupt`` 无活跃 runtime 回 200 并顺带自愈；
- 第 3 件：漏网异常 → 结构化 500 ``internal_error``；
- 第 4 件：``GET /backends/{id}/debug/last-history`` 只在 ``DASH_DEBUG=1`` 时挂载；
- 第 5 件：``runState``（idle | running | stopping-unconfirmed）。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 或本文件里的桩，不碰任何
真实引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.api.session_views import debug_shape  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402

# SSE 探针与 ``test_session_router`` 共用一份：那里已经把「直接驱动 ASGI 应用、
# 收到 response.start 才算挂上」这件事做对了，再抄一遍只会有两份会漂移的实现。
from app.tests.test_session_router import SseProbe  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_envelope import RunStarted  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import RUNTIME_LOST_CODE, SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch17"


class Harness:
    def __init__(self, tmp_path: Path, *, driver=None, debug: bool = False) -> None:
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
            # 用例不该为了看一条「没确认」的提示等满 5 秒。
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
                debug_endpoints=debug,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(
            Backend.create(
                key=self.driver.backend_id.split(":", 1)[1],
                display_name="Mock",
                driver_kind=self.driver.driver_kind,
            )
        )
        project = await repos.projects.save(
            Project.create(slug=SLUG, display_name="批次十七")
        )
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend=self.driver.backend_id.split(":", 1)[1],
                display_name="绑定",
                is_default=True,
            )
        )
        self.project_id = project.id

    async def new_conversation(self, **fields) -> Conversation:
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
                **fields,
            )
        )

    async def stuck_run(self, run_id: str = "run-stuck") -> Conversation:
        """造一条「时间线停在运行中、后端没有 runtime」的会话。

        这正是真机现象的最小复现：网关被停的那一轮只有 ``run.started``，之后
        一条终态都没有（`docs/quality/verify5.md` ②）。
        """
        conversation = await self.new_conversation()
        await self.host.emit_conversation_event(
            conversation, RunStarted(run_id=run_id), run_id=run_id
        )
        saved = await self.repositories.conversations.save(
            conversation.evolve(state="running-card")
        )
        return saved

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
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {harness.token}"}
    ) as test_client:
        yield test_client


def _run_failed(envelopes):
    return [e for e in envelopes if e.event.type == "run.failed"]


# --------------------------------------------------------------------------- #
# 第 1 件：三处自愈
# --------------------------------------------------------------------------- #


async def test_startup_reconcile_converges_a_stuck_run(tmp_path):
    """进程启动：落盘时还停在 ``running-card`` 的会话被收敛为 idle。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.stuck_run()
        healed = await built.host.reconcile_startup()
        assert healed == (conversation.id,)
        failures = _run_failed(await built.host.event_store.replay(conversation.id))
        assert len(failures) == 1
        assert failures[0].event.error.code == RUNTIME_LOST_CODE
        assert failures[0].event.run_id == "run-stuck"
        again = await built.repositories.conversations.get(conversation.id)
        assert again.state == "idle"
        # 幂等：再跑一遍不会再补一条（这一轮已经是终态了）。
        assert await built.host.reconcile_startup() == ()
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


def test_get_conversation_reconciles_the_timeline(harness, client):
    """取会话：时间线当场收敛，``runState`` 回 idle，不再是「运行中」。"""
    conversation = asyncio.run(harness.stuck_run())
    body = client.get(f"/api/conversations/{conversation.id}").json()
    assert body["runState"] == "idle"
    assert body["timeline"]["runState"] == "failed"
    assert body["timeline"]["error"]["code"] == RUNTIME_LOST_CODE
    assert body["conversation"]["state"] == "idle"


async def test_sse_attach_reconciles_and_replays_the_synthesized_terminal(tmp_path):
    """附着事件流：合成的终态排在订阅之前入库，因此重放拿得到它。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.stuck_run()
        async with SseProbe(
            built.app, f"/api/conversations/{conversation.id}/events"
        ) as probe:
            assert probe.status == 200
            events = await probe.next_events(2)
        assert [event["event"]["type"] for event in events] == [
            "run.started",
            "run.failed",
        ]
        assert events[1]["event"]["error"]["code"] == RUNTIME_LOST_CODE
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


async def test_reconcile_leaves_an_externally_held_conversation_alone(tmp_path):
    """外部终端持有写权时不动：那一轮真的可能在别处跑着（AD-122）。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.stuck_run()
        await built.host.leases.try_acquire_external_cli(
            conversation.id, owner_id="terminal-1"
        )
        assert await built.host.reconcile_conversation(conversation) is False
        assert _run_failed(await built.host.event_store.replay(conversation.id)) == []
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


# --------------------------------------------------------------------------- #
# 第 2 件：interrupt 无 runtime
# --------------------------------------------------------------------------- #


def test_interrupt_without_a_runtime_heals_instead_of_failing(harness, client):
    """走查 ②：点停止不再回「没有活跃 runtime」，而是 200 + 时间线收敛。"""
    conversation = asyncio.run(harness.stuck_run())
    response = client.post(f"/api/conversations/{conversation.id}/interrupt")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "conversationId": conversation.id,
        "interrupted": False,
        "active": False,
        "reconciled": True,
        "synthesizedRunFailed": True,
    }
    assert (
        client.get(f"/api/conversations/{conversation.id}").json()["timeline"][
            "runState"
        ]
        == "failed"
    )


# --------------------------------------------------------------------------- #
# 第 5 件：runState
# --------------------------------------------------------------------------- #


class _DeafDriver(MockDriver):
    """点了停止什么都不做的引擎——「正在停止」这一档的最小复现。"""

    async def interrupt(self, runtime):  # type: ignore[override]
        return None


async def test_run_state_running_then_stopping_unconfirmed(tmp_path):
    """跑着 = running；点过停止而引擎没确认 = stopping-unconfirmed。"""
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
        await built.host.interrupt(conversation.id)
        assert await built.host.run_state_of(conversation) == "stopping-unconfirmed"
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


def test_get_conversation_reports_run_state_idle_for_a_fresh_one(harness, client):
    conversation = asyncio.run(harness.new_conversation())
    assert client.get(f"/api/conversations/{conversation.id}").json()["runState"] == "idle"


# --------------------------------------------------------------------------- #
# 第 3 件：兜底 500
# --------------------------------------------------------------------------- #


def test_an_unexpected_exception_becomes_a_structured_500(tmp_path):
    """漏网异常也要有形状：``internal_error`` + requestId，且**不**带异常原文。"""

    class _Exploding(MockDriver):
        async def get_capabilities(self):  # type: ignore[override]
            raise RuntimeError("敏感内容-不该上 wire")

    built = Harness(tmp_path, driver=_Exploding())
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.patch(
                f"/api/conversations/{conversation.id}", json={"modelId": "mock-large"}
            )
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "internal_error"
        assert error["message"] == "服务端出错：RuntimeError"
        assert "敏感内容" not in json.dumps(error, ensure_ascii=False)
        assert len(error["requestId"]) == 12
    finally:
        built.close()


# --------------------------------------------------------------------------- #
# 第 4 件：取证端点
# --------------------------------------------------------------------------- #


class _RecordingDriver(MockDriver):
    """带一份「最近一次原生历史响应」的假 Driver。"""

    payload = {
        "object": "list",
        "data": [
            {
                "id": "10",
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "terminal"}}],
                "content": "x" * 200,
            }
        ],
        "api_key": "super-secret-value",
    }

    def debug_last_history_payload(self, native_session_id=None):
        del native_session_id
        return self.payload


def test_debug_endpoint_is_not_mounted_by_default(tmp_path):
    """默认不挂：取证面不该长在普通部署上。"""
    built = Harness(tmp_path, driver=_RecordingDriver())
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/backends/{built.driver.backend_id}/debug/last-history"
            )
        assert response.status_code == 404
    finally:
        built.close()


def test_debug_endpoint_returns_a_redacted_shape(tmp_path):
    """挂上之后只回形状：键名 + 值类型 + 截断 80 字；像凭据的键名连值都不给。"""
    built = Harness(tmp_path, driver=_RecordingDriver(), debug=True)
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/backends/{built.driver.backend_id}/debug/last-history",
                params={"conversation": conversation.id},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["captured"] is True
        keys = body["shape"]["keys"]
        assert keys["api_key"] == {"type": "redacted"}
        row = keys["data"]["items"][0]["keys"]
        assert row["tool_calls"]["type"] == "array"
        assert row["content"]["truncated"] is True
        assert len(row["content"]["preview"]) == 80
        # 原始正文不得整段上 wire。
        assert "x" * 100 not in json.dumps(body)
    finally:
        built.close()


def test_debug_shape_stops_at_a_sane_depth():
    """嵌套过深时说「不展开了」，而不是把整份响应转储出去。"""
    nested = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    shape = debug_shape(nested)
    leaf = shape["keys"]["a"]["keys"]["b"]["keys"]["c"]["keys"]["d"]
    assert leaf["note"].startswith("嵌套过深")
