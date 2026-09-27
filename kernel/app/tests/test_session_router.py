"""Phase 3B 会话接入层的端到端测试（`app/api/session_router.py`）。

覆盖
----
1. **全部端点的正常路径**：列会话 / 建会话 / 看会话 / 发消息（202 + runId）/
   中断 / 答交互 / 停 Runtime / 原生历史 / Model Catalog；
2. **SSE 两种模式**：`?after=` 回放（Event Store）与开着流收实时事件，
   外加服务端 `: keepalive` 注释帧（AD-62）；
3. **审批往返**：`permission.requested` → `POST /interactions/{id}` →
   `permission.resolved`，剧本继续往下跑（N §13.4 的请求-响应闭环）；
4. **错误码**：每一条都是 `{"error": {"code", "message"}}`，code 稳定可分支；
5. **AD-41 后台任务**：`MaintenanceLoop.run_once` 把 `probe_state` 写回领域库。

隔离：SQLite 建在 `tmp_path`，Driver 是 MockDriver——不碰任何真实 Agent、
不读任何凭据。
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.api.session_views import SSE_PADDING_FRAME  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import InteractionResponse  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

PROJECT_SLUG = "workbench"
OTHER_SLUG = "other"

#: D-17：本文件验的是端点行为，不是鉴权，所以每个请求都带这把固定的测试 token。
TEST_TOKEN = "test-token-0123456789"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


class Harness:
    """一套「库 + Registry + Session Host + TestClient」。"""

    def __init__(self, tmp_path: Path, *, keepalive: float = 25.0) -> None:
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
        )
        # D-17：路由一律带鉴权，所以本文件里每个请求都得带 token——夹具把它挂进
        # TestClient 的默认头，正常路径的断言因此不必逐条改。鉴权本身的用例在
        # `tests/test_session_auth.py`。
        self.token = TEST_TOKEN
        self.auth_policy = SessionAuthPolicy(lambda: self.token)
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=self.auth_policy,
                keepalive_interval=keepalive,
                run_id_timeout=5.0,
            )
        )

    async def seed(self) -> None:
        """建 fixture 数据。单独一步：异步测试要在**自己的**事件循环里建。"""
        repos = self.repositories
        await repos.backends.save(
            Backend.create(key="mock", display_name="Mock", driver_kind="mock")
        )
        await repos.backends.save(
            Backend.create(key="ghost", display_name="Ghost", driver_kind="native")
        )
        project = await repos.projects.save(
            Project.create(slug=PROJECT_SLUG, display_name="工作台")
        )
        other = await repos.projects.save(
            Project.create(slug=OTHER_SLUG, display_name="别处")
        )
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="mock", display_name="Mock 绑定", is_default=True
            )
        )
        self.other_binding = await repos.bindings.save(
            AgentBinding.create(
                project=other, backend="mock", display_name="别处的绑定"
            )
        )
        self.ghost_binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="ghost", display_name="没有 Driver 的绑定"
            )
        )
        self.project_id = project.id
        self.other_project_id = other.id

    def close(self) -> None:
        asyncio.run(self.host.aclose())
        self.unit_of_work.close()

    async def aclose(self) -> None:
        await self.host.aclose()
        self.unit_of_work.close()

    # --- 便捷方法 ------------------------------------------------------- #

    def new_conversation(self, client: TestClient, **body) -> str:
        payload = {"bindingId": self.binding.id, **body}
        response = client.post(
            f"/api/projects/{self.project_id}/conversations", json=payload
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


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


def read_sse(response, *, limit: int) -> tuple[list[dict], list[str]]:
    """从一条 SSE 响应里读出 ``limit`` 个 ``data:`` 帧，同时收集注释行。"""
    events: list[dict] = []
    comments: list[str] = []
    for line in response.iter_lines():
        if not line:
            continue
        if line.startswith(":"):
            comments.append(line)
            continue
        assert line.startswith("data: "), line
        events.append(json.loads(line[len("data: ") :]))
        if len(events) >= limit:
            break
    return events, comments


def wait_for(predicate, *, timeout: float = 5.0):
    """轮询直到 ``predicate()`` 返回真值。

    事件是异步灌进 Event Store 的：HTTP 请求返回的那一刻，剧本可能还在往下发。
    断言「时间线现在长什么样」必须给它一点时间，否则测试会随机红——
    这是**竞态**，不是功能缺陷，所以用轮询而不是 sleep 一个魔法常数。
    """
    import time

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s），最后一次取到：{last!r}")


# --------------------------------------------------------------------------- #
# 1. Conversation 列表 / 新建
# --------------------------------------------------------------------------- #


def test_list_conversations_is_empty_at_first(harness, client):
    body = client.get(f"/api/projects/{harness.project_id}/conversations").json()
    assert body == {
        "projectId": harness.project_id,
        "conversations": [],
        "count": 0,
    }


def test_list_conversations_accepts_a_bare_slug(harness, client):
    harness.new_conversation(client)
    body = client.get(f"/api/projects/{PROJECT_SLUG}/conversations").json()
    assert body["count"] == 1


def test_list_conversations_404_for_unknown_project(client):
    response = client.get("/api/projects/nope/conversations")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_create_conversation_returns_201_and_the_domain_object(harness, client):
    response = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "第一条"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "第一条"
    assert body["agentBindingId"] == harness.binding.id
    assert body["state"] == "idle"
    # 没有原生 Session：N §9 允许懒创建，不得伪造一个 id。
    assert body["nativeSessionId"] is None


def test_create_conversation_falls_back_to_the_binding_name(harness, client):
    body = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id},
    ).json()
    assert body["title"] == harness.binding.display_name


def test_create_conversation_can_adopt_a_native_session_ref(harness, client):
    body = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "nativeSessionRef": "native-abc"},
    ).json()
    assert body["nativeSessionId"] == "native-abc"


def test_create_conversation_rejects_a_binding_from_another_project(harness, client):
    response = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.other_binding.id},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "binding_project_mismatch"


def test_create_conversation_404_for_unknown_binding(harness, client):
    response = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": "binding:workbench:mock:nope"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "binding_not_found"


# --------------------------------------------------------------------------- #
# 2. 单条 Conversation
# --------------------------------------------------------------------------- #


def test_get_conversation_before_any_run(harness, client):
    conversation_id = harness.new_conversation(client)
    body = client.get(f"/api/conversations/{conversation_id}").json()
    assert body["conversation"]["id"] == conversation_id
    assert body["runtime"] == {
        "active": False,
        "runtimeId": None,
        "backendId": None,
        "nativeSessionId": None,
        "owner": None,
        "workspaceRoot": None,
    }
    # 缓冲里一条事件都没有 → 「不知道」，不是「空摘要」。
    assert body["timeline"] is None
    assert body["advisory"] is None


def test_get_conversation_404(client):
    response = client.get("/api/conversations/conversation:00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404
    assert set(response.json()["error"]) >= {"code", "message"}
    assert response.json()["error"]["code"] == "conversation_not_found"


# --------------------------------------------------------------------------- #
# 3. 发消息
# --------------------------------------------------------------------------- #


def test_send_message_starts_the_runtime_and_returns_202_with_a_run_id(
    harness, client
):
    conversation_id = harness.new_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "你好"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["runId"] == "run-full"
    assert body["runIdPending"] is False
    assert body["runtimeId"]
    assert harness.host.is_active(conversation_id)


def test_second_message_reuses_the_same_runtime(harness, client):
    conversation_id = harness.new_conversation(client)
    first = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "一"}
    ).json()
    client.post(f"/api/conversations/{conversation_id}/interrupt")
    second = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "二"}
    ).json()
    assert second["runtimeId"] == first["runtimeId"]


def test_send_message_rejects_an_empty_text(harness, client):
    conversation_id = harness.new_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": ""}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_message"


def test_send_message_accepts_an_optional_client_ref(harness, client):
    """AD-94：``clientRef`` 是可选的，给不给都是 202。"""
    conversation_id = harness.new_conversation(client)
    assert (
        client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"text": "带编号", "clientRef": "ref-1"},
        ).status_code
        == 202
    )


def test_send_message_rejects_an_overlong_client_ref(harness, client):
    """长度上限 64：编号是个编号，不是又一个正文字段。"""
    conversation_id = harness.new_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "hi", "clientRef": "x" * 65},
    )
    assert response.status_code == 422


def test_send_message_404_for_unknown_conversation(client):
    response = client.post(
        "/api/conversations/conversation:00000000-0000-4000-8000-000000000000/messages",
        json={"text": "hi"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


def test_send_message_503_when_the_backend_has_no_driver(harness, client):
    """AD-53：Binding 指向的 Backend 没注册 Driver 是**显式状态**，不是 500。"""
    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.ghost_binding.id},
    ).json()
    response = client.post(
        f"/api/conversations/{created['id']}/messages", json={"text": "hi"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "driver_not_registered"


# --------------------------------------------------------------------------- #
# 4. 事件流（SSE）
#
# 为什么这一节走 ASGI 而不是 TestClient：`TestClient` 与 `httpx.ASGITransport`
# 都是**先把整个响应跑完再返回**（`await self.app(...)` 在 handle_request 里），
# 对一条永不结束的 SSE 流就是死等。所以这里直接驱动 ASGI 应用，一帧一帧地收
# ——顺带也把「客户端断开」做成真的 `http.disconnect`，AD-61 才验得准。
# --------------------------------------------------------------------------- #


class SseProbe:
    """直接驱动 ASGI 应用的一条 SSE 连接。

    ``__aenter__`` 在收到 ``http.response.start`` 后返回，因此「流已经挂上」是
    可断言的事实，而不是靠 sleep 一个魔法常数猜出来的。
    """

    def __init__(
        self, app, path: str, *, query: str = "", token: str | None = TEST_TOKEN
    ) -> None:
        self.app = app
        self.path = path
        self.query = query
        self.token = token
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "SseProbe":
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.started.wait(), timeout=5.0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._disconnect.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        async def receive():
            await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.headers = {
                    key.decode(): value.decode() for key, value in message["headers"]
                }
                self.started.set()
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    self.frames.put_nowait(body.decode())

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query.encode(),
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"accept", b"text/event-stream"),
                *(
                    [(b"authorization", f"Bearer {self.token}".encode())]
                    if self.token
                    else []
                ),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        try:
            await self.app(scope, receive, send)
        finally:
            self.started.set()

    async def next_frame(self, *, timeout: float = 5.0) -> str:
        return await asyncio.wait_for(self.frames.get(), timeout=timeout)

    async def next_event(self, *, timeout: float = 5.0) -> dict:
        while True:
            frame = await self.next_frame(timeout=timeout)
            if frame.startswith("data: "):
                return json.loads(frame[len("data: ") :].strip())

    async def next_events(self, count: int, *, timeout: float = 5.0) -> list[dict]:
        return [await self.next_event(timeout=timeout) for _ in range(count)]


async def _seeded(tmp_path: Path, **kwargs) -> Harness:
    built = Harness(tmp_path, **kwargs)
    await built.seed()
    return built


async def _conversation_with_a_run(built: Harness, *, at_least: int) -> str:
    """建一条会话、开一轮，并等到缓冲里至少有 ``at_least`` 条事件。"""
    conversation = await built.repositories.conversations.save(
        Conversation.create(
            project_id=built.project_id,
            agent_binding_id=built.binding.id,
            title="流测试",
        )
    )
    await built.host.ensure_runtime(conversation)
    await built.host.send_message(conversation.id, "hi")
    await _await_sequence(built, conversation.id, at_least - 1)
    return conversation.id


async def _await_sequence(built: Harness, conversation_id: str, target: int) -> int:
    for _ in range(500):
        latest = await built.host.event_store.latest_sequence(conversation_id)
        if latest is not None and latest >= target:
            return latest
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到 sequence ≥ {target}")


async def test_sse_replays_the_event_store_from_the_beginning(tmp_path):
    built = await _seeded(tmp_path)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=3)
        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events"
        ) as probe:
            assert probe.status == 200
            assert probe.headers["content-type"].startswith("text/event-stream")
            events = await probe.next_events(4)
        assert [e["sequence"] for e in events] == [0, 1, 2, 3]
        assert [e["event"]["type"] for e in events] == [
            # 批次八第 6 件：用户自己发的那句话是本轮的第一条事件。
            "extension.event",
            "session.created",
            "run.started",
            "reasoning.status",
        ]
        assert events[0]["conversationId"] == conversation_id
    finally:
        await built.aclose()


async def test_sse_replay_carries_the_client_ref_through_the_api(tmp_path):
    """AD-94 端到端：``POST /messages`` 收下的编号，在 ``?after=`` 重放里还在。

    两条消息一条带编号一条不带，验的是两件事：带的那条**原样**回来（页面按编号
    换占位），不带的那条**没有这个键**（不是 ``null``）——两种「空」在 wire 上
    只有一种形状，前端不必分别处理。
    """
    httpx = pytest.importorskip("httpx")
    built = await _seeded(tmp_path)
    try:
        conversation = await built.repositories.conversations.save(
            Conversation.create(
                project_id=built.project_id,
                agent_binding_id=built.binding.id,
                title="对账",
            )
        )
        transport = httpx.ASGITransport(app=built.app)
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=headers
        ) as api:
            first = await api.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "带编号的一句", "clientRef": "ref-e2e-1"},
            )
            assert first.status_code == 202
            await built.host.interrupt(conversation.id)
            second = await api.post(
                f"/api/conversations/{conversation.id}/messages",
                json={"text": "不带编号的一句"},
            )
            assert second.status_code == 202

        replayed = await built.host.event_store.replay(conversation.id)
        user_events = [
            envelope.event.data
            for envelope in replayed
            if envelope.event.type == "extension.event"
            and envelope.event.name == "user.message"
        ]
        assert [data["text"] for data in user_events] == [
            "带编号的一句",
            "不带编号的一句",
        ]
        assert user_events[0]["clientRef"] == "ref-e2e-1"
        assert "clientRef" not in user_events[1]
    finally:
        await built.aclose()


async def test_sse_after_cursor_skips_what_the_client_already_has(tmp_path):
    built = await _seeded(tmp_path)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=5)
        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events", query="after=2"
        ) as probe:
            events = await probe.next_events(2)
        assert [e["sequence"] for e in events] == [3, 4]
    finally:
        await built.aclose()


async def test_sse_delivers_realtime_events_after_the_replay(tmp_path):
    """先确认流已经空转（收到一条心跳 = 回放已放完），再制造新事件。

    这样「后到的事件是被实时推过来的」就不是猜的：订阅建立时它们还不存在，
    不可能来自回放。
    """
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=1)
        # 剧本停在权限请求上；等它出现，此刻缓冲就到此为止。
        request_id = "req-full-permission"
        await _await_pending(built, conversation_id, request_id)
        latest = await built.host.event_store.latest_sequence(conversation_id)

        async with SseProbe(
            built.app,
            f"/api/conversations/{conversation_id}/events",
            query=f"after={latest}",
        ) as probe:
            # 批次二十八：重放批之后先来一帧 2KB 填充注释，再才是心跳。
            assert await probe.next_frame() == SSE_PADDING_FRAME
            assert await probe.next_frame() == ": keepalive\n\n"
            await built.host.resolve_interaction(
                conversation_id,
                request_id,
                InteractionResponse(kind="permission", option_id="allow"),
            )
            event = await probe.next_event()
        assert event["sequence"] == latest + 1
        assert event["event"]["type"] == "permission.resolved"
    finally:
        await built.aclose()


async def _await_pending(built: Harness, conversation_id: str, request_id: str) -> None:
    for _ in range(500):
        state = built.host.timeline(conversation_id)
        if state is not None and state.item(f"interaction:{request_id}") is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到待答交互 {request_id!r}")


async def test_sse_emits_a_keepalive_comment_when_idle(tmp_path):
    """AD-62：Backend 侧没有心跳，服务端自己发注释帧，代理才不会把连接判死。"""
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation = await built.repositories.conversations.save(
            Conversation.create(
                project_id=built.project_id,
                agent_binding_id=built.binding.id,
                title="空转",
            )
        )
        async with SseProbe(
            built.app, f"/api/conversations/{conversation.id}/events"
        ) as probe:
            padding = await probe.next_frame()
            first = await probe.next_frame()
            second = await probe.next_frame()
        # 空会话也照样先发填充：隧道那边要先见到字节才肯把响应转出去。
        assert padding == SSE_PADDING_FRAME
        assert first == second == ": keepalive\n\n"
    finally:
        await built.aclose()


async def test_sse_disconnect_only_closes_the_subscription(tmp_path):
    """AD-61：断流不影响 Runtime；恢复手段就是带 ``?after=`` 重连。"""
    built = await _seeded(tmp_path)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=2)
        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events"
        ) as probe:
            await probe.next_event()
        assert built.host.is_active(conversation_id) is True
        assert built.host.subscriber_count(conversation_id) == 0

        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events", query="after=0"
        ) as probe:
            resumed = await probe.next_event()
        assert resumed["sequence"] == 1
    finally:
        await built.aclose()


def test_sse_404_for_unknown_conversation(client):
    response = client.get(
        "/api/conversations/conversation:00000000-0000-4000-8000-000000000000/events"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


# --------------------------------------------------------------------------- #
# 5. 审批往返
# --------------------------------------------------------------------------- #


def _pending_interaction(client, conversation_id: str, *, kind: str) -> dict:
    def probe():
        timeline = client.get(f"/api/conversations/{conversation_id}").json()["timeline"]
        for entry in (timeline or {}).get("pendingInteractions", ()):
            if entry["interactionKind"] == kind:
                return entry
        return None

    return wait_for(probe)


def test_permission_round_trip(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    pending = _pending_interaction(client, conversation_id, kind="permission")

    response = client.post(
        f"/api/conversations/{conversation_id}/interactions/{pending['interactionId']}",
        json={"kind": "permission", "optionId": "allow"},
    )
    assert response.status_code == 200
    assert response.json()["resolved"] is True

    # 剧本继续往下跑：下一张待答卡变成提问。
    _pending_interaction(client, conversation_id, kind="question")


def test_resolving_an_unknown_interaction_is_404(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    _pending_interaction(client, conversation_id, kind="permission")
    response = client.post(
        f"/api/conversations/{conversation_id}/interactions/nope",
        json={"kind": "permission", "optionId": "allow"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "interaction_not_found"


def test_resolving_without_a_runtime_is_409(harness, client):
    conversation_id = harness.new_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/interactions/whatever",
        json={"kind": "permission", "optionId": "allow"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "runtime_not_active"


def test_interaction_kind_is_a_closed_set(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    response = client.post(
        f"/api/conversations/{conversation_id}/interactions/x",
        json={"kind": "telepathy"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_interaction_response"


# --------------------------------------------------------------------------- #
# 6. 中断 / 停止
# --------------------------------------------------------------------------- #


def test_interrupt_produces_a_terminal_run_state(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    assert client.post(f"/api/conversations/{conversation_id}/interrupt").status_code == 200
    wait_for(
        lambda: client.get(f"/api/conversations/{conversation_id}").json()["timeline"][
            "runState"
        ]
        == "interrupted"
    )


def test_interrupt_without_a_runtime_is_200(harness, client):
    """批次十七第 2 件：没有活跃 runtime 不是错误，是「没什么可停的」。

    原口径是 409 ``runtime_not_active``——真机上用户看到的就是那句内部报错
    （`docs/quality/verify5.md` ②），而页面仍旧停在运行中。
    """
    conversation_id = harness.new_conversation(client)
    response = client.post(f"/api/conversations/{conversation_id}/interrupt")
    assert response.status_code == 200
    body = response.json()
    assert body["interrupted"] is False
    assert body["active"] is False
    assert body["reconciled"] is True
    # 这条会话一条事件都没有 → 没有失配可补。
    assert body["synthesizedRunFailed"] is False


def test_stop_keeps_the_conversation_and_the_replayable_timeline(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    stopped = client.post(f"/api/conversations/{conversation_id}/stop").json()
    assert stopped == {
        "conversationId": conversation_id,
        "stopped": True,
        "active": False,
    }
    body = client.get(f"/api/conversations/{conversation_id}").json()
    assert body["runtime"]["active"] is False
    # v1.0 §9.4：停 Runtime 不删会话；摘要仍从 Event Store 重建得出。
    assert body["conversation"]["id"] == conversation_id
    assert body["timeline"]["itemCount"] > 0


def test_stop_is_idempotent(harness, client):
    conversation_id = harness.new_conversation(client)
    assert client.post(f"/api/conversations/{conversation_id}/stop").json()["stopped"] is False


# --------------------------------------------------------------------------- #
# 7. 原生历史
# --------------------------------------------------------------------------- #


def test_history_409_when_no_native_session_is_bound(harness, client):
    conversation_id = harness.new_conversation(client)
    response = client.get(f"/api/conversations/{conversation_id}/history")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "native_session_not_bound"


def test_history_404_for_an_unknown_native_session(harness, client):
    conversation_id = harness.new_conversation(client, nativeSessionRef="native-ghost")
    response = client.get(f"/api/conversations/{conversation_id}/history")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "native_session_not_found"


def test_history_returns_the_native_ledger(harness, client):
    from drivers.base import CreateSessionOptions

    session = asyncio.run(
        harness.driver.create_native_session(harness.binding, CreateSessionOptions())
    )
    conversation_id = harness.new_conversation(
        client, nativeSessionRef=session.native_session_id
    )
    body = client.get(f"/api/conversations/{conversation_id}/history").json()
    assert body["nativeSessionId"] == session.native_session_id
    assert body["complete"] is True
    assert [entry["role"] for entry in body["entries"]] == ["user", "assistant"]


# --------------------------------------------------------------------------- #
# 8. Model Catalog
# --------------------------------------------------------------------------- #


def test_model_catalog_requires_a_binding(client):
    response = client.get("/api/backends/mock/models")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "binding_required"


def test_model_catalog_for_a_binding(harness, client):
    body = client.get(
        "/api/backends/mock/models", params={"binding": harness.binding.id}
    ).json()
    assert body["bindingId"] == harness.binding.id
    assert [m["modelId"] for m in body["models"]] == ["mock-small", "mock-large"]
    assert body["mode"] == "open"


def test_model_catalog_rejects_a_binding_from_another_backend(harness, client):
    response = client.get(
        "/api/backends/ghost/models", params={"binding": harness.binding.id}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "binding_backend_mismatch"


def test_model_catalog_503_without_a_driver(harness, client):
    response = client.get(
        "/api/backends/ghost/models", params={"binding": harness.ghost_binding.id}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "driver_not_registered"


def test_model_catalog_404_for_unknown_binding(client):
    response = client.get("/api/backends/mock/models", params={"binding": "binding:x:mock"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "binding_not_found"


# --------------------------------------------------------------------------- #
# 9. 错误形状（全端点统一）
# --------------------------------------------------------------------------- #


def test_every_error_uses_the_same_envelope(harness, client):
    missing = "conversation:00000000-0000-4000-8000-000000000000"
    for method, path, body in (
        ("get", "/api/projects/nope/conversations", None),
        ("get", f"/api/conversations/{missing}", None),
        ("delete", f"/api/conversations/{missing}", None),
        ("post", f"/api/conversations/{missing}/messages", {"text": "x"}),
        ("post", f"/api/conversations/{missing}/interrupt", None),
        ("post", f"/api/conversations/{missing}/stop", None),
        ("get", f"/api/conversations/{missing}/history", None),
        ("get", "/api/backends/mock/models", None),
    ):
        response = getattr(client, method)(path, **({"json": body} if body else {}))
        assert response.status_code >= 400, path
        payload = response.json()
        assert set(payload) == {"error"}, path
        assert set(payload["error"]) == {"code", "message"}, path
        assert isinstance(payload["error"]["code"], str)


# --------------------------------------------------------------------------- #
# 10. 删会话（批次十一第 1 件）
#
# 语义：停 Runtime → 删领域记录 → 清事件缓冲；**原生 Session 不删**（v1.0 §16.6）。
# --------------------------------------------------------------------------- #


def test_delete_conversation_returns_the_agreed_shape(harness, client):
    conversation_id = harness.new_conversation(client)
    response = client.delete(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200
    assert response.json() == {"conversationId": conversation_id, "deleted": True}


def test_delete_conversation_404_for_unknown_conversation(client):
    missing = "conversation:00000000-0000-4000-8000-000000000000"
    response = client.delete(f"/api/conversations/{missing}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


def test_delete_conversation_drops_it_from_both_lists(harness, client):
    """删完之后两张列表都不该再有它——真删除，不是软删除。"""
    conversation_id = harness.new_conversation(client)
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 200

    by_project = client.get(
        f"/api/projects/{harness.project_id}/conversations"
    ).json()
    assert [c["id"] for c in by_project["conversations"]] == []
    recent = client.get("/api/conversations").json()
    assert [c["id"] for c in recent["conversations"]] == []
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


async def test_delete_conversation_purges_the_event_buffer(tmp_path):
    """事件缓冲跟着走：留着只会让 ``?after=`` 重放出一条已删会话的历史。"""
    built = await _seeded(tmp_path)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=3)
        assert await built.host.event_store.latest_sequence(conversation_id) is not None

        httpx = pytest.importorskip("httpx")
        transport = httpx.ASGITransport(app=built.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as api:
            response = await api.delete(f"/api/conversations/{conversation_id}")
        assert response.status_code == 200

        assert await built.host.event_store.replay(conversation_id) == ()
        assert await built.host.event_store.latest_sequence(conversation_id) is None
        # 停 Runtime 也在这条路径里：会话都没了，不该还有活跃 runtime。
        assert built.host.is_active(conversation_id) is False
    finally:
        await built.aclose()


async def test_delete_conversation_ends_the_sse_stream_with_a_deleted_event(tmp_path):
    """在线订阅者收到一条 ``kaus/conversation.deleted`` 之后流结束。

    v1.1 的 30 条核心事件已经冻结，这条通知走 ``extension.event``（与
    ``kaus/user.message`` 同一个出口、同一个 namespace，见 AD-86）。
    """
    built = await _seeded(tmp_path)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=3)
        latest = await built.host.event_store.latest_sequence(conversation_id)
        async with SseProbe(
            built.app,
            f"/api/conversations/{conversation_id}/events",
            query=f"after={latest}",
        ) as probe:
            assert probe.status == 200
            httpx = pytest.importorskip("httpx")
            transport = httpx.ASGITransport(app=built.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            ) as api:
                assert (
                    await api.delete(f"/api/conversations/{conversation_id}")
                ).status_code == 200
            event = await probe.next_event()
        assert event["event"]["type"] == "extension.event"
        assert event["event"]["namespace"] == "kaus"
        assert event["event"]["name"] == "conversation.deleted"
        assert event["event"]["data"] == {"conversationId": conversation_id}
        # 序号接着清空前的最后一条：从 0 重来的话订阅端会把它当旧事件跳过。
        assert event["sequence"] == latest + 1
        # 通知之后流就结束了（不是继续挂着等心跳）——注意这里没有断开连接，
        # 是服务端自己收的流。
        assert probe._task is not None
        done, _ = await asyncio.wait({probe._task}, timeout=5.0)
        assert done, "SSE 流在 conversation.deleted 之后没有结束"
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 11. 首发失败的空会话标记（批次十一第 2 件）
#
# 「发送前失败」= Runtime 根本没起来，一个字都没发出去。这类失败的错误信封上
# 多一个 `emptyConversation: true`，前端据此紧接着调 DELETE 收掉空壳。
# 服务端**不自动删**——删除是显式动作。
# --------------------------------------------------------------------------- #


class UnreachableGatewayDriver(MockDriver):
    """`start_runtime` 就连不上——模拟「网关没在跑」。"""

    async def start_runtime(self, conversation, surface):  # type: ignore[override]
        raise ConnectionRefusedError("[Errno 111] Connection refused")


def test_empty_conversation_marked_when_the_driver_is_not_registered(harness, client):
    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.ghost_binding.id},
    ).json()
    response = client.post(
        f"/api/conversations/{created['id']}/messages", json={"text": "hi"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["emptyConversation"] is True


def test_empty_conversation_marked_when_the_gateway_is_unreachable(harness, client):
    """网关不可达同样是「发送前失败」，而且不该是 500。"""
    harness.registry.register(UnreachableGatewayDriver(), replace=True)
    conversation_id = harness.new_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}
    )
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "runtime_start_failed"
    assert body["emptyConversation"] is True


def test_no_empty_conversation_marker_once_the_conversation_has_events(harness, client):
    """已经发生过事情的会话不带标记——删了会丢东西。"""
    conversation_id = harness.new_conversation(client)
    assert (
        client.post(
            f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}
        ).status_code
        == 202
    )
    wait_for(
        lambda: asyncio.run(
            harness.host.event_store.latest_sequence(conversation_id)
        )
        is not None
    )
    # 现在把 Driver 摘掉：下一次发送同样在「发送前」失败，但这条会话不是空的。
    harness.registry.unregister("mock")
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "再来一句"}
    )
    assert response.status_code == 503
    assert "emptyConversation" not in response.json()["error"]


# --------------------------------------------------------------------------- #
# 12. 未注册 Driver 的可读错误（批次十一第 3 件）
# --------------------------------------------------------------------------- #


def _driver_error_of(harness, configured: tuple[str, ...]) -> dict:
    """用给定的 `configured_backend_ids` 重挂一次路由，取那条错误的 detail。"""
    app = fastapi.FastAPI()
    app.include_router(
        build_session_router(
            session_host=harness.host,
            repositories=harness.repositories,
            registry=harness.registry,
            auth_policy=harness.auth_policy,
            configured_backend_ids=configured,
        )
    )
    with TestClient(app, headers={"Authorization": f"Bearer {harness.token}"}) as api:
        response = api.get(
            "/api/backends/ghost/models",
            params={"binding": harness.ghost_binding.id},
        )
    assert response.status_code == 503
    return response.json()["error"]


def test_driver_not_registered_hint_when_the_config_has_no_such_entry(harness):
    """配置里压根没有这条 id → 告诉用户去 backends 里加一条。"""
    error = _driver_error_of(harness, ())
    assert error["code"] == "driver_not_registered"
    # message 是给人看的一句话，不是 `未注册的 backend：'backend:ghost'`。
    assert "backend:ghost" in error["message"]
    assert error["detail"]["backendId"] == "backend:ghost"
    assert error["detail"]["registered"] == ["backend:mock"]
    assert "dashboard-config.json" in error["detail"]["hint"]
    assert "docs/ops/backends.md" in error["detail"]["hint"]


def test_driver_not_registered_hint_when_the_entry_exists_but_did_not_register(harness):
    """配置里有这条 id 却没注册上 → 那是网关/base_url 的事，不是少写配置。"""
    error = _driver_error_of(harness, ("backend:ghost",))
    assert error["detail"]["hint"] == "该引擎的网关没在运行或 base_url 未配置"
    # 批次十六：`cause` 是稳定机器可读串（与 `runtime_start_failed` 同一形状）。
    assert error["detail"]["cause"] == "backend_configured_but_not_registered"
    # detail 里只有 id 与根因，不含任何配置内容（AD-48：配置与凭据不进接口）。
    assert set(error["detail"]) == {"backendId", "registered", "hint", "cause"}


def test_archive_restores_same_native_session_and_hides_from_lists(harness):
    with TestClient(harness.app, headers={"Authorization": f"Bearer {harness.token}"}) as client:
        ident = harness.new_conversation(client, title="归档验收", nativeSessionRef="native-kept")
        archived = client.patch(f"/api/conversations/{ident}/archive", json={"archived": True})
        assert archived.status_code == 200, archived.text
        assert archived.json()["conversation"]["archivedAt"]
        assert client.get("/api/conversations").json()["count"] == 0
        assert client.get(f"/api/projects/{harness.project_id}/conversations").json()["count"] == 0
        assert [row["id"] for row in client.get("/api/conversations?archived=true").json()["conversations"]] == [ident]
        blocked = client.post(f"/api/conversations/{ident}/messages", json={"text": "不要发送"})
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "conversation_archived"
        restored = client.patch(f"/api/conversations/{ident}/archive", json={"archived": False})
        assert restored.status_code == 200, restored.text
        assert restored.json()["conversation"]["nativeSessionId"] == "native-kept"
        assert restored.json()["conversation"]["archivedAt"] is None
        assert client.get("/api/conversations").json()["count"] == 1
        assert client.get("/api/conversations?archived=true").json()["count"] == 0


def test_archive_cannot_hide_externally_owned_conversation(harness):
    with TestClient(harness.app, headers={"Authorization": f"Bearer {harness.token}"}) as client:
        ident = harness.new_conversation(client)
        asyncio.run(harness.host.leases.try_acquire_external_cli(ident, owner_id="terminal"))
        result = client.patch(f"/api/conversations/{ident}/archive", json={"archived": True})
        assert result.status_code == 409, result.text
        assert not asyncio.run(harness.repositories.conversations.get(ident)).archived_at
