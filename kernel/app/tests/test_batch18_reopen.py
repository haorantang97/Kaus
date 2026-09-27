"""批次十八第 1 件：会话「重开为空」（AD-143）。

真机现象（`docs/quality/verify-next.md` ②③）：新建会话发一条消息，跑完或运行中
重新打开页面，**消息内容为空**；而同一条会话的原生历史里内容都在。前端附着事件
流时 ``?after=`` 为空（全量重放），所以问题只可能在后端这一侧。

这份用例把「重开」拆成前端实际做的两步——``GET /api/conversations/{id}`` 之后
不带 ``?after=`` 地附 SSE——覆盖三种时机（跑完后 / 运行中 / interrupt 之后），
外加两条**根因回归**：

- 缓冲被清空（保留期到期 / 上一个进程没落库 / ``DELETE`` 清过）之后，重开必须
  从原生历史重建，而不是给一张白纸（v1.0 §11.3）；
- ``kaus/user.message`` 不再按 24 小时诊断保留期清理（AD-86 的正文走的是
  ``extension.event`` 这条道，那是编码细节，不是「它是诊断数据」）。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 或本文件里的桩，不碰任何
真实引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from app.tests.test_session_router import SseProbe  # noqa: E402
from drivers.base import NativeHistory, NativeHistoryEntry  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch18"


class Harness:
    """SQLite 落盘 + 会话路由 + 一个由脚本驱动的 Driver。"""

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

    def client(self) -> "httpx.AsyncClient":
        """直接驱动 ASGI 应用：**同一个事件循环**。

        不用 ``TestClient``——它把应用跑在另一个线程的另一个循环里，退出时会把
        Session Host 的事件泵任务一起带走，那是测试装置造成的「事件消失」，
        会盖住真正要复现的那一个。
        """
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.token}"},
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
            Project.create(slug=SLUG, display_name="批次十八")
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

    async def aclose(self) -> None:
        await self.host.aclose()
        self.unit_of_work.close()


def quick_driver() -> MockDriver:
    """一轮跑完就停的引擎：``full_lifecycle`` 会停在审批上，重开用例不需要那段。"""
    from drivers.mock.fixtures import text_stream_script

    return MockDriver(script_factory=text_stream_script)


async def _wait_for(host: SessionHost, conversation_id: str, predicate) -> None:
    for _ in range(600):
        envelopes = await host.event_store.replay(conversation_id)
        if predicate(envelopes):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到期望的事件")


def _types(payloads) -> list[str]:
    return [payload["event"]["type"] for payload in payloads]


async def _reopen(built: Harness, conversation_id: str, *, expect: int):
    """模拟「重新打开页面」：先取会话，再不带 ``after`` 附 SSE 全量重放。"""
    async with built.client() as client:
        detail = await client.get(f"/api/conversations/{conversation_id}")
    assert detail.status_code == 200
    async with SseProbe(
        built.app, f"/api/conversations/{conversation_id}/events"
    ) as probe:
        assert probe.status == 200
        return detail.json(), await probe.next_events(expect)


# --------------------------------------------------------------------------- #
# 三种重开时机
# --------------------------------------------------------------------------- #


async def test_reopen_after_a_finished_run_replays_everything(tmp_path):
    """跑完之后重开：``kaus/user.message`` 与整轮 ``run.*`` / ``message.*`` 都在。"""
    built = Harness(tmp_path, driver=quick_driver())
    await built.seed()
    try:
        conversation = await built.new_conversation()
        async with built.client() as client:
            accepted = await client.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "跑一轮"},
            )
        assert accepted.status_code == 202
        await _wait_for(
            built.host,
            conversation.id,
            lambda es: any(e.event.type == "run.completed" for e in es),
        )
        stored = await built.host.event_store.replay(conversation.id)
        detail, replayed = await _reopen(built, conversation.id, expect=len(stored))
        assert detail["runState"] == "idle"
        kinds = _types(replayed)
        assert "extension.event" in kinds, "用户那句话不见了"
        assert "run.started" in kinds and "run.completed" in kinds
        assert any(kind.startswith("message.") for kind in kinds)
        assert len(replayed) == len(stored)
    finally:
        await built.aclose()


async def test_reopen_while_the_run_is_still_going(tmp_path):
    """运行中重开：发完消息立刻 GET + 订阅，已经发生的那几条必须重放得到。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation()
        async with built.client() as client:
            accepted = await client.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "跑一轮"},
            )
        assert accepted.status_code == 202
        await _wait_for(
            built.host,
            conversation.id,
            lambda es: any(e.event.type == "run.started" for e in es),
        )
        detail, replayed = await _reopen(built, conversation.id, expect=2)
        # 运行中不该被自愈误判成「引擎没了」。
        assert detail["runState"] in ("running", "stopping-unconfirmed")
        assert detail["timeline"]["runState"] == "running"
        assert _types(replayed)[:2] == ["extension.event", "session.created"]
    finally:
        await built.aclose()


async def test_reopen_after_an_interrupt_keeps_the_whole_timeline(tmp_path):
    """interrupt 之后重开：自愈路径不得把这条会话的缓冲清掉。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation()
        async with built.client() as client:
            await client.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "跑一轮"},
            )
            await _wait_for(
                built.host,
                conversation.id,
                lambda es: any(e.event.type == "run.started" for e in es),
            )
            before = len(await built.host.event_store.replay(conversation.id))
            stopped = await client.post(
                f"/api/conversations/{conversation.id}/interrupt"
            )
        assert stopped.status_code == 200
        after = await built.host.event_store.replay(conversation.id)
        assert len(after) >= before, "interrupt 把已经落库的事件弄少了"
        detail, replayed = await _reopen(built, conversation.id, expect=before)
        assert "extension.event" in _types(replayed)
        assert detail["timeline"] is not None
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 根因一：缓冲被清空之后，从原生历史重建（v1.0 §11.3）
# --------------------------------------------------------------------------- #


class _HistoryDriver(MockDriver):
    """只回一份固定原生历史的引擎——重建那条路的最小夹具。"""

    history = NativeHistory(
        native_session_id="api_1788540169_d077ca1d",
        entries=(
            NativeHistoryEntry(
                entry_id="2540", role="user", kind="message", text="用终端执行 pwd"
            ),
            NativeHistoryEntry(
                entry_id="2541",
                role="assistant",
                kind="tool_call",
                text="",
                metadata={
                    "toolCalls": [
                        {
                            "id": "call_5mJFQcWjkPeHY0wNYmOjkjCW",
                            "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                        }
                    ]
                },
            ),
            NativeHistoryEntry(
                entry_id="2542",
                role="tool",
                kind="tool_result",
                text="/Users/x",
                metadata={"toolCallId": "call_5mJFQcWjkPeHY0wNYmOjkjCW", "toolName": "terminal"},
            ),
            NativeHistoryEntry(
                entry_id="2543", role="assistant", kind="message", text="当前目录是 /Users/x"
            ),
        ),
    )

    async def load_native_history(self, binding, native_session_id):  # type: ignore[override]
        del binding, native_session_id
        return self.history


async def test_an_emptied_buffer_is_rebuilt_from_the_native_history(tmp_path):
    """缓冲整表可删（D-16），删完重开必须从原生历史重建，而不是给一张白纸。"""
    built = Harness(tmp_path, driver=_HistoryDriver())
    await built.seed()
    try:
        conversation = await built.new_conversation(
            native_session_id="api_1788540169_d077ca1d"
        )
        assert await built.host.event_store.latest_sequence(conversation.id) is None
        detail, replayed = await _reopen(built, conversation.id, expect=7)
        kinds = _types(replayed)
        assert kinds == [
            "run.started",
            "extension.event",
            "tool.started",
            "tool.completed",
            "message.started",
            "message.completed",
            "run.completed",
        ]
        assert replayed[1]["event"]["data"]["text"] == "用终端执行 pwd"
        assert replayed[1]["event"]["data"]["restored"] is True
        assert replayed[2]["event"]["name"] == "terminal"
        assert replayed[3]["event"]["output"] == "/Users/x"
        assert replayed[5]["event"]["text"] == "当前目录是 /Users/x"
        # 重建出来的是已经发生过的历史：页头不该显示还在跑。
        assert detail["runState"] == "idle"
        assert detail["timeline"]["runState"] == "completed"
    finally:
        await built.aclose()


async def test_rebuilding_never_touches_a_conversation_that_still_has_events(tmp_path):
    """有一条缓冲事件都不重建——重建绝不覆盖真实事件流。"""
    built = Harness(tmp_path, driver=_HistoryDriver())
    await built.seed()
    try:
        conversation = await built.new_conversation(
            native_session_id="api_1788540169_d077ca1d"
        )
        from runtime.event_envelope import RunStarted

        await built.host.emit_conversation_event(
            conversation, RunStarted(run_id="run-real"), run_id="run-real"
        )
        assert await built.host.restore_from_native_history(conversation) == 0
        # 没绑原生 Session 的会话也不重建（无从读起）。
        naked = await built.new_conversation()
        assert await built.host.restore_from_native_history(naked) == 0
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 根因二：保留期把用户那句话当成诊断数据
# --------------------------------------------------------------------------- #


async def test_the_user_message_outlives_the_diagnostic_retention(tmp_path):
    """``kaus/user.message`` 走 7 天，``diagnostic.notice`` 走 24 小时。"""
    from datetime import datetime, timezone

    from runtime.event_envelope import DiagnosticNotice, ExtensionEvent
    from runtime.event_reducer import USER_MESSAGE_NAME, USER_MESSAGE_NAMESPACE

    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation()
        await built.host.emit_conversation_event(
            conversation,
            ExtensionEvent(
                namespace=USER_MESSAGE_NAMESPACE,
                name=USER_MESSAGE_NAME,
                data={"text": "我说的话", "role": "user", "attachmentCount": 0},
            ),
        )
        await built.host.emit_conversation_event(
            conversation, DiagnosticNotice(level="warn", message="一句提示")
        )
        later = datetime.now(tz=timezone.utc) + timedelta(hours=25)
        assert await built.host.purge_expired_events(now=later) == 1
        left = await built.host.event_store.replay(conversation.id)
        assert [e.event.type for e in left] == ["extension.event"]
        assert left[0].event.data["text"] == "我说的话"
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 根因三：订阅在重放途中被取消时吞掉整段历史
# --------------------------------------------------------------------------- #


async def test_a_cancelled_replay_does_not_swallow_the_backlog(tmp_path):
    """SSE 的 keepalive 用的是 ``asyncio.wait_for``：它取消一次不能丢历史。"""
    from runtime.event_envelope import RunStarted

    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation()
        await built.host.emit_conversation_event(
            conversation, RunStarted(run_id="run-1"), run_id="run-1"
        )
        subscription = built.host.subscribe(conversation.id)
        slow = built.host.event_store.replay
        started = asyncio.Event()

        async def _slow_replay(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.5)
            return await slow(*args, **kwargs)

        built.host.event_store.replay = _slow_replay  # type: ignore[method-assign]
        task = asyncio.create_task(subscription.__anext__())
        await started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        built.host.event_store.replay = slow  # type: ignore[method-assign]
        envelope = await asyncio.wait_for(subscription.__anext__(), timeout=2.0)
        assert envelope.event.type == "run.started"
        subscription.close()
    finally:
        await built.aclose()
