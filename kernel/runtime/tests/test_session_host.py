"""Session Host 的行为测试（N §9.3 / Phase 3A 验收）。

全程只用 MockDriver，不运行任何真实引擎。覆盖任务书要求的七项：

1. 完整生命周期（文本流 + tool 更新同一张卡 + permission/question/authentication 闭环）；
2. 断线重连按 ``after_sequence`` 续传：无重复、无遗漏；
3. Driver 崩溃 → 合成 ``run.failed`` 并收敛状态；
4. 两条 Conversation 并发互不串流；
5. 保留期清理后卡片仍可从原生历史重建；
6. 同一 Conversation 二次 ``start_runtime`` 被拒；
7. 带外变更信号触发「发送前刷新 + 软提示」。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest

from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository
from app.events.repository import InMemoryEventStoreRepository
from app.projects.models import AgentBinding, Project
from app.projects.repository import InMemoryAgentBindingRepository
from app.runtimes.repository import InMemoryRuntimeLeaseRepository
from drivers.base import CreateSessionOptions, InteractionResponse, RuntimeHandle
from drivers.mock.driver import MockDriver
from drivers.mock.fixtures import (
    MockScript,
    full_lifecycle_script,
    text_stream_script,
    tool_lifecycle_script,
)
from drivers.mock.out_of_band import AsyncFakeOutOfBandWatcher, FakeOutOfBandWatcher
from drivers.registry import BackendDriverRegistry
from runtime.event_envelope import AgentEventEnvelope, TERMINAL_RUN_EVENT_TYPES
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.event_reducer import USER_MESSAGE_NAME, USER_MESSAGE_NAMESPACE
from runtime.session_host import (
    EventSubscription,
    RuntimeAlreadyActiveError,
    RuntimeNotActiveError,
    SessionHost,
)

TIMEOUT = 5.0
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


class _Clock:
    """可推进的假时钟：空闲回收要能在不 sleep 的前提下被测出来。"""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class World:
    host: SessionHost
    registry: BackendDriverRegistry
    event_store: EventStore
    leases: LeaseManager
    conversations: InMemoryConversationRepository
    bindings: InMemoryAgentBindingRepository
    clock: _Clock

    async def add_backend(
        self,
        *,
        backend_key: str = "mock",
        driver: MockDriver | None = None,
        script: MockScript | None = None,
        title: str = "card",
        with_native_session: bool = False,
    ) -> tuple[MockDriver, Conversation]:
        """注册一个 MockDriver + 一条 Project/Binding/Conversation。"""
        driver = driver or MockDriver(backend_key=backend_key)
        self.registry.register(driver, replace=True)
        project = Project.create(
            slug=backend_key, display_name=backend_key, workspace_root="/tmp/session-host"
        )
        binding = AgentBinding.create(
            project=project,
            backend=driver.backend_id,
            native_scope_ref=f"scope-{backend_key}",
            is_default=True,
        )
        await self.bindings.save(binding)
        native_session_id = None
        if with_native_session:
            session = await driver.create_native_session(binding, CreateSessionOptions())
            native_session_id = session.native_session_id
        conversation = Conversation.create(
            project_id=project.id,
            agent_binding_id=binding.id,
            title=title,
            native_session_id=native_session_id,
        )
        await self.conversations.save(conversation)
        if script is not None:
            driver.set_script(conversation.id, script)
        return driver, conversation


def make_world(
    *,
    idle_timeout: timedelta = timedelta(minutes=15),
    retention=timedelta(hours=24),
    event_repository=None,
    lease_repository=None,
) -> World:
    """搭一套完整的 Session Host 环境。

    ``event_repository`` / ``lease_repository`` 默认用内存参考实现；传入
    SQLite 实现即可让同一套用例跑在 v1.0 §11.1 的真实表上。
    """
    clock = _Clock()
    event_store = EventStore(
        event_repository or InMemoryEventStoreRepository(),
        retention=retention,
        clock=clock,
    )
    leases = LeaseManager(lease_repository or InMemoryRuntimeLeaseRepository(), clock=clock)
    conversations = InMemoryConversationRepository()
    bindings = InMemoryAgentBindingRepository()
    registry = BackendDriverRegistry()
    host = SessionHost(
        registry=registry,
        bindings=bindings,
        event_store=event_store,
        lease_manager=leases,
        conversations=conversations,
        idle_timeout=idle_timeout,
        clock=clock,
    )
    return World(
        host=host,
        registry=registry,
        event_store=event_store,
        leases=leases,
        conversations=conversations,
        bindings=bindings,
        clock=clock,
    )


async def next_event(subscription: EventSubscription) -> AgentEventEnvelope:
    return await asyncio.wait_for(subscription.__anext__(), TIMEOUT)


async def next_run_started(subscription: EventSubscription) -> AgentEventEnvelope:
    """吃掉本轮开头的用户消息事件，返回 ``run.started``。

    批次八第 6 件之后，每次 `send_message` 会先落一条
    ``extension.event``（``kaus`` / ``user.message``）——用户那句话本身也是时间线
    上的一条消息。这个助手把「一轮从 run.started 开始」的老写法保住，同时**断言**
    前面那条确实是用户消息（而不是悄悄跳过任何东西）。
    """
    first = await next_event(subscription)
    assert first.event.type == "extension.event"
    assert (first.event.namespace, first.event.name) == (
        USER_MESSAGE_NAMESPACE,
        USER_MESSAGE_NAME,
    )
    return await next_event(subscription)


async def drive_until_run_terminal(
    host: SessionHost,
    subscription: EventSubscription,
    conversation_id: str,
    *,
    auto_resolve: bool = True,
    max_events: int = 200,
) -> tuple[AgentEventEnvelope, ...]:
    """跑到本轮终态为止；沿途自动闭合 permission / question / authentication。"""
    collected: list[AgentEventEnvelope] = []
    for _ in range(max_events):
        envelope = await next_event(subscription)
        collected.append(envelope)
        event = envelope.event
        if auto_resolve and event.type == "permission.requested":
            await host.resolve_interaction(
                conversation_id,
                event.request.request_id,
                InteractionResponse(kind="permission", option_id="allow"),
            )
        elif auto_resolve and event.type == "question.requested":
            await host.resolve_interaction(
                conversation_id,
                event.request.request_id,
                InteractionResponse(kind="question", text="继续"),
            )
        elif auto_resolve and event.type == "authentication.requested":
            await host.resolve_interaction(
                conversation_id,
                event.request.request_id,
                InteractionResponse(kind="authentication", option_id="device-code"),
            )
        elif event.type in TERMINAL_RUN_EVENT_TYPES and not envelope.is_child_run:
            return tuple(collected)
    raise AssertionError("没有等到本轮终态事件")


class CrashingDriver(MockDriver):
    """在收到第 ``crash_after`` 条事件后让事件流抛异常的 MockDriver。

    这是「Driver 崩溃」在测试里的最小可控形态：不改公共层，也不接真实引擎。
    """

    def __init__(self, *, crash_after: int = 2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.crash_after = crash_after

    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]:
        emitted = 0
        async for envelope in super().events(runtime):
            yield envelope
            emitted += 1
            if emitted >= self.crash_after:
                raise ConnectionResetError("Driver 连接断开")


# --------------------------------------------------------------------------- #
# 1. 完整生命周期
# --------------------------------------------------------------------------- #


async def test_full_card_lifecycle_without_any_real_backend() -> None:
    """Phase 3A 验收：不运行真实 Backend 也能走完整卡片生命周期。"""
    world = make_world()
    _, conversation = await world.add_backend(script=full_lifecycle_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "开始吧")
    events = await drive_until_run_terminal(host, subscription, conversation.id)

    types = [envelope.event.type for envelope in events]
    assert types[-1] == "run.completed"
    assert "message.delta" in types and "tool.updated" in types

    state = host.timeline(conversation.id)
    assert state is not None
    # 规则 2：tool.updated 更新同一张卡，不产生重复卡。
    tools = state.items_of("tool")
    assert len(tools) == 1 and tools[0].status == "completed"
    # 规则 3：delta 按 messageId 合并成一条**助手**消息。
    # （另一条 message 是用户自己发的那句，批次八第 6 件。）
    assistant = [item for item in state.items_of("message") if item.role == "assistant"]
    assert len(assistant) == 1
    user_said = [item for item in state.items_of("message") if item.role == "user"]
    assert [item.text for item in user_said] == ["开始吧"]
    messages = assistant
    assert messages[0].text == "先看一下文件，已完成。"
    # 规则 4：三种交互请求都闭环。
    interactions = state.items_of("interaction")
    assert {item.interaction_kind for item in interactions} == {
        "permission",
        "question",
        "authentication",
    }
    assert all(item.status == "resolved" for item in interactions)
    assert state.run_state == "completed"

    # sequence 由 Session Host 分配，从 0 起连续。
    stored = await world.event_store.read_range(conversation.id)
    assert [row.sequence for row in stored] == list(range(len(stored)))

    # AD-11：runtime 活着时 owner=card；停止后释放。
    owner = await host.describe_runtime_owner(conversation.id)
    assert owner is not None and owner.owner_type == "card"
    await host.stop_runtime(conversation.id)
    assert await host.describe_runtime_owner(conversation.id) is None
    assert (await world.conversations.get(conversation.id)).state == "idle"
    subscription.close()


async def test_interrupt_converges_the_run() -> None:
    """N §9.3「处理取消」：interrupt 让本轮进终态。"""
    from drivers.mock.fixtures import hold_script

    world = make_world()
    _, conversation = await world.add_backend(script=hold_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "跑个长任务")
    assert (await next_run_started(subscription)).event.type == "run.started"

    await host.interrupt(conversation.id)
    events = await drive_until_run_terminal(host, subscription, conversation.id)

    assert events[-1].event.type == "run.interrupted"
    assert host.timeline(conversation.id).run_state == "interrupted"
    subscription.close()


# --------------------------------------------------------------------------- #
# 2. 断线重连
# --------------------------------------------------------------------------- #


async def test_reconnect_after_sequence_has_no_gaps_and_no_duplicates() -> None:
    """v1.0 §8.5：``?after=<sequence>`` 续传 —— 不重不漏。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    first = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    original = await drive_until_run_terminal(host, first, conversation.id)
    first.close()

    cut = 3  # 假装客户端只收到了前四条就断线了
    resumed = host.subscribe(conversation.id, after_sequence=cut)
    replayed = [
        await next_event(resumed) for _ in range(len(original) - (cut + 1))
    ]
    resumed.close()

    assert [envelope.sequence for envelope in replayed] == [
        envelope.sequence for envelope in original[cut + 1 :]
    ]
    assert [envelope.event_id for envelope in replayed] == [
        envelope.event_id for envelope in original[cut + 1 :]
    ]
    # 全量重放（after=None）与原始流逐条一致。
    full = host.subscribe(conversation.id)
    everything = [await next_event(full) for _ in original]
    full.close()
    assert [envelope.event_id for envelope in everything] == [
        envelope.event_id for envelope in original
    ]


async def test_subscription_bridges_replay_and_live_without_duplicates() -> None:
    """订阅在「重放尾巴」与「实时流」之间无缝衔接，中间不产生重复。"""
    world = make_world()
    driver, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    warmup = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "第一轮")
    first_round = await drive_until_run_terminal(host, warmup, conversation.id)
    warmup.close()

    # 断线：从第一轮中间续订，随后再跑第二轮。
    resumed = host.subscribe(conversation.id, after_sequence=1)
    driver.set_script(
        conversation.id, text_stream_script(run_id="run-2", message_id="msg-2")
    )
    await host.send_message(conversation.id, "第二轮")
    # 第一段：补齐缓冲里第一轮的尾巴（after=1 之后）。
    replayed = await drive_until_run_terminal(host, resumed, conversation.id)
    # 第二段：无缝接上第二轮的实时事件。
    live = await drive_until_run_terminal(host, resumed, conversation.id)
    resumed.close()

    sequences = [envelope.sequence for envelope in (*replayed, *live)]
    assert sequences == sorted(set(sequences))  # 单调且无重复
    assert sequences[0] == 2  # 从 after_sequence=1 之后接上，没有跳号
    assert sequences == list(range(2, len(first_round) + len(live)))
    assert replayed[-1].event.type == "run.completed"
    # 第二轮的第一条是用户那句话（批次八第 6 件），run.started 紧随其后。
    assert live[0].event.type == "extension.event"
    assert live[1].event.type == "run.started"
    assert live[-1].event.type == "run.completed"


async def test_restarting_a_runtime_resumes_the_sequence_and_timeline() -> None:
    """恢复：停掉再开，游标不倒退、时间线从缓冲重建。"""
    world = make_world()
    driver, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "第一轮")
    first_round = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()
    await host.stop_runtime(conversation.id)

    driver.set_script(
        conversation.id, text_stream_script(run_id="run-2", message_id="msg-2")
    )
    await host.start_runtime(conversation)
    # 重建的时间线里，第一轮的消息还在。
    rebuilt = host.timeline(conversation.id)
    assert rebuilt is not None and rebuilt.message_text("msg-text") == "Hello, world!"

    subscription = host.subscribe(conversation.id, after_sequence=len(first_round) - 1)
    await host.send_message(conversation.id, "第二轮")
    second_round = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    assert second_round[0].sequence == len(first_round)  # 跨 runtime 继续单调


async def test_duplicate_event_ids_are_dropped_once_stored() -> None:
    """N §7.3 规则 9：同一 ``eventId`` 二次投递不入库、不广播、不占 sequence。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    original = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    runtime = host._runtimes[conversation.id]  # noqa: SLF001 - 白盒：直接重投一条
    await host._ingest(runtime, original[0])  # noqa: SLF001

    stored = await world.event_store.read_range(conversation.id)
    assert len(stored) == len(original)
    assert runtime.next_sequence == len(original)


# --------------------------------------------------------------------------- #
# 3. Driver 崩溃
# --------------------------------------------------------------------------- #


async def test_driver_crash_converges_to_run_failed() -> None:
    """Driver 崩溃 → 合成 ``run.failed``，Conversation 收敛为 error，lease 释放。"""
    world = make_world()
    driver = CrashingDriver(crash_after=2, backend_key="mock")
    _, conversation = await world.add_backend(
        driver=driver, script=text_stream_script()
    )
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    failure = events[-1]
    assert failure.event.type == "run.failed"
    assert failure.event.error.code == "runtime.driver_failed"
    assert "ConnectionResetError" in failure.event.error.message

    assert host.is_active(conversation.id) is False
    assert (await world.conversations.get(conversation.id)).state == "error"
    assert await host.describe_runtime_owner(conversation.id) is None
    # 崩溃事件本身也进了缓冲，断线的客户端重连后能看到终态。
    stored = await world.event_store.read_range(conversation.id)
    assert stored[-1].envelope.event.type == "run.failed"

    with pytest.raises(RuntimeNotActiveError):
        await host.send_message(conversation.id, "还在吗")


# --------------------------------------------------------------------------- #
# 4. 多 Conversation 并发
# --------------------------------------------------------------------------- #


async def test_two_conversations_run_concurrently_and_stay_isolated() -> None:
    """N §9.2：多条 Conversation 各有独立事件流、独立 sequence、独立 lease。"""
    world = make_world()
    _, first = await world.add_backend(
        backend_key="mock", script=tool_lifecycle_script(), title="第一条"
    )
    _, second = await world.add_backend(
        backend_key="mock-two",
        script=text_stream_script(run_id="run-b", message_id="msg-b"),
        title="第二条",
    )
    host = world.host

    await host.start_runtime(first)
    await host.start_runtime(second)
    first_sub = host.subscribe(first.id)
    second_sub = host.subscribe(second.id)

    await host.send_message(first.id, "A")
    await host.send_message(second.id, "B")
    first_events, second_events = await asyncio.gather(
        drive_until_run_terminal(host, first_sub, first.id),
        drive_until_run_terminal(host, second_sub, second.id),
    )
    first_sub.close()
    second_sub.close()

    assert {e.conversation_id for e in first_events} == {first.id}
    assert {e.conversation_id for e in second_events} == {second.id}
    assert [e.sequence for e in first_events] == list(range(len(first_events)))
    assert [e.sequence for e in second_events] == list(range(len(second_events)))
    assert host.timeline(first.id).items_of("tool")
    assert not host.timeline(second.id).items_of("tool")
    assert host.timeline(second.id).message_text("msg-b") == "Hello, world!"

    owners = {row.conversation_id for row in await world.leases.describe_all()}
    assert owners == {first.id, second.id}


async def test_two_conversations_on_one_backend_stay_isolated() -> None:
    """同一个 Backend 的两条 Conversation 也各自独立（N §9.2 的 twin 场景）。"""
    world = make_world()
    driver, first = await world.add_backend(backend_key="mock", title="第一条")
    project_binding = await world.bindings.get(first.agent_binding_id)
    second = Conversation.create(
        project_id=first.project_id,
        agent_binding_id=project_binding.id,
        title="第二条",
    )
    await world.conversations.save(second)
    driver.set_script(first.id, text_stream_script(message_id="msg-a"))
    driver.set_script(
        second.id, text_stream_script(run_id="run-b", message_id="msg-b")
    )
    host = world.host

    await host.start_runtime(first)
    await host.start_runtime(second)
    first_sub, second_sub = host.subscribe(first.id), host.subscribe(second.id)
    await host.send_message(first.id, "A")
    await host.send_message(second.id, "B")
    first_events, second_events = await asyncio.gather(
        drive_until_run_terminal(host, first_sub, first.id),
        drive_until_run_terminal(host, second_sub, second.id),
    )
    first_sub.close()
    second_sub.close()

    assert {e.event_id for e in first_events}.isdisjoint(
        {e.event_id for e in second_events}
    )
    assert host.timeline(first.id).message_text("msg-b") is None
    assert host.timeline(second.id).message_text("msg-a") is None


async def test_event_id_collision_across_conversations_is_loud() -> None:
    """N §7.1：``eventId`` 全局唯一。撞车必须报错，而不是让另一条会话丢事件。"""
    from runtime.session_host import DuplicateEventIdError

    world = make_world()
    _, first = await world.add_backend(backend_key="mock", script=text_stream_script())
    _, second = await world.add_backend(
        backend_key="mock-two", script=text_stream_script()
    )
    host = world.host

    await host.start_runtime(first)
    await host.start_runtime(second)
    subscription = host.subscribe(first.id)
    await host.send_message(first.id, "A")
    events = await drive_until_run_terminal(host, subscription, first.id)
    subscription.close()

    stolen = events[0].model_copy(update={"conversation_id": second.id})
    with pytest.raises(DuplicateEventIdError):
        await host._ingest(host._runtimes[second.id], stolen)  # noqa: SLF001

    await host.aclose()


# --------------------------------------------------------------------------- #
# 5. 保留期清理
# --------------------------------------------------------------------------- #


async def test_expired_events_are_purged_and_the_card_rebuilds_from_native_history() -> None:
    """Phase 3A 验收：过期清理生效；清空后卡片仍可从原生历史重建。"""
    world = make_world(retention=timedelta(hours=1))
    driver, conversation = await world.add_backend(
        script=text_stream_script(), with_native_session=True
    )
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()
    assert len(await world.event_store.read_range(conversation.id)) == len(events)

    world.clock.advance(timedelta(hours=2))
    removed = await host.purge_expired_events()

    assert removed == len(events)
    assert await world.event_store.read_range(conversation.id) == ()
    # 内容的权威源是原生历史（D-16 / R-02），缓冲清空不影响可恢复性。
    binding = await world.bindings.get(conversation.agent_binding_id)
    history = await driver.load_native_history(binding, conversation.native_session_id)
    assert history.complete is True and len(history.entries) == 2


async def test_idle_runtimes_are_reclaimed_but_mappings_survive() -> None:
    """v1.0 §9.4：空闲超时回收 runtime 状态，保留 Conversation 与原生 Session 映射。"""
    world = make_world(idle_timeout=timedelta(minutes=5))
    _, conversation = await world.add_backend(
        script=text_stream_script(), with_native_session=True
    )
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    assert await host.sweep_idle() == ()  # 还不到阈值
    world.clock.advance(timedelta(minutes=10))
    assert await host.sweep_idle() == (conversation.id,)

    assert host.is_active(conversation.id) is False
    stored = await world.conversations.get(conversation.id)
    assert stored.state == "idle"
    assert stored.native_session_id == conversation.native_session_id
    assert await world.event_store.latest_sequence(conversation.id) is not None


async def test_busy_runtimes_are_not_reclaimed() -> None:
    """正在跑的一轮不算空闲。"""
    from drivers.mock.fixtures import hold_script

    world = make_world(idle_timeout=timedelta(minutes=5))
    _, conversation = await world.add_backend(script=hold_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "长任务")
    assert (await next_run_started(subscription)).event.type == "run.started"

    world.clock.advance(timedelta(hours=1))
    assert await host.sweep_idle() == ()
    assert host.is_active(conversation.id) is True

    subscription.close()
    await host.stop_runtime(conversation.id)


# --------------------------------------------------------------------------- #
# 6. 同一 Conversation 只能有一个 runtime
# --------------------------------------------------------------------------- #


async def test_second_start_for_the_same_conversation_is_refused() -> None:
    world = make_world()
    driver, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    handle = await host.start_runtime(conversation)
    with pytest.raises(RuntimeAlreadyActiveError):
        await host.start_runtime(conversation)

    # 拒绝之后不得留下第二个 Driver 侧 runtime。
    assert driver.runtime_ids() == (handle.runtime_id,)
    # ensure_runtime 是「有就复用」的显式入口。
    assert (await host.ensure_runtime(conversation)).runtime_id == handle.runtime_id

    await host.stop_runtime(conversation.id)
    restarted = await host.start_runtime(conversation)
    assert restarted.runtime_id != handle.runtime_id


# --------------------------------------------------------------------------- #
# 7. 带外变更信号（AD-20）
# --------------------------------------------------------------------------- #


async def test_out_of_band_signal_refreshes_history_before_send() -> None:
    """AD-20 / v1.0 §8.8.2：发送前刷新 + 软提示，**不阻断发送**。"""
    world = make_world()
    _, conversation = await world.add_backend(
        script=text_stream_script(), with_native_session=True
    )
    host = world.host
    watcher = FakeOutOfBandWatcher()
    host.register_out_of_band_watcher(conversation.id, watcher)

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)

    # 没有带外变更：不刷新、不提示。
    await host.send_message(conversation.id, "第一轮")
    await drive_until_run_terminal(host, subscription, conversation.id)
    assert watcher.poll_count == 1
    assert host.advisory_for(conversation.id) is None
    assert host.last_refreshed_history(conversation.id) is None

    # 用户自己在终端里动了这条会话。
    watcher.mark_changed()
    await host.send_message(conversation.id, "第二轮")
    notice = await next_event(subscription)

    assert notice.event.type == "diagnostic.notice"
    advisory = host.advisory_for(conversation.id)
    assert advisory is not None and advisory.requires_refresh_before_send is True
    history = host.last_refreshed_history(conversation.id)
    assert history is not None and history.native_session_id == conversation.native_session_id
    # 提示归提示，这一轮照常跑完（软提示不锁不禁用）。
    rest = await drive_until_run_terminal(host, subscription, conversation.id)
    assert rest[-1].event.type == "run.completed"
    subscription.close()


async def test_async_watcher_is_accepted_too() -> None:
    world = make_world()
    _, conversation = await world.add_backend(
        script=text_stream_script(), with_native_session=True
    )
    host = world.host
    watcher = AsyncFakeOutOfBandWatcher()
    watcher.mark_changed()
    host.register_out_of_band_watcher(conversation.id, watcher)

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    first = await next_event(subscription)
    subscription.close()

    assert first.event.type == "diagnostic.notice"
    assert host.last_refreshed_history(conversation.id) is not None
    await host.stop_runtime(conversation.id)


async def test_watcher_without_native_session_only_advises() -> None:
    """还没绑定原生 Session 时无从刷新——只留提示，不报错。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host
    watcher = FakeOutOfBandWatcher(changed=True)
    host.register_out_of_band_watcher(conversation.id, watcher)

    await host.start_runtime(conversation)
    await host.send_message(conversation.id, "你好")

    assert host.advisory_for(conversation.id) is not None
    assert host.last_refreshed_history(conversation.id) is None
    await host.stop_runtime(conversation.id)


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


async def test_unknown_conversation_is_not_active() -> None:
    world = make_world()
    with pytest.raises(RuntimeNotActiveError):
        await world.host.interrupt("conversation:00000000-0000-0000-0000-000000000000")


async def test_aclose_stops_everything() -> None:
    world = make_world()
    _, first = await world.add_backend(backend_key="mock", script=text_stream_script())
    _, second = await world.add_backend(
        backend_key="mock-two", script=text_stream_script()
    )
    host = world.host

    await host.start_runtime(first)
    await host.start_runtime(second)
    subscription = host.subscribe(first.id)

    await host.aclose()

    assert host.active_conversation_ids() == ()
    assert await world.leases.describe_all() == ()
    with pytest.raises(StopAsyncIteration):
        await next_event(subscription)


def test_subscriptions_are_registered_synchronously() -> None:
    """``subscribe`` 是同步方法：注册与开始迭代之间没有丢事件的窗口。"""
    world = make_world()
    subscription = world.host.subscribe("conversation:00000000-0000-0000-0000-000000000000")
    assert isinstance(subscription, EventSubscription)
    assert world.host._subscriptions  # noqa: SLF001
    subscription.close()
    assert not world.host._subscriptions  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 与真实持久化层的对接（v1.0 §11.1 的 event_store / runtime_leases 两张表）
# --------------------------------------------------------------------------- #


async def test_host_runs_against_the_sqlite_repositories(tmp_path) -> None:
    """内存参考实现之外，同一套编排必须能直接跑在 SQLite 表上。"""
    from app.persistence.sqlite.database import SqliteDatabase
    from app.persistence.sqlite.events import SqliteEventStoreRepository
    from app.persistence.sqlite.runtimes import SqliteRuntimeLeaseRepository

    database = SqliteDatabase(tmp_path / "kernel.sqlite3")
    world = make_world(
        event_repository=SqliteEventStoreRepository(database),
        lease_repository=SqliteRuntimeLeaseRepository(database),
    )
    host = world.host
    _, conversation = await world.add_backend(script=text_stream_script())

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "你好")
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    rows = await world.event_store.read_range(conversation.id)
    assert [row.sequence for row in rows] == list(range(len(events)))
    owner = await host.describe_runtime_owner(conversation.id)
    assert owner is not None and owner.owner_type == "card"

    await host.stop_runtime(conversation.id)
    assert await host.describe_runtime_owner(conversation.id) is None

    world.clock.advance(timedelta(days=2))
    assert await host.purge_expired_events() == len(events)
    assert await world.event_store.read_range(conversation.id) == ()
