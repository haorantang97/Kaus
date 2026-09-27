"""批次三十七 · R6 / R7：被引擎拒绝的那一句要留下状态，忙碌要回 409。

R6（`docs/quality/external-review-2026-09-07.md`）：Host 在引擎接受之前就持久化并
广播了 `kaus/user.message`（顺序是对的——它是**发起**下一轮的东西）。但引擎随后
拒绝时，此前没有任何持久化的失败状态：刷新之后那句话与被正常处理过的一模一样，
重发还会显示两遍。AD-105 的「不回滚」不变，补的是一条状态事件。

R7：ACP 的「上一轮还在跑」抛的是裸 `RuntimeError`，从会话端点漏进统一 500 兜底
（真机：`500 / internal_error / 服务端出错：RuntimeError`），而同一个状态在 Hermes
那边早就是 409 + 一句人话。

隔离：SQLite 建在 `tmp_path`，引擎是 MockDriver 的子类或仓库自带的假 ACP 子进程。
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.events.models import PRODUCT_CONTENT_EVENT_NAMES, retention_for  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.acp.driver import AcpDriver  # noqa: E402
from drivers.acp.testing.fake_acp_agent import fake_agent_spec  # noqa: E402
from drivers.base import (  # noqa: E402
    TURN_ALREADY_RUNNING,
    DriverError,
    MessageInput,
    TurnAlreadyRunningError,
    turn_already_running_hint,
)
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.mock.fixtures import text_stream_script  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_reducer import (  # noqa: E402
    USER_MESSAGE_FAILED_NAME,
    USER_MESSAGE_NAMESPACE,
    MessageItem,
)
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch37"


class BusyDriver(MockDriver):
    """一台「上一轮还在跑」的引擎（不真的跑，直接拒）。"""

    async def send_message(self, runtime, content):  # noqa: ANN001
        raise TurnAlreadyRunningError(
            "上一轮尚未结束；先 interrupt 或等待终态",
            failure=turn_already_running_hint(),
        )


class RejectingDriver(MockDriver):
    """一台「网关明确拒绝」的引擎。"""

    async def send_message(self, runtime, content):  # noqa: ANN001
        raise DriverError("引擎没有接下这一句")


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
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                run_id_timeout=2.0,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        key = self.driver.backend_id.split(":", 1)[1]
        await repos.backends.save(
            Backend.create(
                key=key, display_name="引擎", driver_kind=self.driver.driver_kind
            )
        )
        project = await repos.projects.save(
            Project.create(
                slug=SLUG, display_name="批次三十七", workspace_root=tempfile.gettempdir()
            )
        )
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend=key, display_name="绑定", is_default=True
            )
        )
        register = getattr(self.driver, "register_binding", None)
        if register is not None:
            register(self.binding)

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
    return TestClient(built.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"})


@pytest.fixture()
def busy(tmp_path):
    built = Harness(tmp_path, driver=BusyDriver(backend_key="busy"))
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def rejecting(tmp_path):
    built = Harness(tmp_path, driver=RejectingDriver(backend_key="rejecting"))
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


async def _timeline(host: SessionHost, conversation_id: str):
    return await host.timeline_from_store(conversation_id)


# --------------------------------------------------------------------------- #
# R6：失败状态被持久化，且能在重放里被读回来
# --------------------------------------------------------------------------- #


def test_a_rejected_message_gets_a_persisted_failed_status(busy) -> None:
    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        response = client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "这一句没被接下", "clientRef": "ref-1"},
        )
    assert response.status_code == 409

    # 重放（= 刷新页面走的那条路）：那条用户消息还在，但它现在是 failed。
    state = asyncio.run(_timeline(busy.host, conversation.id))
    assert state is not None
    users = [
        item
        for item in state.items
        if isinstance(item, MessageItem) and item.role == "user"
    ]
    # AD-105 不回滚：那句话仍然只有一条，位置也没变。
    assert len(users) == 1
    assert users[0].text == "这一句没被接下"
    assert users[0].delivery_status == "failed"
    assert users[0].failure_code == TURN_ALREADY_RUNNING
    assert users[0].failure_message


def test_the_failure_event_is_on_the_wire_with_code_and_client_ref(busy) -> None:
    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "你好", "clientRef": "ref-2"},
        )
    envelopes = asyncio.run(busy.host.event_store.replay(conversation.id))
    failures = [
        e
        for e in envelopes
        if e.event.type == "extension.event"
        and e.event.namespace == USER_MESSAGE_NAMESPACE
        and e.event.name == USER_MESSAGE_FAILED_NAME
    ]
    assert len(failures) == 1
    data = failures[0].event.data
    assert data["code"] == TURN_ALREADY_RUNNING
    assert data["message"]
    assert data["clientRef"] == "ref-2"


def test_without_a_client_ref_the_key_is_absent_not_null(busy) -> None:
    """AD-94 的老规矩：「不适用」在 wire 上只有一种形状。"""
    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "你好"}
        )
    envelopes = asyncio.run(busy.host.event_store.replay(conversation.id))
    failure = next(
        e for e in envelopes if getattr(e.event, "name", None) == USER_MESSAGE_FAILED_NAME
    )
    assert "clientRef" not in failure.event.data


def test_the_error_response_carries_the_client_ref_in_detail(busy) -> None:
    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        response = client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "你好", "clientRef": "ref-3"},
        )
    body = response.json()["error"]
    assert body["code"] == "turn_already_running"
    assert body["detail"]["clientRef"] == "ref-3"
    # 既有的 hint 一个键都没被挤掉。
    assert body["detail"]["cause"] == TURN_ALREADY_RUNNING
    assert body["detail"]["hint"]


def test_a_gateway_rejection_is_marked_too(rejecting) -> None:
    conversation = asyncio.run(rejecting.new_conversation())
    with _client(rejecting) as client:
        response = client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "你好"}
        )
    assert response.status_code == 502
    state = asyncio.run(_timeline(rejecting.host, conversation.id))
    users = [
        item
        for item in state.items
        if isinstance(item, MessageItem) and item.role == "user"
    ]
    assert users[0].delivery_status == "failed"
    assert users[0].failure_code == "message_rejected"


def test_a_successful_send_leaves_the_message_ok(tmp_path) -> None:
    """没坏的那条路一个字都不该变。"""
    built = Harness(tmp_path, driver=MockDriver(backend_key="fine"))
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        built.driver.set_script(conversation.id, text_stream_script())
        with _client(built) as client:
            response = client.post(
                f"/api/conversations/{conversation.id}/messages", json={"text": "你好"}
            )
        assert response.status_code == 202
        state = asyncio.run(_timeline(built.host, conversation.id))
        users = [
            item
            for item in state.items
            if isinstance(item, MessageItem) and item.role == "user"
        ]
        assert users[0].delivery_status == "ok"
        assert users[0].failure_code is None
    finally:
        built.close()


def test_the_second_of_two_messages_is_the_one_marked(busy) -> None:
    """配对按 ``clientRef``，不是「最后一条」——同一条会话上可能连发好几句。"""
    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "第一句", "clientRef": "a"},
        )
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "第二句", "clientRef": "b"},
        )
    state = asyncio.run(_timeline(busy.host, conversation.id))
    users = [
        item
        for item in state.items
        if isinstance(item, MessageItem) and item.role == "user"
    ]
    assert [item.client_ref for item in users] == ["a", "b"]
    assert [item.delivery_status for item in users] == ["failed", "failed"]


def test_the_failure_event_shares_the_user_messages_retention() -> None:
    """它是那条正文的状态，不是诊断——按 24 小时清掉，「刷新后仍看得见」只成立一天。"""
    assert USER_MESSAGE_FAILED_NAME in PRODUCT_CONTENT_EVENT_NAMES
    assert retention_for(
        "extension.event",
        namespace=USER_MESSAGE_NAMESPACE,
        name=USER_MESSAGE_FAILED_NAME,
    ) == retention_for(
        "extension.event", namespace=USER_MESSAGE_NAMESPACE, name="user.message"
    )


# --------------------------------------------------------------------------- #
# R7：ACP 忙碌经真实 HTTP 路由回 409
# --------------------------------------------------------------------------- #


@pytest.fixture()
def acp(tmp_path):
    workdir = tempfile.gettempdir()
    driver = AcpDriver(
        fake_agent_spec("interrupt", cwd=workdir),
        backend_key="acpbusy",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )
    built = Harness(tmp_path, driver=driver)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


def test_acp_busy_over_http_is_409_not_500(acp) -> None:
    conversation = asyncio.run(acp.new_conversation())
    with _client(acp) as client:
        first = client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "第一句"}
        )
        assert first.status_code == 202
        second = client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "第二句", "clientRef": "acp-1"},
        )
    # 真机上这里是 500 / internal_error / 「服务端出错：RuntimeError」。
    assert second.status_code == 409
    body = second.json()["error"]
    assert body["code"] == "turn_already_running"
    assert "RuntimeError" not in body["message"]
    assert body["detail"]["hint"]
    assert body["detail"]["clientRef"] == "acp-1"


def test_acp_busy_also_leaves_a_failed_status(acp) -> None:
    conversation = asyncio.run(acp.new_conversation())
    with _client(acp) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages", json={"text": "第一句"}
        )
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "第二句", "clientRef": "acp-2"},
        )
    envelopes = asyncio.run(acp.host.event_store.replay(conversation.id))
    failures = [
        e for e in envelopes if getattr(e.event, "name", None) == USER_MESSAGE_FAILED_NAME
    ]
    assert len(failures) == 1
    assert failures[0].event.data["clientRef"] == "acp-2"
    assert failures[0].event.data["code"] == TURN_ALREADY_RUNNING


def test_the_busy_hint_is_shared_across_drivers() -> None:
    """同一个状态在两台引擎上不该说两句不同的话。"""
    from drivers.hermes import failure_hints

    assert failure_hints.turn_already_running() == turn_already_running_hint()
    assert failure_hints.TURN_ALREADY_RUNNING == TURN_ALREADY_RUNNING


def test_sending_a_message_by_hand_still_raises_for_callers(busy) -> None:
    """Host 记完状态之后**照旧把异常抛回去**——它没有把失败吞掉。"""
    conversation = asyncio.run(busy.new_conversation())

    async def scenario() -> None:
        await busy.host.ensure_runtime(conversation)
        with pytest.raises(TurnAlreadyRunningError):
            await busy.host.send_message(conversation.id, MessageInput(text="你好"))

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 第二轮：保留期钉死在「和用户那句话同一档」（7 天），不是诊断档（24 小时）
# --------------------------------------------------------------------------- #


def test_the_failure_event_is_stored_with_the_seven_day_tier(busy) -> None:
    """不看规则函数，直接看**落库那一行的 expires_at**。

    前端评审提的是这一条：这个事件如果按 24 小时的诊断档清掉，「刷新之后仍然看得见
    这句话没被处理」就只成立一天——而它要解释的正是「这条历史为什么和别的不一样」。
    上一条用例比的是两个函数返回值，这一条比的是真正写进库里的时刻。
    """
    from datetime import timedelta

    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "你好", "clientRef": "ttl-1"},
        )

    async def rows():
        return await busy.host.event_store.read_range(conversation.id)

    stored = {
        getattr(row.envelope.event, "name", None): row for row in asyncio.run(rows())
    }
    failed = stored[USER_MESSAGE_FAILED_NAME]
    original = stored["user.message"]
    lifetime = failed.expires_at - failed.created_at
    assert lifetime == timedelta(days=7)
    # 与它要解释的那条正文同生同灭：两者的保留期必须逐字相同，否则时间线上会出现
    # 「一条失败状态孤零零地指着一条已经不在了的消息」，或者反过来。
    assert lifetime == original.expires_at - original.created_at


def test_the_failure_event_outlives_the_diagnostic_tier(busy) -> None:
    """把时钟推过诊断档（24 小时）再清一次：它必须还在。"""
    from datetime import timedelta

    conversation = asyncio.run(busy.new_conversation())
    with _client(busy) as client:
        client.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "你好", "clientRef": "ttl-2"},
        )

    async def purge_and_replay():
        store = busy.host.event_store
        later = store._clock() + timedelta(days=2)  # noqa: SLF001 - 白盒推时钟
        await store.purge_expired(now=later)
        return await store.replay(conversation.id)

    names = {getattr(e.event, "name", None) for e in asyncio.run(purge_and_replay())}
    assert USER_MESSAGE_FAILED_NAME in names
    assert "user.message" in names
