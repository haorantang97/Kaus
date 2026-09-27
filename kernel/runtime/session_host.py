"""Session Host：Conversation ⇄ Runtime 的编排层（N §9.3 / v1.0 §9.4）。

职责（N §9.3 逐条）
-------------------
- 创建与恢复 Conversation 的 Card Runtime（经 Driver Registry 取 Driver →
  ``start_runtime``）；
- 转发 ``send_message`` / ``interrupt`` / ``resolve_interaction``；
- 把 Driver 产出的 :class:`~runtime.event_envelope.AgentEventEnvelope` 交给
  **Event Store** 与 **Reducer**；
- 维护短期重放缓冲与渲染缓存（**不是**持久对话账本，D-16）；
- 向前端提供 ``?after=<sequence>`` 续传的订阅（:meth:`SessionHost.subscribe`）；
- 维护 Runtime Lease（AD-11，信息性）与带外检测的接入点（AD-20）；
- 处理取消、错误与恢复：Driver 崩溃 → 合成 ``run.failed`` 并收敛状态。

三条硬口径
----------
1. **同一 Conversation 不允许两个 runtime。** :meth:`SessionHost.start_runtime`
   在已有活跃 runtime 时抛 :class:`RuntimeAlreadyActiveError`。这与 AD-11 的
   「lease 不是锁」不冲突：前者是**本进程内**的资源不变量（一个会话一条事件流），
   后者说的是**跨进程**的写者归属——Session Host 从不因为「lease 被别人持有」
   而拒绝启动。
2. **sequence 由 Session Host 分配，不用 Driver 的。** Driver 的序号只在它自己
   那次 runtime 内单调；而 ``?after=<sequence>`` 是 **Conversation 级**游标，
   必须跨 runtime 重启保持单调。因此入库前统一改写成
   ``Event Store 的下一个 sequence``（v1.0 §8.5）。
3. **``eventId`` 去重。** 已在 Event Store 里的 ``eventId`` 直接丢弃，不占新
   sequence、不推给订阅者（N §7.3 规则 9）。

带外变更（AD-20）
-----------------
本层只放**骨架**：:class:`OutOfBandWatcher` 是「原生存储被别人动过了吗」的抽象
（``poll() -> changed``），具体探测手段属于 Driver 内部（N §5.4）——AD-20 已把
第一个真实 Backend 的做法定为监视原生存储 WAL 文件的 mtime/size，那属于 Phase 3B。
检测到变化时 Session Host 在**发送前**调用 ``load_native_history`` 刷新，并产出
一条 :class:`~app.runtimes.models.ConcurrencyAdvisory` 软提示 +
``diagnostic.notice`` 事件。**不加锁、不禁用 Composer、不降级只读**（D-09 / R-04）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Mapping, Protocol, runtime_checkable

from app.conversations.models import Conversation
from app.conversations.repository import ConversationRepository
from app.errors import DomainError
from app.projects.models import AgentBinding
from app.projects.repository import AgentBindingRepository
from app.runtimes.models import ConcurrencyAdvisory
from drivers.base import (
    BackendDriver,
    DriverError,
    FailureHint,
    InteractionResponse,
    MessageInput,
    NativeHistory,
    RuntimeHandle,
    SessionProjection,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from drivers.registry import BackendDriverRegistry
from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    DiagnosticNotice,
    EventSource,
    ExtensionEvent,
    MessageCompleted,
    MessageStarted,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    ToolCompleted,
    ToolStarted,
    make_envelope,
    new_event_id,
)
from runtime.event_reducer import (
    MODEL_ADOPTED_NAME,
    MODEL_ADOPTED_NAMESPACE,
    USER_MESSAGE_FAILED_NAME,
    USER_MESSAGE_NAME,
    USER_MESSAGE_NAMESPACE,
    TimelineState,
    reduce_event,
)
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseDescription, LeaseManager

LOGGER = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT: timedelta = timedelta(minutes=15)
"""v1.0 §9.4「空闲超时回收」的默认阈值。回收的是 runtime 状态，不是会话映射。"""

INTERRUPT_CONFIRM_SECONDS: float = 5.0
"""批次十六第 2 件：``interrupt`` 之后等引擎确认的上限（秒）。

5 秒是「用户还愿意等」与「慢引擎也来得及收敛」的折中。等不到不是失败，
只是要如实说一句（见 :meth:`SessionHost.interrupt`）。
"""

INTERRUPT_POLL_SECONDS: float = 0.05
"""确认等待的轮询间隔。"""

CONVERSATION_DELETED_NAMESPACE = USER_MESSAGE_NAMESPACE
"""``DELETE /api/conversations/{id}`` 的收流通知走产品自己的 namespace（``kaus``）。"""

CONVERSATION_DELETED_NAME = "conversation.deleted"
"""同上：事件名。核心 union 已冻结，这条走 ``extension.event``（同 AD-86 的做法）。"""

RUNTIME_LOST_CODE = "runtime_lost"
"""AD-136 运行态自愈：合成 ``run.failed`` 时用的错误 code（前端按 code 分支）。"""

RESTORED_RUN_PREFIX = "restored:"
"""AD-143：从原生历史重建时间线时，那条合成 run 的 id 前缀。"""

RUNTIME_LOST_MESSAGE = (
    "引擎运行中断（后端重启或网关断开），这一轮没有收到结束事件"
)
"""同上：给人看的那句话。改文案不算破坏性变更，改 code 才算。"""

STOP_UNCONFIRMED_GRACE_SECONDS: float = 60.0
"""AD-147 第 3 件：``stopping-unconfirmed`` 挂多久之后才允许自愈。

Driver 侧自己等 30s（`STOP_CONFIRM_TIMEOUT`），这条兜底必须排在它后面——
两条路一起动手只会让同一轮出现两条终态。60s 是「Driver 那条路本身也出事了」
才轮得到的门槛。
"""

STOP_UNCONFIRMED_REASON = "stop_unconfirmed"
"""同上：合成 ``run.interrupted`` 的 ``reason``（与 Driver 侧同一个词）。"""

STOP_UNCONFIRMED_MESSAGE = (
    "这一轮以未确认的中断收场：停止请求已发出，引擎始终没有给出结束状态，"
    "且现在它那边也没有这一轮的活动了"
)
"""同上：给人看的那句话。"""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class SessionHostError(DomainError):
    """Session Host 层异常基类。"""


class RuntimeAlreadyActiveError(SessionHostError):
    """同一 Conversation 已经有一个活跃 runtime（见模块文档口径 1）。"""


class RuntimeNotActiveError(SessionHostError, KeyError):
    """该 Conversation 当前没有活跃 runtime。"""


class StaleRunError(SessionHostError):
    """The requested run is no longer the active run; no stop was forwarded."""


class BindingNotFoundError(SessionHostError, KeyError):
    """Conversation 指向的 Agent Binding 不存在。"""


class ExternalSurfaceActiveError(SessionHostError):
    """外部终端正持有这条会话的写权（AD-122：单写者）。

    Card 侧因此**不启动** runtime——同一 Native Session 不允许 Card 与 CLI
    并行写入（v1.0 §8.6）。``lease`` 是拦住这次启动的那条 lease，接入层把它
    原样放进 409 的 detail，前端据此显示「正在外部终端运行」与强制接管入口。
    """

    def __init__(self, message: str, *, lease: LeaseDescription) -> None:
        super().__init__(message)
        self.lease = lease


class DuplicateEventIdError(SessionHostError):
    """两条不同 Conversation 的事件用了同一个 ``eventId``（N §7.1：全局唯一）。"""


# --------------------------------------------------------------------------- #
# 带外变更信号（AD-20）
# --------------------------------------------------------------------------- #


@runtime_checkable
class OutOfBandWatcher(Protocol):
    """「原生存储被 Dashboard 之外的写者动过了吗」的抽象（AD-20 / v1.0 §8.8.2）。

    :meth:`poll` 返回 ``True`` 表示**自上次 poll 以来**发生过带外变更。实现可以是
    同步或异步的；Session Host 两种都接受。

    公共层刻意不知道探测手段：AD-20 定的「监视原生存储 WAL 的 mtime/size + SQL
    回读」是某个 Backend 的实现细节，属于 Driver 内部（N §5.4）。
    """

    def poll(self) -> bool | Awaitable[bool]: ...


# --------------------------------------------------------------------------- #
# 能力投影器（批次四十三）
# --------------------------------------------------------------------------- #


@runtime_checkable
class CapabilityProjector(Protocol):
    """「这条 Binding 的有效能力是什么」——建会话时问一次（批次四十三）。

    Session Host 不认识 Project Tree，也不该去读能力表（那是 ``app`` 层的事）；
    装配时注入这么一个可调用对象，它就能在建 runtime 之前问出一份有效能力集合，
    交给 Driver 去翻成自家协议的参数（见
    :meth:`~drivers.base.BackendDriver.session_options_for`）。

    **不装 = 与本批之前完全一样**：一个字都不解析、一个字都不送。测试里绝大多数
    夹具就停在这个状态。真正装上它的是 ``session_bootstrap.py``。
    """

    def __call__(self, binding: AgentBinding) -> Awaitable[Any]: ...


# --------------------------------------------------------------------------- #
# 订阅
# --------------------------------------------------------------------------- #

_CLOSED = object()


class EventSubscription:
    """一条 Conversation 的事件订阅：先重放，再跟随实时流。

    语义（v1.0 §8.5 断线重连）：

    - ``after_sequence=None`` → 从 Event Store 里现存的第一条开始；
    - ``after_sequence=N`` → 从 ``N`` 之后（开区间）开始，先补齐缓冲里的历史，
      再无缝接上实时事件；
    - **无重复**：实时队列里 ``sequence`` 不大于已产出者的事件被跳过；
    - **无遗漏**：队列在 :meth:`SessionHost.subscribe` 返回时就已注册，
      重放与实时之间不存在窗口期。
    """

    def __init__(
        self, host: "SessionHost", conversation_id: str, after_sequence: int | None
    ) -> None:
        self._host = host
        self.conversation_id = conversation_id
        self._last_sequence = after_sequence
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._replayed = False
        self._closed = False
        self._buffer: list[AgentEventEnvelope] = []

    # --- 生命周期 ------------------------------------------------------ #

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._host._detach_subscription(self)  # noqa: SLF001
            self._queue.put_nowait(_CLOSED)

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> "EventSubscription":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.close()

    # --- 迭代 ---------------------------------------------------------- #

    def __aiter__(self) -> "EventSubscription":
        return self

    async def __anext__(self) -> AgentEventEnvelope:
        while True:
            if not self._replayed:
                # 先取回来再置标志（AD-143）。反过来写的话，只要消费者在这一次
                # `await` 上被取消——SSE 的 keepalive 用的就是
                # `asyncio.wait_for`，客户端断开也会取消——`_replayed` 已经是真
                # 而 `_buffer` 还是空的：这条订阅从此只跟实时流，整段历史被永久
                # 吞掉。页面上的样子正是「GET 正常、SSE 200、一条事件都没有」。
                replayed = await self._host._event_store.replay(  # noqa: SLF001
                    self.conversation_id, after_sequence=self._last_sequence
                )
                self._buffer.extend(replayed)
                self._replayed = True
            if self._buffer:
                envelope = self._buffer.pop(0)
                self._last_sequence = envelope.sequence
                return envelope
            if self._closed and self._queue.empty():
                raise StopAsyncIteration
            item = await self._queue.get()
            if item is _CLOSED:
                raise StopAsyncIteration
            envelope: AgentEventEnvelope = item
            if (
                self._last_sequence is not None
                and envelope.sequence <= self._last_sequence
            ):
                # 重放已经覆盖过这一段：跳过，保证「无重复」。
                continue
            self._last_sequence = envelope.sequence
            return envelope

    async def drain_replay(self) -> list[AgentEventEnvelope]:
        """把「重放那一批」一次取出来，**不等**实时事件。

        批次二十八：SSE 走 Cloudflare 隧道时，代理会先攒够一小段字节才把响应
        转出去；重放批之后如果立刻进入长时间等待，页面就一直停在「历史没有随
        重放到达」。要在重放与等待之间插一段填充注释，就得先有一个「重放到此
        为止」的可判定时刻——``__anext__`` 把两者揉在同一个循环里，看不出边界，
        所以单独给一个方法。语义与 ``__anext__`` 完全一致（同一份 ``_buffer``、
        同一个 ``_last_sequence``），只是不阻塞：缓冲为空就返回空列表。
        """
        if not self._replayed:
            replayed = await self._host._event_store.replay(  # noqa: SLF001
                self.conversation_id, after_sequence=self._last_sequence
            )
            self._buffer.extend(replayed)
            self._replayed = True
        drained = self._buffer
        self._buffer = []
        if drained:
            self._last_sequence = drained[-1].sequence
        return drained

    # --- 供 Host 推送 --------------------------------------------------- #

    def _push(self, envelope: AgentEventEnvelope) -> None:
        if not self._closed:
            self._queue.put_nowait(envelope)


# --------------------------------------------------------------------------- #
# 运行时状态
# --------------------------------------------------------------------------- #


@dataclass
class _ConversationRuntime:
    conversation: Conversation
    binding: AgentBinding
    driver: BackendDriver
    handle: RuntimeHandle
    state: TimelineState
    next_sequence: int
    last_activity: datetime
    pump: "asyncio.Task[None] | None" = None
    failed: bool = False
    #: 批次十七第 5 件：点过停止但还没进终态的那一刻（``runState`` 的
    #: ``stopping-unconfirmed`` 就是它）。进终态或下一轮开始时清掉。
    interrupt_requested_at: datetime | None = None
    #: AD-162：这条会话背后的引擎有没有可回放的原生历史（``sessions.history``）。
    #: ``None`` = 问不出来，按老口径走 TTL。
    history_recoverable: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_dispatch: bool = False


# --------------------------------------------------------------------------- #
# Session Host
# --------------------------------------------------------------------------- #


class SessionHost:
    """N §9.3 的 Session Host。

    参数
    ----
    registry:
        Driver Registry —— 取 Driver 的唯一入口（N §3 / §5）。
    bindings:
        Agent Binding 仓库；Conversation 只带 ``agent_binding_id``，
        backend 与原生作用域从 Binding 上取。
    event_store:
        短期重放缓冲（D-16）。
    lease_manager:
        AD-11 的 lease 写入者；**信息性**，不参与准入。
    conversations:
        可选。给了就把 Conversation 的 ``state`` 收敛写回（running-card / idle /
        error），没给就只在内存里编排。
    idle_timeout:
        v1.0 §9.4 空闲回收阈值；实际回收由 :meth:`sweep_idle` 触发
        （不自建后台定时器：调度节奏由宿主决定，测试也因此是确定性的）。
    """

    def __init__(
        self,
        *,
        registry: BackendDriverRegistry,
        bindings: AgentBindingRepository,
        event_store: EventStore,
        lease_manager: LeaseManager,
        conversations: ConversationRepository | None = None,
        idle_timeout: timedelta = DEFAULT_IDLE_TIMEOUT,
        owner_id: str = "session-host",
        clock=_utcnow,
        interrupt_confirm_timeout: float = INTERRUPT_CONFIRM_SECONDS,
        capability_projector: "CapabilityProjector | None" = None,
    ) -> None:
        self._capability_projector = capability_projector
        self._registry = registry
        self._bindings = bindings
        self._event_store = event_store
        self._leases = lease_manager
        self._conversations = conversations
        self.idle_timeout = idle_timeout
        self.owner_id = owner_id
        self.interrupt_confirm_timeout = interrupt_confirm_timeout
        self._clock = clock

        self._runtimes: dict[str, _ConversationRuntime] = {}
        #: R2（批次三十七）：按会话串行化「启动」这件事。判断「有没有」与真的建起来
        #: 之间隔着好几个 ``await``，没有它两个请求会各开一条 runtime。
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._subscriptions: dict[str, list[EventSubscription]] = {}
        self._watchers: dict[str, OutOfBandWatcher] = {}
        self._advisories: dict[str, ConcurrencyAdvisory] = {}
        self._histories: dict[str, NativeHistory] = {}
        #: 批次四十三：这条会话最近一次建 runtime 时，**随协议参数送进引擎**的
        #: 那份项目能力摘要（只有名字）。没装投影器 / 这台引擎没有这条路时不写。
        self._session_projections: dict[str, SessionProjection] = {}
        #: 批次四十五 a：**每一条落库事件**的旁观者（:meth:`add_event_observer`）。
        self._event_observers: list[Any] = []

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def event_store(self) -> EventStore:
        """短期重放缓冲（D-16）。

        接入层需要它来算 ``?after=`` 的游标与做无 runtime 的重放；开成只读属性
        比让上层去摸 ``_event_store`` 好——后者会把「Host 拥有 Store」这条关系
        变成靠约定维持的东西。
        """
        return self._event_store

    @property
    def leases(self) -> LeaseManager:
        """Runtime Lease 的写入方（AD-11 / AD-122）。

        与 :attr:`event_store` 同一个理由开成只读属性：Phase 4 的交接编排要按
        AD-122 自己 ``try_acquire_*``，让它去摸 ``_leases`` 会把「Host 拥有
        LeaseManager」这条关系变成靠约定维持的东西。
        """
        return self._leases

    def active_conversation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtimes))

    def is_active(self, conversation_id: str) -> bool:
        return conversation_id in self._runtimes

    def handle_for(self, conversation_id: str) -> RuntimeHandle | None:
        runtime = self._runtimes.get(conversation_id)
        return runtime.handle if runtime is not None else None

    def timeline(self, conversation_id: str) -> TimelineState | None:
        """当前渲染态（Reducer 的输出）。runtime 停止后即释放，重开时从缓冲重建。"""
        runtime = self._runtimes.get(conversation_id)
        return runtime.state if runtime is not None else None

    def advisory_for(self, conversation_id: str) -> ConcurrencyAdvisory | None:
        """最近一次带外变更软提示（R-04；**不影响**能否发送）。"""
        return self._advisories.get(conversation_id)

    def last_refreshed_history(self, conversation_id: str) -> NativeHistory | None:
        """最近一次「发送前刷新」拉到的原生历史（R-02：原生历史才是权威账本）。"""
        return self._histories.get(conversation_id)

    async def describe_runtime_owner(
        self, conversation_id: str
    ) -> LeaseDescription | None:
        """当前 Runtime Owner（v1.0 §8.8.4：UI 必须始终显示）。"""
        return await self._leases.describe(conversation_id)

    # ------------------------------------------------------------------ #
    # 带外检测（AD-20）
    # ------------------------------------------------------------------ #

    def register_out_of_band_watcher(
        self, conversation_id: str, watcher: OutOfBandWatcher
    ) -> None:
        self._watchers[conversation_id] = watcher

    def unregister_out_of_band_watcher(self, conversation_id: str) -> None:
        self._watchers.pop(conversation_id, None)

    # ------------------------------------------------------------------ #
    # 启动 / 恢复
    # ------------------------------------------------------------------ #

    def _start_lock(self, conversation_id: str) -> asyncio.Lock:
        """这条会话的启动锁（R2（批次三十七））。

        懒创建：一个进程里会话数不设上限，预先给每条会话备一把锁没有意义；
        用完之后由 :meth:`_discard_start_lock` 收掉，字典不会随会话数单调增长。
        """
        lock = self._start_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._start_locks[conversation_id] = lock
        return lock

    def _discard_start_lock(self, conversation_id: str, lock: asyncio.Lock) -> None:
        """没人在等就把锁收掉。还有人排队时留着——收掉会让后来者换一把新锁，
        那等于没有锁。"""
        if lock.locked():
            return
        if self._start_locks.get(conversation_id) is lock:
            self._start_locks.pop(conversation_id, None)

    async def start_runtime(self, conversation: Conversation) -> RuntimeHandle:
        """为一条 Conversation 建立 Card Runtime。

        已有活跃 runtime 时抛 :class:`RuntimeAlreadyActiveError`——一条会话只能有
        一条事件流（模块文档口径 1）。想要「有就复用」的语义用
        :meth:`ensure_runtime`。

        恢复语义：启动时先把 Event Store 里现存的事件重放进 Reducer，
        并把 sequence 游标接到缓冲末尾之后，因此断线重启不会让 ``?after=`` 游标
        倒退（v1.0 §8.5）。

        **R2（批次三十七）**：整个启动过程在这条会话的启动锁里完成——「有没有活跃
        runtime」的判断、Driver 拉起、原生 id 落库与 Host 注册之间隔着好几个
        ``await``，不串行化的话两个并发请求会双双通过检查、各开一条 runtime
        （同 owner 的 lease 续期也拦不住），最后 Driver 手上两条、Host 只记一条。
        """
        lock = self._start_lock(conversation.id)
        try:
            async with lock:
                if conversation.id in self._runtimes:
                    raise RuntimeAlreadyActiveError(
                        "该 Conversation 已有活跃 runtime，同一会话不允许两个："
                        f"{conversation.id!r}"
                    )
                return await self._start_runtime_locked(conversation)
        finally:
            self._discard_start_lock(conversation.id, lock)

    async def _start_runtime_locked(
        self, conversation: Conversation
    ) -> RuntimeHandle:
        """:meth:`start_runtime` 的正文；调用方必须已经持有这条会话的启动锁。"""
        if self._conversations is not None:
            latest = await self._conversations.get(conversation.id)
            if latest is not None and latest.archived_at:
                raise UnsupportedCapabilityError("请先恢复此对话。")
        binding = await self._bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise BindingNotFoundError(
                f"未知的 Agent Binding：{conversation.agent_binding_id!r}"
            )
        driver = self._registry.get(binding.backend_id)

        # AD-122：先拿写权再拉起 Driver。顺序不能反——反过来的话，「外部终端正在
        # 写」这条会话仍然会被真的开出一个 Card runtime，我们只是事后不记账而已。
        now = self._clock()
        acquisition = await self._leases.try_acquire_card(
            conversation.id,
            owner_id=self.owner_id,
            metadata={"backendId": binding.backend_id},
            at=now,
        )
        if not acquisition.granted:
            conflict = acquisition.conflict
            assert conflict is not None  # granted=False 必然带 conflict
            raise ExternalSurfaceActiveError(
                "这条会话正由外部终端持有；先在终端里结束，或强制接管后再在站内发送",
                lease=conflict,
            )
        projection = await self._session_projection(conversation, binding, driver)
        try:
            if projection is None:
                handle = await driver.start_runtime(conversation, "card")
            else:
                handle = await driver.start_runtime(
                    conversation, "card", session_options=projection.options
                )
        except Exception:
            # 拉起失败就把刚拿到的写权还回去，否则这条会话会挂着一把没人用的锁，
            # 直到 ttl 过期为止。
            await self._leases.release(
                conversation.id, expected_owner_id=self.owner_id
            )
            raise

        # R2（批次三十七）：从这里往下任何一步失败或被取消，都必须把这条半初始化的
        # runtime 拆干净——Driver 侧那条也要停。否则 Host 手上什么都没有、
        # Driver 手上留着一条没人认领的实例，下一次启动就成了第二条引擎进程。
        try:
            state = TimelineState.initial(conversation.id)
            for envelope in await self._event_store.replay(conversation.id):
                state = reduce_event(state, envelope)

            runtime = _ConversationRuntime(
                conversation=conversation,
                binding=binding,
                driver=driver,
                handle=handle,
                state=state,
                next_sequence=await self._event_store.next_sequence(conversation.id),
                last_activity=now,
                history_recoverable=await self._history_recoverable(driver),
            )
            self._runtimes[conversation.id] = runtime

            # 拿到真实 runtime 身份之后把 lease 上的 backend_process_id 补齐
            # （R-10 的 reconcile 按它判存活）。owner 没变，这次一定成功。
            await self._leases.try_acquire_card(
                conversation.id,
                owner_id=self.owner_id,
                backend_process_id=handle.runtime_id,
                metadata={"backendId": handle.backend_id},
                at=now,
            )
            await self._save_conversation_state(runtime, "running-card")
            root = handle.metadata.get("workspaceRoot")
            if isinstance(root, str) and root:
                await self._emit_host_event(
                    runtime,
                    ExtensionEvent(namespace="kaus", name="runtime.workspace", data={"workspaceRoot": root}),
                    run_id=None,
                )
        except BaseException:
            self._runtimes.pop(conversation.id, None)
            await self._abandon_half_started(driver, handle, conversation.id)
            raise
        runtime.pump = asyncio.create_task(
            self._pump(runtime), name=f"session-host-pump:{conversation.id}"
        )
        return handle

    async def _session_projection(
        self,
        conversation: Conversation,
        binding: AgentBinding,
        driver: BackendDriver,
    ) -> SessionProjection | None:
        """建 runtime 之前问一次：这次要随协议参数送进去什么（批次四十三）。

        两个前提缺一不可，缺哪个都是「什么都不做」，而不是报错：

        1. 装了 :class:`CapabilityProjector`（没装 = 与本批之前完全一样）；
        2. 这台 Driver 实现了 ``session_options_for``（没实现 = 这台引擎没有
           「随会话送项目能力」这条路——不是所有引擎都有，有的引擎走的是写进
           自己持久配置的那条路，与本方法无关）。

        **算不出来不拦着会话起来。** 能力表读不到、Driver 翻译时抛了——那都不该
        让用户发不出消息；如实记一条日志，这一轮不带 MCP 照常开（N §13.1：
        降级要说出来，但降级本身是允许的）。
        """
        self._session_projections.pop(conversation.id, None)
        if self._capability_projector is None:
            return None
        hook = getattr(driver, "session_options_for", None)
        if hook is None:
            return None
        try:
            effective = await self._capability_projector(binding)
            projection = await hook(conversation, effective)
        except Exception:  # noqa: BLE001 - 投射算不出来不该拦住会话起来
            LOGGER.warning(
                "会话 %s 的项目能力没能算出来，这一轮不带能力照常开",
                conversation.id,
                exc_info=True,
            )
            return None
        if projection is None:
            return None
        self._session_projections[conversation.id] = projection
        return projection

    def session_projection(self, conversation_id: str) -> SessionProjection | None:
        """这条会话最近一次**送进引擎**的项目能力（批次四十三）。

        接入层用它回 ``GET /api/conversations/{id}`` 的 ``projection``——那一份
        **只有名字**。没送过（没装投影器 / 这台引擎没有这条路 / 这条会话是续接的）
        就是 ``None``，而不是一个空壳：两者在界面上要说的话不一样。
        """
        return self._session_projections.get(conversation_id)

    async def _history_recoverable(self, driver: BackendDriver) -> bool | None:
        """这台引擎能不能把原生历史交回来（AD-162）。

        判据是能力矩阵的 ``sessions.history`` 那一格，不是 Driver 的类名——加一台
        新引擎不该让保留期这条规则重写一遍。``unknown`` 与 ``unsupported`` 一样算
        「没有」：这是能力矩阵自己的口径（``bool(state)`` 只有 ``supported`` 为
        真），而在保留期这件事上，两种错误的代价并不对等——多留一段能删的事件，
        比删掉一段没有第二份的历史轻得多。

        问不出来（Driver 没有这个方法、探测抛错）返回 ``None``：不知道就不改
        既有口径（N §13.1）。
        """
        getter = getattr(driver, "get_capabilities", None)
        if getter is None:
            return None
        try:
            capabilities = await getter()
        except Exception:  # noqa: BLE001 - 探不出来不该拦住会话起来
            return None
        sessions = getattr(capabilities, "sessions", None)
        history = getattr(sessions, "history", None)
        if history is None:
            return None
        return bool(history)

    async def _abandon_half_started(
        self, driver: BackendDriver, handle: RuntimeHandle, conversation_id: str
    ) -> None:
        """拆掉一条没能建成的 runtime（R2（批次三十七））。

        两件事都不许因为对方抛错而漏做，也不许因为「当前任务已被取消」而漏做：
        取消路径正是这条清理最需要跑的时候（用户切走了页面 / 请求断了），
        所以两步各自吞掉自己的异常。
        """
        try:
            await driver.stop_runtime(handle)
        except BaseException:  # noqa: BLE001 - 清理路径不得因 Driver 抛错而中断
            pass
        try:
            await self._leases.release(
                conversation_id, expected_owner_id=self.owner_id
            )
        except BaseException:  # noqa: BLE001 - 同上
            pass

    def update_conversation(self, conversation: Conversation) -> None:
        """领域对象被改过之后，把活跃 runtime 上的那份换新（批次十六第 4 件）。

        runtime 建立时把 Conversation 抄了一份（Driver 也可能自己留了一份），
        会话级模型快照改了以后不换，这一轮仍然按旧快照发——那正是 AD-12 想避免
        的「界面上改了，实际没改」。没有活跃 runtime 时什么都不用做：下次
        ``start_runtime`` 自然读的是新的。
        """
        runtime = self._runtimes.get(conversation.id)
        if runtime is None:
            return
        runtime.conversation = conversation
        adopt = getattr(runtime.driver, "adopt_conversation", None)
        if adopt is not None:
            adopt(runtime.handle, conversation)

    @asynccontextmanager
    async def conversation_control(self, conversation_id: str):
        """Serialize settings with startup and message submission."""
        lock = self._start_lock(conversation_id)
        try:
            async with lock:
                runtime = self._runtimes.get(conversation_id)
                if runtime is None:
                    yield
                else:
                    async with runtime.control_lock:
                        if runtime.pending_dispatch or runtime.state.run_state == "running":
                            raise TurnAlreadyRunningError("上一轮尚未结束。")
                        yield
        finally:
            self._discard_start_lock(conversation_id, lock)

    async def ensure_runtime(self, conversation: Conversation) -> RuntimeHandle:
        """有就复用，没有就启动（恢复路径）。

        R2（批次三十七）：两次检查、一把锁。锁外那次是快路径（绝大多数调用会话早就
        跑着，不该为它排队）；锁内那次才是判据——第一个进来的把 runtime 建好，
        后来者在锁上等到的是**同一条**，而不是自己再开一条。
        """
        existing = self._runtimes.get(conversation.id)
        if existing is not None:
            return existing.handle
        lock = self._start_lock(conversation.id)
        try:
            async with lock:
                existing = self._runtimes.get(conversation.id)
                if existing is not None:
                    return existing.handle
                return await self._start_runtime_locked(conversation)
        finally:
            self._discard_start_lock(conversation.id, lock)

    # ------------------------------------------------------------------ #
    # 运行态自愈（AD-136 / 批次十七第 1 件）
    # ------------------------------------------------------------------ #

    async def timeline_from_store(self, conversation_id: str) -> TimelineState | None:
        """没有活跃 runtime 时，从 Event Store 重算一遍渲染态。

        与接入层的 ``_timeline_of`` 是同一件事，但这里是**权威实现**：自愈要判
        「最后一个 run 有没有终态」，那个判断必须与前端看到的时间线出自同一个
        Reducer，否则两边会各算各的。
        """
        live = self.timeline(conversation_id)
        if live is not None:
            return live
        envelopes = await self._event_store.replay(conversation_id)
        if not envelopes:
            return None
        state = TimelineState.initial(conversation_id)
        for envelope in envelopes:
            state = reduce_event(state, envelope)
        return state

    async def reconcile_conversation(self, conversation: Conversation) -> bool:
        """把「页面说在跑、后端其实早就没了」这种失配收敛掉（AD-136）。

        真机现象（`docs/quality/verify5.md` ②）：网关被停的那一轮没有任何终态
        事件，时间线于是永远停在 ``running``；用户点停止，后端却回一句「该
        Conversation 当前没有活跃 runtime」——页面与后端各说各话，而且没有任何
        一条路能让页面自己走出来。

        判据两条，**都**满足才动手：

        1. ``host.is_active`` 为假——本进程手上没有这条会话的 runtime；
        2. Event Store 里最后一个 run 没有终态（``run_state == "running"``）。

        动手 = 合成一条 ``run.failed{error.code="runtime_lost"}`` 落库并广播
        （信封 v1.1 冻结期内**只用已有的核心事件**，不新造类型），并把
        Conversation 的 ``state`` 写回 ``idle``。

        **外部终端持有写权时不动**：那时回合真的可能在别处跑着，我们只是看不到
        它的事件；合成一条「中断了」等于对用户撒谎。

        返回是否真的合成了那条终态。
        """
        conversation_id = conversation.id
        if self.is_active(conversation_id):
            # AD-147 第 3 件：本进程手上**有** runtime，但这一轮点过停止之后就
            # 再没动静了——那是另一种失配，判据不同，见下面那个方法。
            return await self._reconcile_stuck_stop(conversation)
        lease = await self._leases.describe(conversation_id)
        if (
            lease is not None
            and not getattr(lease, "is_stale", False)
            and getattr(lease, "owner_type", None) == "external-cli"
        ):
            return False
        state = await self.timeline_from_store(conversation_id)
        if state is None or state.run_state != "running":
            return False
        await self.emit_conversation_event(
            conversation,
            RunFailed(
                run_id=state.active_run_id,
                error=AgentError(
                    code=RUNTIME_LOST_CODE,
                    message=RUNTIME_LOST_MESSAGE,
                    retriable=True,
                ),
            ),
            run_id=state.active_run_id,
        )
        await self._mark_idle(conversation_id)
        return True

    async def _reconcile_stuck_stop(self, conversation: Conversation) -> bool:
        """「点了停止，然后就再也没有然后了」也要能自己走出来（AD-147 第 3 件）。

        AD-136 的自愈只管「页面说在跑、本进程手上没有 runtime」那一种失配，判据
        第一条就是 ``is_active`` 为假；而真机 25e6 卡住的时候 runtime **还在**
        （Driver 侧那一轮已经被收尾成死局，Session Host 并不知道），于是那条自愈
        永远够不着它。这里补上第二种失配。

        判据三条，**都**满足才动手：

        1. 这条会话点过停止且还没收敛（``runState == "stopping-unconfirmed"``）；
        2. 距离那一次点击已经超过 :data:`STOP_UNCONFIRMED_GRACE_SECONDS`；
        3. Driver 明确说这一轮**已经没有活动**（可选钩子 ``run_is_active`` 回
           ``False``）。回 ``True`` 或 ``None``（不知道 / 没有这条面）都不动——
           编一句「中断了」比不收敛更糟（N §13.1）。

        动手 = 一条 ``diagnostic.notice(warn)`` + 一条
        ``run.interrupted{reason:"stop_unconfirmed"}``，与 Driver 侧那条兜底同一
        形状（信封 v1.1 冻结期内只用已有事件）。
        """
        runtime = self._runtimes.get(conversation.id)
        if runtime is None or runtime.interrupt_requested_at is None:
            return False
        if runtime.state.run_state != "running":
            return False
        waited = (self._clock() - runtime.interrupt_requested_at).total_seconds()
        if waited < STOP_UNCONFIRMED_GRACE_SECONDS:
            return False
        probe = getattr(runtime.driver, "run_is_active", None)
        if probe is None:
            return False
        try:
            still_active = await probe(runtime.handle)
        except Exception:  # noqa: BLE001 - 自愈坏掉不该让「打开会话」失败
            return False
        if still_active is not False:
            return False
        run_id = runtime.state.active_run_id
        await self._emit_host_event(
            runtime,
            DiagnosticNotice(level="warn", message=STOP_UNCONFIRMED_MESSAGE),
            run_id=run_id,
        )
        await self._emit_host_event(
            runtime,
            RunInterrupted(run_id=run_id, reason=STOP_UNCONFIRMED_REASON),
            run_id=run_id,
        )
        await self._mark_idle(conversation.id)
        return True

    async def restore_from_native_history(self, conversation: Conversation) -> int:
        """缓冲是空的就从**原生历史**重建一份时间线（AD-143 / v1.0 §11.3）。

        真机现象（`docs/quality/verify-next.md` ②③）：跑完或运行中重新打开页面，
        消息内容为空——而同一条会话的原生历史里，用户那句话、工具调用与工具输出
        都在。之所以会这样，是因为**页面唯一的内容来源是 Event Store**，而
        Event Store 按定义是一块可以随时被清空的短期缓冲（D-16 / §8.5）：保留期
        到了、``DELETE`` 清过、上一个进程没来得及落库、事件流从头到尾没接上……
        任何一条都会让重开变成一张白纸。v1.0 §11.3 早就写明它整表可删、
        「删完重开卡片从原生历史重建」——**重建这一半此前没有实现**。

        所以这里不再去逐条堵「谁清了缓冲」，而是把重开这条路本身修成不依赖缓冲：

        - 只在**确实没有**任何缓冲事件时才做（``latest_sequence is None``）——
          有一条都不碰，重建绝不覆盖真实事件流；
        - 只在没有活跃 runtime 时做——正在跑的那一轮自己会往缓冲里写；
        - 只在这条会话已经绑了原生 Session、且它的 Driver 支持读历史时做；
          读不到（不支持 / 报错 / 空历史）就**什么都不发**，维持现状而不是编内容
          （N §13.1）。

        重建出来的事件走 :meth:`emit_conversation_event` 正常入库，因此
        ``?after=`` 重放、``timeline`` 摘要、``runState`` 全都自动一致。整段挂在
        一个合成的 ``run.*`` 上并以 ``run.completed`` 收尾——重建出来的是**已经
        发生过**的历史，不该让页面以为还有一轮在跑。

        返回补出来的事件条数（0 表示没做）。
        """
        conversation_id = conversation.id
        if self.is_active(conversation_id):
            return 0
        native_session_id = conversation.native_session_id
        if not native_session_id:
            return 0
        if await self._event_store.latest_sequence(conversation_id) is not None:
            return 0
        binding = await self._bindings.get(conversation.agent_binding_id)
        if binding is None:
            return 0
        driver = self._registry.try_get(binding.backend_id)
        if driver is None:
            return 0
        try:
            history = await driver.load_native_history(binding, native_session_id)
        except UnsupportedCapabilityError:
            return 0
        except Exception:  # noqa: BLE001 - 读不到历史不该让「打开会话」失败
            return 0
        events = _events_from_native_history(history, run_id=_restored_run_id(conversation_id))
        if not events:
            return 0
        for event, run_id in events:
            await self.emit_conversation_event(conversation, event, run_id=run_id)
        return len(events)

    async def reconcile_startup(self, *, limit: int = 500) -> tuple[str, ...]:
        """进程启动时把所有「落盘时还在跑」的会话过一遍（AD-136 的第一处）。

        只扫 ``state == "running-card"`` 的会话：那正是「上一个进程开着 runtime
        就没了」留下的痕迹，而 ``running-external`` 是外部终端的账，不归这里管。
        没有配 Conversation 仓库（纯内存编排的宿主）时什么都不做。

        返回被收敛的 Conversation id。**不抛异常**：自愈坏掉不能让服务起不来，
        单条失败只跳过这一条。
        """
        if self._conversations is None:
            return ()
        try:
            candidates = await self._conversations.list_recent(limit=limit)
        except Exception:  # noqa: BLE001 - 启动期的提示性扫描，坏了不拦服务
            return ()
        healed: list[str] = []
        for conversation in candidates:
            if conversation.state != "running-card":
                continue
            try:
                if await self.reconcile_conversation(conversation):
                    healed.append(conversation.id)
            except Exception:  # noqa: BLE001 - 单条失配不该拖垮整轮
                continue
        return tuple(healed)

    async def run_state_of(self, conversation: Conversation) -> str:
        """``idle | running | stopping-unconfirmed``（批次十七第 5 件）。

        页头此前只能靠时间线推断「在不在跑」，而时间线恰恰是会卡住的那个东西
        （AD-136）。这里由 **EventStore + host 手上的 runtime** 一起算：

        - 没有活跃 runtime → ``idle``（调用方应先跑一次
          :meth:`reconcile_conversation`，让时间线也跟着收敛）；
        - 有活跃 runtime 且这一轮没进终态 → ``running``；点过停止但引擎还没确认
          → ``stopping-unconfirmed``（AD-130：停止是请求不是事实）。
        """
        runtime = self._runtimes.get(conversation.id)
        if runtime is None:
            return "idle"
        if runtime.state.run_state != "running":
            return "idle"
        if runtime.interrupt_requested_at is not None:
            return "stopping-unconfirmed"
        return "running"

    async def _mark_idle(self, conversation_id: str) -> None:
        """把 Conversation 的 ``state`` 写回 ``idle``（自愈的落库那一半）。"""
        if self._conversations is None:
            return
        current = await self._conversations.get(conversation_id)
        if current is None or current.state == "idle":
            return
        await self._conversations.save(
            current.evolve(state="idle", updated_at=self._clock())
        )

    # ------------------------------------------------------------------ #
    # 转发
    # ------------------------------------------------------------------ #

    async def send_message(
        self, conversation_id: str, content: MessageInput | str
    ) -> None:
        runtime = self._require(conversation_id)
        async with runtime.control_lock:
            if self._require(conversation_id) is not runtime:
                raise StaleRunError("会话运行位置已经变化，请重试。")
            await self._send_message_locked(conversation_id, content)

    async def _send_message_locked(
        self, conversation_id: str, content: MessageInput | str
    ) -> None:
        """发送用户消息。

        AD-20 软提示流程：发送**前**先看带外信号，若原生存储被别人动过就先
        ``load_native_history`` 刷新，并留下一条软提示——**不阻断发送**。
        """
        runtime = self._require(conversation_id)
        if self._conversations is not None:
            latest = await self._conversations.get(conversation_id)
            if latest is not None and latest.archived_at:
                raise UnsupportedCapabilityError("请先恢复此对话。")
        await self._refresh_if_out_of_band(runtime)
        message = MessageInput(text=content) if isinstance(content, str) else content
        runtime.last_activity = self._clock()
        await self._leases.heartbeat(conversation_id, at=runtime.last_activity)
        # 批次八第 6 件：用户那句话也进时间线。**先落再发**——顺序就是它在
        # 时间线上的位置，落在 `run.started` 之后就成了「先有回复再有提问」。
        await self._emit_user_message(runtime, message)
        previous_interrupt = runtime.interrupt_requested_at
        runtime.interrupt_requested_at = None
        runtime.pending_dispatch = True
        try:
            await runtime.driver.send_message(runtime.handle, message)
        except Exception as exc:  # noqa: BLE001 - 原样抛回，只是先记一笔状态
            runtime.pending_dispatch = False
            runtime.interrupt_requested_at = previous_interrupt
            # R6（批次三十七）：引擎在接受之前就拒了这一句。AD-105 的「不回滚」
            # 不变——上面那条正文留在时间线上（顺序是对的，它确实被说出来过）；
            # 补的是一个**可持久化**的状态。此前这里什么都不补：页面上那句临时
            # 错误提示一刷新就没了，而那条用户消息看起来和被正常处理过的一模一样。
            await self._emit_user_message_failed(runtime, message, exc)
            raise

    async def interrupt(self, conversation_id: str, *, expected_run_id: str | None = None) -> None:
        runtime = self._require(conversation_id)
        async with runtime.control_lock:
            if self._require(conversation_id) is not runtime:
                raise StaleRunError("会话运行位置已经变化，停止请求未发送。")
            if expected_run_id is not None and (
                runtime.pending_dispatch or runtime.state.is_run_terminal
                or runtime.state.active_run_id != expected_run_id
            ):
                raise StaleRunError("这轮执行已经变化，停止请求没有发给新的执行。")
            await self._interrupt_locked(conversation_id)

    async def _interrupt_locked(self, conversation_id: str) -> None:
        """中断当前一轮，并**等一个确认**（批次十六第 2 件 / 走查 F2）。

        原来这里只是把调用转给 Driver 就返回了：Driver 没抛异常 = 我们对外说
        「已中断」。但「停止请求被接受」不等于「这一轮停了」——有的引擎的停止
        接口只表示「开始停」，回合可能还在跑，于是页面停在「正在回复」，用户点了
        停止却什么都没发生，也没有任何说明（走查 F2）。

        现在：调用之后最多等 :attr:`interrupt_confirm_timeout` 秒，看时间线有没有
        进终态（``run.completed`` / ``run.failed`` / ``run.interrupted`` 任一都算
        确认）。等不到就发一条 ``diagnostic.notice(level="warn")``——核心事件，
        信封 v1.1 冻结期内不新增类型（AD-27 / AD-86 同一口径）——并把这次真机
        证据回灌给 Driver（``note_interrupt_unconfirmed``，可选钩子），让能力矩阵
        如实降级，而不是继续声明「immediate」。

        **等不到确认不算失败**：不抛异常。中断请求确实发出去了，只是引擎没认。
        """
        runtime = self._require(conversation_id)
        runtime.last_activity = self._clock()
        already_terminal = runtime.state.is_run_terminal
        if not already_terminal:
            # 批次十七第 5 件：从这一刻起页头是「正在停止」，直到终态到达
            # （`_ingest` 里清掉）或这一轮本来就结束了。
            runtime.interrupt_requested_at = runtime.last_activity
        await runtime.driver.interrupt(runtime.handle)
        if already_terminal:
            # 本来就没有在跑的回合，没什么可确认的。
            return
        if await self._wait_for_run_terminal(runtime, self.interrupt_confirm_timeout):
            return
        await self._emit_host_event(
            runtime,
            DiagnosticNotice(
                level="warn",
                message=(
                    "引擎没有确认中断：停止请求已发出，但这一轮还没有进入终态；"
                    "如果它继续输出，请到引擎那一侧停止"
                ),
            ),
            run_id=runtime.state.active_run_id,
        )
        note = getattr(runtime.driver, "note_interrupt_unconfirmed", None)
        if note is not None:
            outcome = note()
            if inspect.isawaitable(outcome):
                await outcome

    async def _wait_for_run_terminal(
        self, runtime: _ConversationRuntime, timeout: float
    ) -> bool:
        """轮询时间线是否进终态。轮询而不是订阅：终态也可能由 Driver 自己合成，
        订阅只看得到「新到的事件」，而这里要的是「现在是不是已经收敛了」。
        """
        if timeout <= 0:
            return runtime.state.is_run_terminal
        deadline = time.monotonic() + timeout
        while True:
            if runtime.state.is_run_terminal:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(INTERRUPT_POLL_SECONDS, timeout))

    async def resolve_interaction(
        self,
        conversation_id: str,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None:
        runtime = self._require(conversation_id)
        runtime.last_activity = self._clock()
        await runtime.driver.resolve_interaction(
            runtime.handle, interaction_id, response
        )

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def subscribe(
        self, conversation_id: str, after_sequence: int | None = None
    ) -> EventSubscription:
        """订阅一条 Conversation 的事件（WS/SSE 的 ``?after=<sequence>``）。

        **同步方法**：队列在返回前就已挂上，因此「注册 → 开始迭代」之间不会漏事件。
        订阅不要求 runtime 处于活跃状态——重放纯缓冲的历史也走同一个入口。
        """
        subscription = EventSubscription(self, conversation_id, after_sequence)
        self._subscriptions.setdefault(conversation_id, []).append(subscription)
        return subscription

    def subscriber_count(self, conversation_id: str) -> int:
        """当前挂在这条 Conversation 上的订阅数（AD-61 的可观测点）。

        断线只该让这个数掉，不该让 runtime 掉——有了它，「断流不影响 Runtime」
        就是可断言的，而不是靠读代码相信。
        """
        return len(self._subscriptions.get(conversation_id, ()))

    def _detach_subscription(self, subscription: EventSubscription) -> None:
        bucket = self._subscriptions.get(subscription.conversation_id)
        if not bucket:
            return
        if subscription in bucket:
            bucket.remove(subscription)
        if not bucket:
            self._subscriptions.pop(subscription.conversation_id, None)

    # ------------------------------------------------------------------ #
    # 停止 / 回收
    # ------------------------------------------------------------------ #

    async def stop_runtime(
        self, conversation_id: str, *, state: str = "idle"
    ) -> None:
        """停止 runtime。保留 Conversation 与原生 Session 映射（v1.0 §9.4）。"""
        runtime = self._runtimes.pop(conversation_id, None)
        if runtime is None:
            return
        pump = runtime.pump
        if pump is not None and pump is not asyncio.current_task():
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        try:
            await runtime.driver.stop_runtime(runtime.handle)
        except Exception:  # noqa: BLE001 - 停止路径不得因 Driver 抛错而卡住回收
            pass
        # AD-11：runtime 停止即释放自己的 lease；不碰别人的（如 external-cli）。
        await self._leases.release(conversation_id, expected_owner_id=self.owner_id)
        await self._save_conversation_state(runtime, state)

    async def sweep_idle(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """v1.0 §9.4 空闲超时回收。返回被回收的 Conversation id。

        正在跑的一轮不会被回收（``run_state == "running"``）——空闲的定义是
        「既没有用户动作，也没有事件流入」。
        """
        at = now or self._clock()
        doomed = [
            conversation_id
            for conversation_id, runtime in self._runtimes.items()
            if runtime.state.run_state != "running"
            and at - runtime.last_activity >= self.idle_timeout
        ]
        for conversation_id in doomed:
            await self.stop_runtime(conversation_id)
        return tuple(doomed)

    async def purge_expired_events(self, *, now: datetime | None = None) -> int:
        """v1.0 §8.5 保留期清理的调度入口。"""
        return await self._event_store.purge_expired(now=now)

    async def purge_conversation(self, conversation: Conversation) -> int:
        """会话被删除：清空它的事件缓冲，并让在线的订阅者干净地收流。

        调用方（``DELETE /api/conversations/{id}``）的顺序是「先 ``stop_runtime``
        → 再删领域记录 → 最后调这里」。本方法**不碰**领域库，也**不碰**原生
        Session（v1.0 §16.6：删 Conversation 不删原生会话）。

        订阅者拿到的最后一帧是一条 ``kaus/conversation.deleted`` 扩展事件，之后
        订阅结束。为什么是 ``extension.event``：AgentEventEnvelope v1.1 的 30 条
        union 已经冻结，「会话被删了」不是 Backend 产生的事实、也不该为它开一条
        新的核心事件——与 ``kaus/user.message``（AD-86）同一个出口、同一个
        namespace。

        它的 ``sequence`` 取「清空**前**的最后一个 + 1」：订阅端会跳过序号不大于
        已产出者的事件，从 0 开始重来的话，正在看这条会话的人反而收不到它。
        这条通知**不入库**——缓冲马上就要被清空，落一行进去只会让下一次
        ``?after=`` 重放出一条已删会话的尾巴。
        """
        conversation_id = conversation.id
        latest = await self._event_store.latest_sequence(conversation_id)
        purged = await self._event_store.purge_conversation(conversation_id)
        self._broadcast(
            await self._deleted_envelope(
                conversation, sequence=0 if latest is None else latest + 1
            )
        )
        for subscription in tuple(self._subscriptions.get(conversation_id, ())):
            subscription.close()
        # 顺带把只属于这条会话的内存态放掉——留着就是纯泄漏。
        self._watchers.pop(conversation_id, None)
        self._advisories.pop(conversation_id, None)
        self._histories.pop(conversation_id, None)
        return purged

    async def _deleted_envelope(
        self, conversation: Conversation, *, sequence: int
    ) -> AgentEventEnvelope:
        """造 ``kaus/conversation.deleted`` 的信封。

        ``source`` 是信封头的必填位，而这条通知由 Session Host 合成、不来自任何
        Driver：Binding 还在且它的 Driver 已注册就报那个 Driver 的 kind，否则退到
        ``native``。「Driver 没注册」恰恰是这条端点最常见的来路（首条消息发不出去
        留下的空会话），不能因为报不出 kind 就不通知。
        """
        binding = await self._bindings.get(conversation.agent_binding_id)
        backend_id = binding.backend_id if binding is not None else "backend:unknown"
        driver = self._registry.try_get(backend_id) if binding is not None else None
        return make_envelope(
            event=ExtensionEvent(
                namespace=CONVERSATION_DELETED_NAMESPACE,
                name=CONVERSATION_DELETED_NAME,
                data={"conversationId": conversation.id},
            ),
            project_id=conversation.project_id,
            conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id,
            backend_id=backend_id,
            native_session_id=conversation.native_session_id,
            run_id=None,
            sequence=sequence,
            occurred_at=self._clock(),
            event_id=new_event_id(),
            source=EventSource(
                driver_kind=driver.driver_kind if driver is not None else "native",
                driver_version=None,
            ),
        )

    async def aclose(self) -> None:
        """停掉全部 runtime 并关闭全部订阅。"""
        for conversation_id in tuple(self._runtimes):
            await self.stop_runtime(conversation_id)
        for bucket in tuple(self._subscriptions.values()):
            for subscription in tuple(bucket):
                subscription.close()

    # ------------------------------------------------------------------ #
    # 事件泵
    # ------------------------------------------------------------------ #

    async def _pump(self, runtime: _ConversationRuntime) -> None:
        """把 Driver 的事件流灌进 Event Store + Reducer + 订阅者。

        Driver 崩溃（迭代器抛异常）→ 合成 ``run.failed`` 并收敛状态：
        Timeline 进终态、Conversation 置 ``error``、lease 释放、runtime 移除。
        """
        try:
            stream = runtime.driver.events(runtime.handle)
            if inspect.isawaitable(stream):
                stream = await stream
            async for envelope in stream:
                await self._ingest(runtime, envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Driver 崩溃必须收敛为显式终态
            await self._fail_runtime(runtime, exc)

    async def _ingest(
        self, runtime: _ConversationRuntime, envelope: AgentEventEnvelope
    ) -> None:
        """去重 → 重排 sequence → 落 Event Store → 喂 Reducer → 广播。"""
        existing = await self._event_store.find(envelope.event_id)
        if existing is not None:
            if existing.conversation_id != envelope.conversation_id:
                # N §7.1：eventId 全局唯一。撞车说明 Driver 的 id 生成有问题——
                # 这里必须喊出来，否则会表现为「另一条会话莫名其妙丢事件」。
                raise DuplicateEventIdError(
                    f"eventId 必须全局唯一：{envelope.event_id!r} 已属于 "
                    f"{existing.conversation_id!r}"
                )
            # N §7.3 规则 9：重复投递幂等，不占新 sequence、不再广播。
            return
        stamped = envelope.model_copy(update={"sequence": runtime.next_sequence})
        runtime.next_sequence += 1
        outcome = await self._event_store.append(
            stamped, history_recoverable=runtime.history_recoverable
        )
        if outcome.is_duplicate:  # pragma: no cover - contains() 已挡住
            return
        runtime.state = reduce_event(runtime.state, stamped)
        if stamped.event.type == "run.started" and stamped.parent_run_id is None:
            runtime.pending_dispatch = False
        if runtime.state.is_run_terminal:
            # 收敛了就不再是「正在停止」（批次十七第 5 件）。
            runtime.interrupt_requested_at = None
        runtime.last_activity = self._clock()
        await self._sync_conversation_state(runtime)
        self._broadcast(stamped)
        await self._apply_model_adoption(runtime, stamped)
        await self._notify_observers(stamped)

    async def _apply_model_adoption(
        self, runtime: _ConversationRuntime, envelope: AgentEventEnvelope
    ) -> None:
        """AD-155：Driver 说「这条会话从这里起按 X 跑」→ 把快照落库。

        为什么由 Host 落库而不是 Driver：Conversation 是领域记录，Driver 只认它
        手上那一份副本（``adopt_conversation``）。事件是两边共同的事实源——
        Driver 发一条 ``kaus/model.adopted``，Host 见到它就把快照写成事实，界面
        上那句「这条会话指定模型为 A，而引擎实际会用 B」自此消失，而不是每发一句
        重新算一次。

        幂等：快照已经是这个值就什么都不做（重复投递、重放都会走到这里）。
        """
        event = envelope.event
        if getattr(event, "type", None) != "extension.event":
            return
        if (
            getattr(event, "namespace", None) != MODEL_ADOPTED_NAMESPACE
            or getattr(event, "name", None) != MODEL_ADOPTED_NAME
        ):
            return
        data = getattr(event, "data", None)
        target = data.get("to") if isinstance(data, Mapping) else None
        if not isinstance(target, str) or not target:
            return
        if self._conversations is None:
            return
        current = await self._conversations.get(runtime.conversation.id)
        if current is None:
            current = runtime.conversation
        if current.model_id == target:
            return
        saved = await self._conversations.save(
            current.snapshot_model(model_id=target, at=self._clock())
        )
        runtime.conversation = saved
        adopt = getattr(runtime.driver, "adopt_conversation", None)
        if callable(adopt):
            # Driver 手上那一份也要换，否则下一句还按旧快照再采纳一次。
            adopt(runtime.handle, saved)

    def _broadcast(self, envelope: AgentEventEnvelope) -> None:
        for subscription in tuple(
            self._subscriptions.get(envelope.conversation_id, ())
        ):
            subscription._push(envelope)  # noqa: SLF001

    # ------------------------------------------------------------------ #
    # 旁观者（批次四十五 a）
    # ------------------------------------------------------------------ #

    def add_event_observer(self, observer: Any) -> None:
        """登记一个**每条落库事件都过一遍**的异步回调。

        为什么不用 :meth:`subscribe`：``EventSubscription`` 的坐标是**一条会话**，
        而组要问的是「**任何一条**成员会话刚刚跑完了一轮吗」。用订阅实现，就得为
        每个成员各挂一条长期订阅、并在加入/移出/进程重启时维护它们的生死——那是
        一套影子生命周期，只为拿到一件本来就从这里流过的事实。

        约定三条，缺一条就会把「记一行时间线」变成「引擎跑不动」：

        - 回调是 ``async``，接一个 :class:`AgentEventEnvelope`，返回值不看；
        - 它在事件**落库并广播之后**才被调用，所以它看到的与订阅者看到的是同一份；
        - **它抛的异常一律被吞掉并记一条 WARNING**：旁观者坏了是旁观者的事，
          不该让这一轮的事件流断在这里。
        """
        self._event_observers.append(observer)

    async def _notify_observers(self, envelope: AgentEventEnvelope) -> None:
        for observer in tuple(self._event_observers):
            try:
                await observer(envelope)
            except Exception:  # noqa: BLE001 - 见 add_event_observer 第三条
                LOGGER.warning(
                    "事件旁观者处理 %s 时出错（已忽略，不影响事件流）",
                    envelope.event.type,
                    exc_info=True,
                )

    async def _emit_host_event(
        self, runtime: _ConversationRuntime, event: AgentEvent, *, run_id: str | None
    ) -> None:
        """Session Host 自己合成一条事件（崩溃收敛、软提示）。

        它走与 Driver 事件**完全相同**的入库/归并/广播路径，因此断线重连时
        这些事件同样能被 ``?after=`` 重放到。
        """
        conversation = runtime.conversation
        envelope = make_envelope(
            event=event,
            project_id=conversation.project_id,
            conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id,
            backend_id=runtime.binding.backend_id,
            native_session_id=conversation.native_session_id,
            run_id=run_id,
            sequence=0,  # 由 _ingest 统一重排
            occurred_at=self._clock(),
            event_id=new_event_id(),
            source=EventSource(
                driver_kind=runtime.driver.driver_kind, driver_version=None
            ),
        )
        await self._ingest(runtime, envelope)

    async def _emit_user_message(
        self, runtime: _ConversationRuntime, message: MessageInput
    ) -> None:
        """把用户发的这句话作为一条事件落进 Event Store（批次八第 6 件）。

        为什么是 ``extension.event`` 而不是新事件、也不是 ``message.*``：
        AgentEventEnvelope v1.1 的 30 条 union 已经冻结，新增事件属于 v1.2；
        而 ``message.started`` 的 ``role`` 是 ``Literal["assistant"]``，放宽它
        是改一个已发布字段的类型，不在「只加可选字段」的允许范围内。N §7.3
        规则 7 给原生/实验事件留的出口就是 ``extension.event``，这里用产品自己的
        namespace（``kaus``）而不是任何一家 Backend 的。Reducer 认得这一对
        namespace/name，把它归成 ``role="user"`` 的消息卡。详见
        :mod:`runtime.event_reducer` 的模块 docstring。

        走 :meth:`_emit_host_event` = 与 Driver 事件同一条入库/归并/广播路径，
        因此 ``?after=<sequence>`` 重放同样能拿到它。
        """
        data: dict[str, Any] = {
            "text": message.text,
            "role": "user",
            "attachmentCount": len(message.attachments),
        }
        if message.attachments:
            # Only authenticated, scoped references are retained. Prepared content
            # is excluded by AttachmentRef so base64/text cannot bloat the ledger.
            data["attachments"] = [a.model_dump(mode="json", by_alias=True, exclude_none=True) for a in message.attachments]
        # AD-94：调用方给了对账编号就原样带回，没给就**不放这个键**——
        # 让「没有 clientRef」在 wire 上是「键不存在」而不是「键为 null」，
        # 前端对账时不用区分这两种空。事件走正常入库路径，`?after=` 重放照样有它。
        if message.client_ref is not None:
            data["clientRef"] = message.client_ref
        await self._emit_host_event(
            runtime,
            ExtensionEvent(
                namespace=USER_MESSAGE_NAMESPACE,
                name=USER_MESSAGE_NAME,
                data=data,
            ),
            # 用户消息不属于任何一轮——它是**发起**下一轮的东西，
            # run.started 还没来。挂到上一轮的 runId 上会让它显示在上一轮里。
            run_id=None,
        )

    async def _emit_user_message_failed(
        self,
        runtime: _ConversationRuntime,
        message: MessageInput,
        exc: BaseException,
    ) -> None:
        """R6：为上一条 ``kaus/user.message`` 补一个「引擎没接下」的状态。

        与用户消息**同一个出口**（``extension.event`` / namespace ``kaus``）、
        同一条入库广播路径、同一个保留期——它是那条正文的一部分状态，不是诊断，
        按 24 小时清掉会让「刷新后还看得见失败」这件事只成立一天。

        ``code`` 取 Driver 给的稳定码（``failure.code``，AD-108 的形状）；Driver
        没给就退回一个按异常类型分档的稳定码，**不是**异常类名——类名重构一次就变，
        而前端是按 code 分支的。``message`` 是给人看的那句话。

        这条事件**不新建卡片**（Reducer 只把已有那条标成失败），所以它不会让
        「这句话说了几遍」变得答不出来。
        """
        failure = getattr(exc, "failure", None)
        if isinstance(failure, FailureHint):
            code = failure.code
            text = failure.message
        elif isinstance(exc, TurnAlreadyRunningError):
            code = "turn_already_running"
            text = str(exc) or "上一轮还在运行，这一句没有发出去"
        elif isinstance(exc, UnsupportedCapabilityError):
            code = "unsupported_capability"
            text = str(exc) or "这台引擎不支持这次发送"
        elif isinstance(exc, DriverError):
            code = "message_rejected"
            text = str(exc) or "引擎拒绝了这一句"
        else:
            code = "send_failed"
            text = str(exc) or "这一句没有发出去"
        data: dict[str, Any] = {"code": code, "message": text}
        # AD-94 的老规矩：没给对账编号就**不放这个键**，不放 null。
        if message.client_ref is not None:
            data["clientRef"] = message.client_ref
        await self._emit_host_event(
            runtime,
            ExtensionEvent(
                namespace=USER_MESSAGE_NAMESPACE,
                name=USER_MESSAGE_FAILED_NAME,
                data=data,
            ),
            run_id=None,
        )

    async def emit_conversation_event(
        self,
        conversation: Conversation,
        event: AgentEvent,
        *,
        run_id: str | None = None,
    ) -> AgentEventEnvelope:
        """由**接入层**合成一条事件并入库 + 广播（Phase 4 的交接通知）。

        与 :meth:`_emit_host_event` 的区别只有一个：这条路径**不要求**有活跃
        runtime。Card ⇄ External CLI 的交接恰恰发生在没有 Card runtime 的时候
        （终端活跃期间站内是只读的），而这些通知必须能被 ``?after=`` 重放到，
        否则用户刷新一次页面就看不到「已切到外部终端」了。

        有活跃 runtime 时仍然走 :meth:`_ingest`，让 sequence 与 Driver 事件在
        同一条流水线上分配（模块文档口径 2）。
        """
        runtime = self._runtimes.get(conversation.id)
        if runtime is not None:
            await self._emit_host_event(runtime, event, run_id=run_id)
            # `_ingest` 会重排 sequence，这里回读最后一条即为它。
            return (await self._event_store.replay(conversation.id))[-1]
        binding = await self._bindings.get(conversation.agent_binding_id)
        backend_id = binding.backend_id if binding is not None else "backend:unknown"
        driver = self._registry.try_get(backend_id) if binding is not None else None
        envelope = make_envelope(
            event=event,
            project_id=conversation.project_id,
            conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id,
            backend_id=backend_id,
            native_session_id=conversation.native_session_id,
            run_id=run_id,
            sequence=await self._event_store.next_sequence(conversation.id),
            occurred_at=self._clock(),
            event_id=new_event_id(),
            source=EventSource(
                driver_kind=driver.driver_kind if driver is not None else "native",
                driver_version=None,
            ),
        )
        outcome = await self._event_store.append(
            envelope,
            history_recoverable=(
                await self._history_recoverable(driver) if driver is not None else None
            ),
        )
        if not outcome.is_duplicate:
            self._broadcast(envelope)
            # Host 自己合成的终态（``runtime_lost`` 的 ``run.failed``、
            # ``stop_unconfirmed`` 的 ``run.interrupted``）也要过旁观者：对组
            # 时间线来说，「这一轮是被自愈收掉的」和「引擎自己报了失败」是同一
            # 件事——成员那一轮结束了，而且没有正文。
            await self._notify_observers(envelope)
        return envelope

    async def _fail_runtime(
        self, runtime: _ConversationRuntime, exc: BaseException
    ) -> None:
        """Driver 崩溃的收敛路径（N §9.3「处理错误和恢复」）。"""
        if runtime.failed:
            return
        runtime.failed = True
        await self._emit_host_event(
            runtime,
            RunFailed(
                run_id=runtime.state.active_run_id,
                error=AgentError(
                    code="runtime.driver_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    retriable=True,
                ),
            ),
            run_id=runtime.state.active_run_id,
        )
        self._runtimes.pop(runtime.conversation.id, None)
        try:
            await runtime.driver.stop_runtime(runtime.handle)
        except Exception:  # noqa: BLE001 - 已经在失败路径上了
            pass
        await self._leases.release(
            runtime.conversation.id, expected_owner_id=self.owner_id
        )
        await self._save_conversation_state(runtime, "error")

    # ------------------------------------------------------------------ #
    # 带外刷新（AD-20 骨架）
    # ------------------------------------------------------------------ #

    async def _refresh_if_out_of_band(self, runtime: _ConversationRuntime) -> None:
        watcher = self._watchers.get(runtime.conversation.id)
        if watcher is None:
            return
        changed = watcher.poll()
        if inspect.isawaitable(changed):
            changed = await changed
        if not changed:
            return
        advisory = ConcurrencyAdvisory(
            conversation_id=runtime.conversation.id,
            detected_at=self._clock(),
            detection_source="out-of-band-watcher",
        )
        self._advisories[runtime.conversation.id] = advisory
        await self._refresh_native_history(runtime)
        # 软提示进事件流：UI 的提示条与 Composer 状态无关（D-09 / R-04）。
        await self._emit_host_event(
            runtime,
            DiagnosticNotice(level="warning", message=advisory.message),
            run_id=runtime.state.active_run_id,
        )

    async def _refresh_native_history(self, runtime: _ConversationRuntime) -> None:
        """发送前刷新到最新原生历史（v1.0 §8.8.2）。

        Backend 不支持读历史时**不视为错误**：软提示照旧，发送照旧
        （N §13.1：不支持是显式状态，不是失败）。
        """
        native_session_id = runtime.conversation.native_session_id
        if native_session_id is None:
            return
        try:
            history = await runtime.driver.load_native_history(
                runtime.binding, native_session_id
            )
        except UnsupportedCapabilityError:
            return
        self._histories[runtime.conversation.id] = history

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def _require(self, conversation_id: str) -> _ConversationRuntime:
        runtime = self._runtimes.get(conversation_id)
        if runtime is None:
            raise RuntimeNotActiveError(
                f"该 Conversation 当前没有活跃 runtime：{conversation_id!r}"
            )
        return runtime

    async def _sync_conversation_state(
        self, runtime: _ConversationRuntime
    ) -> None:
        """每条事件之后把落盘的 ``state`` 跟上 Reducer 的运行态（AD-159）。

        旧字段 ``state`` 此前只在两个时刻被写：``start_runtime``（→ running-card）
        与 ``stop_runtime``（→ idle）。而一轮跑完之后 Runtime 还留着（空闲回收前
        不停），于是库里那一行就一直停在 ``running-card``——真机上
        ``GET /api/conversations/{id}`` 因此同时给出 ``state=running-card`` 与
        ``runState=idle``（VERIFY-BATCH-31 / 34）。接入层已经在**读**的那一刻把它
        导出成 ``runState`` 的投影；这里补上**写**的那一半，让下一个进程、侧栏的
        增量刷新、以及任何直接读库的人看到的也是同一个事实。

        只动 ``idle`` ⇄ ``running-card`` 这一对：``running-external`` 是终端的账，
        ``error`` 是 :meth:`_fail_runtime` 的结论，``paused`` / ``ended`` 是人定的，
        都不归运行态管。
        """
        if self._conversations is None:
            return
        current = runtime.conversation.state
        if current not in ("idle", "running-card"):
            return
        wanted = "running-card" if runtime.state.run_state == "running" else "idle"
        if current == wanted:
            return
        await self._save_conversation_state(runtime, wanted)

    async def _save_conversation_state(
        self, runtime: _ConversationRuntime, state: str
    ) -> None:
        """写回会话状态，顺带认领 Driver 懒创建出来的原生 Session id。

        懒创建（N §9）意味着 ``start_runtime`` 之后 ``RuntimeHandle`` 上才第一次
        有原生 id。不写回的话这条 Conversation 永远是「没绑原生会话」：`/history`
        恒 409，而下一次开 Runtime 又会再建一个新的原生会话——多轮记忆在重启处断掉，
        引擎里还会堆一堆没人认领的孤儿会话。

        只在**本来是空**的时候认领：已经绑好的 id 是账本（D-16），不因为 Driver
        这一轮解析到别的分段就被悄悄改写。
        """
        if self._conversations is None:
            return
        current = await self._conversations.get(runtime.conversation.id)
        if current is None:
            return
        changes: dict[str, Any] = {}
        if current.state != state:
            changes["state"] = state
        native_session_id = runtime.handle.native_session_id
        if native_session_id and current.native_session_id is None:
            changes["native_session_id"] = native_session_id
        if not changes:
            return
        runtime.conversation = await self._conversations.save(
            current.evolve(**changes, updated_at=self._clock())
        )


def _restored_run_id(conversation_id: str) -> str:
    """重建历史挂的那条合成 run 的 id。

    带 ``restored:`` 前缀而不是随便造一个 uuid：它出现在 wire 上，读到它的人
    应该一眼看出「这一轮不是我们看着跑完的，是从原生历史补出来的」。
    """
    return f"{RESTORED_RUN_PREFIX}{conversation_id}"


def _events_from_native_history(
    history: NativeHistory, *, run_id: str
) -> tuple[tuple[AgentEvent, str | None], ...]:
    """原生历史 → 一串公共事件（AD-143）。

    只翻译**看得懂**的四种行，其余（system / decision / 空正文）跳过——重建的
    目的是让重开的人看见自己和助手说过什么、跑过什么工具，不是把 Backend 的
    每一行都搬上时间线。

    产出形状与真回合一致：``run.started`` → 内容 → ``run.completed``。用户那句话
    仍走 ``kaus/user.message``（AD-86，信封 v1.1 冻结），并带 ``restored: true``
    好让前端将来想区分时有得可分；助手正文用 ``message.started`` +
    ``message.completed`` 一次给全（重建没有 delta 可言）。
    """
    body: list[tuple[AgentEvent, str | None]] = []
    for index, entry in enumerate(history.entries):
        metadata = dict(entry.metadata or {})
        text = entry.text or ""
        if entry.kind == "tool_call":
            raw_calls = metadata.get("toolCalls")
            calls = raw_calls if isinstance(raw_calls, list) else []
            for position, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id") or call.get("call_id")
                function = call.get("function")
                name = None
                arguments: Any = None
                if isinstance(function, dict):
                    name = function.get("name")
                    arguments = function.get("arguments")
                body.append(
                    (
                        ToolStarted(
                            call_id=str(call_id or f"{entry.entry_id}:{position}"),
                            name=str(name or metadata.get("toolName") or "tool"),
                            input=arguments,
                        ),
                        run_id,
                    )
                )
            continue
        if entry.kind == "tool_result":
            call_id = metadata.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                continue
            body.append(
                (ToolCompleted(call_id=call_id, output=text or None), run_id)
            )
            continue
        if not text:
            continue
        if entry.role == "user":
            body.append(
                (
                    ExtensionEvent(
                        namespace=USER_MESSAGE_NAMESPACE,
                        name=USER_MESSAGE_NAME,
                        data={
                            "text": text,
                            "role": "user",
                            "attachmentCount": 0,
                            # 这条不是当时广播出去的那一条，而是事后从原生历史
                            # 补出来的；如实说一句，别让它冒充实时事件。
                            "restored": True,
                        },
                    ),
                    # 用户消息不属于任何一轮（与 `_emit_user_message` 同一口径）。
                    None,
                )
            )
        elif entry.role == "assistant":
            message_id = f"{run_id}:m{index}"
            body.append((MessageStarted(message_id=message_id), run_id))
            body.append((MessageCompleted(message_id=message_id, text=text), run_id))
    if not body:
        return ()
    return (
        (RunStarted(run_id=run_id), run_id),
        *body,
        (RunCompleted(run_id=run_id), run_id),
    )


async def collect(
    subscription: EventSubscription, *, count: int
) -> tuple[AgentEventEnvelope, ...]:
    """从订阅里取 ``count`` 条事件（宿主与测试的便捷函数）。"""
    collected: list[AgentEventEnvelope] = []
    async for envelope in subscription:
        collected.append(envelope)
        if len(collected) >= count:
            break
    return tuple(collected)


__all__ = [
    "BindingNotFoundError",
    "CONVERSATION_DELETED_NAME",
    "CONVERSATION_DELETED_NAMESPACE",
    "INTERRUPT_CONFIRM_SECONDS",
    "DuplicateEventIdError",
    "DEFAULT_IDLE_TIMEOUT",
    "EventSubscription",
    "ExternalSurfaceActiveError",
    "OutOfBandWatcher",
    "RESTORED_RUN_PREFIX",
    "RUNTIME_LOST_CODE",
    "RUNTIME_LOST_MESSAGE",
    "STOP_UNCONFIRMED_GRACE_SECONDS",
    "STOP_UNCONFIRMED_MESSAGE",
    "STOP_UNCONFIRMED_REASON",
    "RuntimeAlreadyActiveError",
    "RuntimeNotActiveError",
    "SessionHost",
    "SessionHostError",
    "collect",
]
