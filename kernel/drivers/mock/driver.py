"""MockDriver：由脚本驱动的 BackendDriver 实现（N §5.1 的 ``mock`` 类）。

职责
----
在完全不运行真实 Agent 的情况下，走通 N §5.3 的整份 Driver 契约与 N §7 的
完整事件生命周期，用于：

- N Phase 2 验收「可通过 Mock Driver 创建一条 ``group_spawned`` Conversation 并加入」；
- N Phase 3A 验收「不运行真实 Backend 也能展示完整卡片生命周期」；
- N §13 契约测试的第一个跑绿实现。

确定性
------
``sequence`` 与 ``occurred_at`` 由固定规则生成（单调 sequence、以 :data:`EPOCH`
为基准的假时钟），因此同一脚本每次产出的事件**内容**完全一致——契约测试可以
直接断言。

``event_id`` 是**唯一**故意不确定的一项：形态是
``evt-<实例种子>-<runtime>-<seq>``，其中实例种子是 Driver 实例化时生成一次的
uuid4 短串。理由见 N §7.1——``eventId`` 要求**全局**唯一，而它此前完全由
``runtime_id``（``<backend>-runtime-<n>``，进程内计数器）派生：进程一重启计数
就归零，于是同一个领域库里的第二条会话会撞上第一次运行留下的 ``eventId``，
被 Session Host 判为重复投递 → 整轮事件被丢弃 → 收敛成 ``run.failed``。
把进程种子放进 event id 而不是 ``runtime_id``：后者会出现在 API 返回体与
Runtime Lease 里，是给人看的身份，不该变成一串随机码。

覆盖的场景
----------
text streaming、tool start/update/complete、permission 与 question 的请求-响应
闭环、interrupt、run.failed，以及 ``extension.event``（未知原生事件的优雅出口）。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Literal
from uuid import uuid4

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.ids import normalize_backend_id
from app.projects.models import AgentBinding, Project
from drivers.base import (
    AuthState,
    BackendProbeResult,
    CliLaunchSpec,
    CreateSessionOptions,
    DriftReport,
    EngineSettings,
    InteractionNotFoundError,
    InteractionResponse,
    MessageInput,
    ModelCatalog,
    ModelDescriptor,
    NativeHistory,
    NativeHistoryEntry,
    NativeSession,
    ProjectionEntry,
    ProjectionResult,
    RuntimeHandle,
    RuntimeNotFoundError,
    SessionProjection,
    UnsupportedCapabilityError,
    redact_account,
    unknown_auth_state,
)
from drivers.mock.fixtures import (
    AwaitInteractionStep,
    EmitStep,
    HoldStep,
    MockScript,
    full_lifecycle_script,
)
from drivers.mock import projector as mock_projector
from drivers.mock.capabilities import (
    DEFAULT_CAPABILITIES,
    MOCK_BRANCH_NOTE,
    MOCK_USAGE_NOTE,
)
from runtime.capability_matrix import (
    BackendCapabilities,
    evaluate_support,
)
from runtime.event_envelope import (
    AgentEvent,
    AgentEventEnvelope,
    AuthenticationOutcome,
    AuthenticationResolved,
    EventSource,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    PermissionResolved,
    QuestionResolved,
    RunInterrupted,
    make_envelope,
)

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
"""假时钟基准，保证事件时间戳可重现。"""

#: 把契约层的 ``option_id`` 映射到 AD-08 的封闭 outcome 集合。
#: 真实 Driver 在这里映射自己的原生认证结果——公共层永远只看到这四个值。
_DECLINE_OPTION_IDS: frozenset[str] = frozenset({"deny", "decline", "declined", "reject"})


def _authentication_outcome(response: InteractionResponse) -> AuthenticationOutcome:
    if response.cancelled:
        return "cancelled"
    if (response.option_id or "").lower() in _DECLINE_OPTION_IDS:
        return "declined"
    return "authenticated"

DEFAULT_MODELS: tuple[ModelDescriptor, ...] = (
    ModelDescriptor(
        model_id="mock-small",
        display_name="Mock Small",
        provider_id="mock-provider",
        context_window=32_000,
        reasoning_levels=("low", "medium", "high"),
    ),
    ModelDescriptor(
        model_id="mock-large",
        display_name="Mock Large",
        provider_id="mock-provider",
        context_window=200_000,
        reasoning_levels=("low", "medium", "high", "xhigh"),
    ),
)

_SENTINEL = object()


@dataclass
class _RuntimeState:
    handle: RuntimeHandle
    conversation: Conversation
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    sequence: int = 0
    pending: dict[str, "asyncio.Future[InteractionResponse]"] = field(
        default_factory=dict
    )
    task: "asyncio.Task[None] | None" = None
    hold: asyncio.Event = field(default_factory=asyncio.Event)
    current_run_id: str | None = None
    #: 批次十六第 4 件：最近一轮实际按哪个模型发的（会话快照 > Binding 默认）。
    #: Mock 不真的调模型，记下来只为让「快照有没有被用上」可断言。
    last_model_id: str | None = None
    #: 这一轮随消息带来的附件名。Mock 不读内容，只在回复里点名收到了哪些。
    last_attachments: tuple[str, ...] = ()
    closed: bool = False


class MockDriver:
    """脚本驱动的 Driver。

    参数
    ----
    backend_key:
        默认 ``mock``；同一进程可注册多个不同 key 的 MockDriver，用来测
        「多 Backend 数据隔离」。
    script_factory:
        默认剧本工厂。也可以用 :meth:`set_script` 为**某条 Conversation**单独指定
        剧本（契约测试的 ``arrange`` 就是这么做的）。
    capabilities:
        对外声明的能力；契约测试会核对「声明的能力」与「实际行为」一致。
    """

    driver_kind: Literal["mock"] = "mock"

    def __init__(
        self,
        *,
        backend_key: str = "mock",
        script_factory: Callable[[], MockScript] = full_lifecycle_script,
        capabilities: BackendCapabilities | None = None,
        models: tuple[ModelDescriptor, ...] = DEFAULT_MODELS,
        version: str = "0.0.1-mock",
        driver_version: str = "0.1.0",
        installed: bool = True,
        home: "Path | str | None" = None,
    ) -> None:
        self.backend_id = normalize_backend_id(backend_key)
        # 批次二十四：给了 home 才有写面（``<home>/mock-config.json``）。
        # 不给 = 没有可写的目标，物化恒为 dry-run——契约套件因此既能验「有写面」
        # 的分支，也能验「压根没有配置面」的分支（N §13.1 的两条腿）。
        self._home = Path(home) if home is not None else None
        self._backend_key = backend_key
        self._script_factory = script_factory
        self._capabilities = capabilities or DEFAULT_CAPABILITIES
        self._models = models
        self._version = version
        self._driver_version = driver_version
        self._installed = installed

        # N §7.1：eventId 全局唯一。runtime 计数器只在**本进程内**单调，所以
        # event id 还要带一个每个 Driver 实例生成一次的种子，否则「重启后接着用
        # 同一个库」必然撞 id（见模块 docstring「确定性」一节）。
        self._instance_seed = uuid4().hex[:8]

        self._scripts_by_conversation: dict[str, MockScript] = {}
        self._runtimes: dict[str, _RuntimeState] = {}
        self._sessions: dict[str, dict[str, NativeSession]] = {}
        self._session_counter = 0
        self._runtime_counter = 0
        #: 批次四十三（测试面）：Session Host 交给我的 MCP 条目 id，按会话记。
        self.projected_capabilities: dict[str, tuple[str, ...]] = {}
        #: 同上：``start_runtime`` 那一刻拿到的 ``session_options.metadata``。
        #: Mock **不写任何文件**——收到不等于要落盘。
        self.received_session_metadata: dict[str, dict[str, object]] = {}

    # ------------------------------------------------------------------ #
    # 夹具控制（不属于 BackendDriver 契约，只给测试用）
    # ------------------------------------------------------------------ #

    def set_script(self, conversation_id: str, script: MockScript) -> None:
        self._scripts_by_conversation[conversation_id] = script

    def script_for(self, conversation_id: str) -> MockScript:
        return self._scripts_by_conversation.get(conversation_id) or self._script_factory()

    # ------------------------------------------------------------------ #
    # Probe / 能力 / 模型
    # ------------------------------------------------------------------ #

    async def probe(self) -> BackendProbeResult:
        return BackendProbeResult(
            backend_id=self.backend_id,
            driver_kind=self.driver_kind,
            state="ready" if self._installed else "unavailable",
            installed=self._installed,
            version=self._version,
            driver_version=self._driver_version,
            capabilities=self._capabilities,
            probed_at=EPOCH,
        )

    async def get_capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def conversation_controls(self, binding: AgentBinding) -> dict[str, Any]:
        return {"reasoning": bool(self._capabilities.models.reasoning), "approvalModes": []}

    def group_context_isolation(self) -> bool:
        return True

    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog:
        self._assert_binding(binding)
        return ModelCatalog(
            binding_id=binding.id,
            mode=self._capabilities.models.mode,
            models=self._models,
            default_model_id=binding.default_model_id or self._models[0].model_id,
            default_provider_id=binding.default_provider_id,
            supports_reasoning=bool(self._capabilities.models.reasoning),
        )

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings:
        """Mock 引擎没有配置文件 → 空设置（批次十三第 1 件的缺省实现）。

        返回空对象而不是抛异常：`/effective-settings` 的解析链靠「引擎那边没有
        这一项」继续往下走，抛异常会把一条正常的链路变成错误路径。
        """
        self._assert_binding(binding)
        return EngineSettings(
            binding_id=binding.id,
            diagnostics=("Mock Backend 没有引擎侧配置文件，本层无来源",),
        )

    async def read_auth_state(self, binding: AgentBinding) -> AuthState:
        """固定的假登录态（批次十九第 1 件）——**跟着自己的能力声明走**。

        Mock 没有凭据、没有账号，所以「已登录」在这里是一个纯粹的夹具值：前端要
        有一份稳定的三态数据才做得出渲染分支。但预设里 ``auth.state_reporting``
        不是 ``supported`` 的那几档（``lean`` / ``unprobed``）必须返回 ``unknown``
        ——否则 Driver 就在做一件自己声明说做不到的事，而那正是契约测试存在的理由。

        ``account`` 用的是一个**已经脱敏**的字面量（不是真账号），它的作用是让
        「账号永远以脱敏形态上 wire」这条红线在假引擎上也有一条可测的路径。
        """
        self._assert_binding(binding)
        declared = self._capabilities.auth.model
        # 能力表允许「还没声明」，AuthState 不允许（报得出状态就一定知道模型）。
        # 夹具里那一档落到假引擎自己的形态：一把由夹具托管的假凭据。
        model = "managed-credential" if declared == "unknown" else declared
        if not self._capabilities.auth.state_reporting:
            return unknown_auth_state(model, checked_at=EPOCH)
        return AuthState(
            state="signed_in",
            model=model,
            provider="mock",
            account=redact_account("mock-user@example.test"),
            checked_at=EPOCH,
        )

    # ------------------------------------------------------------------ #
    # 能力投射
    # ------------------------------------------------------------------ #

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
        *,
        dry_run: bool = True,
        adopt: bool = False,
    ) -> ProjectionResult:
        """有 ``home`` 就真写 ``<home>/mock-config.json``，没有就只归类。

        「没有 home」不是残废分支，是**一台没有配置面的假引擎**——契约套件的
        写检查在那种夹具上按不支持处理（N §13.1：不支持要说得出口，不是空对象）。
        """
        self._assert_binding(binding)
        if self._home is None:
            applied: list[ProjectionEntry] = []
            unsupported: list[ProjectionEntry] = []
            for entry in effective_capabilities.entries:
                (verdict,) = evaluate_support([entry.capability_type], self._capabilities)
                projection = ProjectionEntry(
                    capability_type=entry.capability_type,
                    capability_id=entry.capability_id,
                    level=verdict.level,
                    target_ref=(
                        f"mock://{binding.id}/{entry.capability_type}/{entry.capability_id}"
                    ),
                    detail=verdict.reason,
                    reason=None if verdict.projectable else "not_mapped",
                )
                (applied if verdict.projectable else unsupported).append(projection)
            return ProjectionResult(
                binding_id=binding.id,
                applied=tuple(applied),
                unsupported=tuple(unsupported),
                warnings=(
                    f"project={project.id} 的这台假引擎没有配置面（未给 home），"
                    "本次只归类、不写任何文件",
                ),
                dry_run=True,
            )
        return mock_projector.project(
            binding_id=binding.id,
            home=self._home,
            effective=effective_capabilities,
            capabilities=self._capabilities,
            project_id=project.id,
            dry_run=dry_run,
            adopt=adopt,
        )

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities | None = None,
    ) -> DriftReport:
        self._assert_binding(binding)
        del project
        if self._home is None or effective_capabilities is None:
            # 没有配置面、或没有期望侧 → 没有可漂移的目标，如实报 in_sync。
            return DriftReport(binding_id=binding.id, checked_at=EPOCH, in_sync=True)
        return mock_projector.inspect(
            binding_id=binding.id,
            home=self._home,
            effective=effective_capabilities,
            capabilities=self._capabilities,
        )

    # ------------------------------------------------------------------ #
    # Native Session
    # ------------------------------------------------------------------ #

    async def list_native_sessions(
        self, binding: AgentBinding
    ) -> tuple[NativeSession, ...]:
        self._assert_binding(binding)
        if not self._capabilities.sessions.list:
            raise UnsupportedCapabilityError("该 Backend 不支持列出原生 Session")
        return tuple(self._sessions.get(binding.id, {}).values())

    async def count_native_sessions(self, binding: AgentBinding) -> int | None:
        """数得出来就给数字，列不出来就是 ``None``（批次十九第 2 件）。

        ``sessions.list`` 声明为 ``none`` 时 :meth:`list_native_sessions` 抛
        ``UnsupportedCapabilityError``——那时「有几条」这个问题本身就不成立，
        返回 ``0`` 会被界面渲染成「0 条会话」，是一句谎话。
        """
        self._assert_binding(binding)
        try:
            return len(await self.list_native_sessions(binding))
        except UnsupportedCapabilityError:
            return None

    async def create_native_session(
        self, binding: AgentBinding, options: CreateSessionOptions
    ) -> NativeSession:
        self._assert_binding(binding)
        if not self._capabilities.sessions.create:
            raise UnsupportedCapabilityError("该 Backend 不支持创建原生 Session")
        self._session_counter += 1
        # v1.0 §8.7：确定性 id，不依赖「最新 Session」猜测。
        native_session_id = f"native-{binding.id}-{self._session_counter}"
        session = NativeSession(
            native_session_id=native_session_id,
            binding_id=binding.id,
            title=options.title or f"mock session {self._session_counter}",
            head_id=native_session_id,
            created_at=EPOCH,
            updated_at=EPOCH,
            message_count=0,
        )
        self._sessions.setdefault(binding.id, {})[native_session_id] = session
        return session

    async def load_native_history(
        self, binding: AgentBinding, native_session_id: str
    ) -> NativeHistory:
        self._assert_binding(binding)
        if not self._capabilities.sessions.history:
            raise UnsupportedCapabilityError("该 Backend 不支持读取原生历史")
        if native_session_id not in self._sessions.get(binding.id, {}):
            raise RuntimeNotFoundError(f"未知的原生 Session：{native_session_id!r}")
        return NativeHistory(
            native_session_id=native_session_id,
            entries=(
                NativeHistoryEntry(
                    entry_id=f"{native_session_id}-1",
                    role="user",
                    kind="message",
                    text="mock 用户消息",
                    occurred_at=EPOCH,
                ),
                NativeHistoryEntry(
                    entry_id=f"{native_session_id}-2",
                    role="assistant",
                    kind="message",
                    text="mock 助手回复",
                    occurred_at=EPOCH + timedelta(seconds=1),
                ),
            ),
            complete=True,
        )

    # ------------------------------------------------------------------ #
    # Card Runtime
    # ------------------------------------------------------------------ #

    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities
    ) -> SessionProjection | None:
        """把有效能力里的 MCP 条目**记下来**（批次四十三的测试面）。

        Mock 不连任何引擎，所以这里不做协议翻译：它只把「Session Host 交给我
        什么」原样存进 :attr:`projected_capabilities`，好让上层的测试断言
        「水龙头真的开了」，而不必去起一个真 agent。
        """
        entries = effective.by_type("mcp")
        self.projected_capabilities[conversation.id] = tuple(
            entry.capability_id for entry in entries
        )
        if not entries:
            return None
        return SessionProjection(
            options=CreateSessionOptions(title=conversation.title),
            summary={"mcpServers": [entry.capability_id for entry in entries]},
        )

    async def start_runtime(
        self,
        conversation: Conversation,
        surface: Literal["card"],
        *,
        session_options: CreateSessionOptions | None = None,
    ) -> RuntimeHandle:
        if surface != "card":
            raise UnsupportedCapabilityError(
                "Driver 只负责 Card Surface；站外 CLI 走 build_external_cli_launch"
            )
        # 批次四十三：记下**这次建 runtime 收到的** metadata，供测试断言。
        # Mock 不写任何文件——它没有可写的目标；MCP 在这里只是被看见，不被使用。
        self.received_session_metadata[conversation.id] = dict(
            (session_options.metadata if session_options else {})
        )
        self._runtime_counter += 1
        handle = RuntimeHandle(
            # runtime_id 带上 backend key：同一进程里两个 MockDriver 的
            # runtime 身份不能重名（event id 的跨进程唯一性另由实例种子保证）。
            runtime_id=f"{self._backend_key}-runtime-{self._runtime_counter}",
            conversation_id=conversation.id,
            binding_id=conversation.agent_binding_id,
            backend_id=self.backend_id,
            native_session_id=conversation.native_session_id,
            started_at=EPOCH,
        )
        self._runtimes[handle.runtime_id] = _RuntimeState(
            handle=handle, conversation=conversation
        )
        return handle

    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None:
        state = self._state(runtime)
        # Mock 不解析用户输入，剧本决定输出；附件只记名字，回复里点名确认收到。
        # 夹具测试会直接传字符串：Mock 的约定是「什么输入都收」，只有 MessageInput 才带附件。
        if isinstance(content, MessageInput):
            state.last_attachments = tuple(item.name or item.ref for item in content.attachments)
        if state.task is not None and not state.task.done():
            raise RuntimeError("上一轮尚未结束；先 interrupt 或等待终态")
        # v1.0 §7.3 的优先级链：Conversation 快照 > Binding 默认。
        state.last_model_id = state.conversation.model_id
        script = self.script_for(state.conversation.id)
        state.task = asyncio.create_task(self._play(state, script))

    def adopt_conversation(
        self, runtime: RuntimeHandle, conversation: Conversation
    ) -> None:
        """Session Host 换了 Conversation（例如改了模型快照）→ 换我们这份。"""
        state = self._runtimes.get(runtime.runtime_id)
        if state is not None and state.conversation.id == conversation.id:
            state.conversation = conversation

    def last_model_id(self, runtime: RuntimeHandle) -> str | None:
        """最近一轮实际用的模型快照（测试与契约用）。"""
        return self._state(runtime).last_model_id

    async def interrupt(self, runtime: RuntimeHandle) -> None:
        state = self._state(runtime)
        if not self._capabilities.card.interrupt:
            raise UnsupportedCapabilityError("该 Backend 不支持中断")
        task = state.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for future in state.pending.values():
            if not future.done():
                future.cancel()
        state.pending.clear()
        await self._emit(
            state,
            RunInterrupted(
                run_id=state.current_run_id or "run-unknown", reason="interrupted by user"
            ),
        )

    async def resolve_interaction(
        self,
        runtime: RuntimeHandle,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None:
        state = self._state(runtime)
        future = state.pending.pop(interaction_id, None)
        if future is None or future.done():
            raise InteractionNotFoundError(f"没有待解决的交互请求：{interaction_id!r}")

        if response.kind == "permission":
            decision = "cancelled" if response.cancelled else (response.option_id or "allow")
            await self._emit(
                state, PermissionResolved(request_id=interaction_id, decision=decision)
            )
        elif response.kind == "question":
            await self._emit(state, QuestionResolved(request_id=interaction_id))
        else:
            # AD-08：认证闭环走公共 union 的 authentication.resolved，
            # 不再借道 extension.event。
            await self._emit(
                state,
                AuthenticationResolved(
                    request_id=interaction_id,
                    outcome=_authentication_outcome(response),
                ),
            )
        future.set_result(response)

    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]:
        state = self._state(runtime)
        while True:
            item = await state.queue.get()
            if item is _SENTINEL:
                return
            yield item

    async def stop_runtime(self, runtime: RuntimeHandle) -> None:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None:
            return
        task = state.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.hold.set()
        state.closed = True
        for future in state.pending.values():
            if not future.done():
                future.cancel()
        state.pending.clear()
        await state.queue.put(_SENTINEL)
        self._runtimes.pop(runtime.runtime_id, None)

    # ------------------------------------------------------------------ #
    # 站外 CLI
    # ------------------------------------------------------------------ #

    async def build_external_cli_launch(
        self, conversation: Conversation
    ) -> CliLaunchSpec:
        if not self._capabilities.external_cli.supported:
            raise UnsupportedCapabilityError("该 Backend 不支持站外 CLI")
        # 假命令刻意是**能真跑**的：Phase 4 的端到端要拿一个假启动器把它跑起来，
        # 看着它退出、看着监视器把会话收回卡片。`sh -c` 后面追加的参数落成 $0/$@，
        # 不影响这段脚本本身。
        command: list[str] = ["sh", "-c", "sleep 1; echo done"]
        resume = False
        if conversation.native_session_id and self._capabilities.external_cli.resume:
            command += ["--resume", conversation.native_session_id]
            resume = True
        if conversation.model_id:
            command += ["--model", conversation.model_id]
        return CliLaunchSpec(
            command=tuple(command),
            cwd=None,
            # v1.0 §16.6：只列变量名，不带值，规格里永不出现 Secret。
            env_passthrough=("PATH", "HOME"),
            title=conversation.title,
            correlation_id=f"launch-{conversation.id}",
            resume=resume,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _assert_binding(self, binding: AgentBinding) -> None:
        """N §13.2：Catalog / Session 归 Binding，不得跨 Backend 串数据。"""
        if binding.backend_id != self.backend_id:
            raise ValueError(
                f"Binding {binding.id!r} 属于 {binding.backend_id!r}，"
                f"不能交给 {self.backend_id!r} 的 Driver"
            )

    def _state(self, runtime: RuntimeHandle) -> _RuntimeState:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None or state.closed:
            raise RuntimeNotFoundError(f"未知或已停止的 runtime：{runtime.runtime_id!r}")
        return state

    async def _emit(
        self,
        state: _RuntimeState,
        event: AgentEvent,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        """发一条事件。

        AD-08：``parent_run_id`` 非空表示这条事件属于一个**子 run**——此时
        ``run_id`` 必须显式给出（子 run 有自己的身份），并且不改写 Driver 记录的
        「当前顶层 run」，否则父 run 的 interrupt 会打错目标。
        """
        sequence = state.sequence
        state.sequence += 1
        if event.type == "run.started" and parent_run_id is None:
            state.current_run_id = event.run_id
        envelope = make_envelope(
            event=event,
            project_id=state.conversation.project_id,
            conversation_id=state.conversation.id,
            agent_binding_id=state.conversation.agent_binding_id,
            backend_id=self.backend_id,
            native_session_id=state.conversation.native_session_id,
            run_id=run_id or state.current_run_id,
            parent_run_id=parent_run_id,
            sequence=sequence,
            occurred_at=EPOCH + timedelta(milliseconds=sequence),
            event_id=f"evt-{self._instance_seed}-{state.handle.runtime_id}-{sequence}",
            source=EventSource(
                driver_kind=self.driver_kind,
                driver_version=self._driver_version,
                backend_version=self._version,
            ),
        )
        await state.queue.put(envelope)

    async def _play(self, state: _RuntimeState, script: MockScript) -> None:
        loop = asyncio.get_running_loop()
        for step in script.steps:
            if isinstance(step, EmitStep):
                await self._emit(
                    state,
                    step.event,
                    run_id=step.run_id,
                    parent_run_id=step.parent_run_id,
                )
                if step.event.type == "run.started" and state.last_attachments:
                    await self._acknowledge_attachments(state, step.run_id)
            elif isinstance(step, AwaitInteractionStep):
                future: asyncio.Future[InteractionResponse] = loop.create_future()
                state.pending[step.request_id] = future
                await future
            elif isinstance(step, HoldStep):
                await state.hold.wait()

    async def _acknowledge_attachments(self, state: _RuntimeState, run_id: str | None) -> None:
        """剧本开场先回一句「收到了哪些附件」，上传链路因此不必接真实引擎也能验。"""
        names = state.last_attachments
        state.last_attachments = ()
        message_id = f"attachments-{uuid4().hex[:8]}"
        text = f"收到 {len(names)} 个附件：{'、'.join(names)}"
        await self._emit(state, MessageStarted(message_id=message_id), run_id=run_id)
        await self._emit(state, MessageDelta(message_id=message_id, text=text), run_id=run_id)
        await self._emit(state, MessageCompleted(message_id=message_id, text=text), run_id=run_id)

    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(self._runtimes)


def replay_envelopes(
    envelopes: tuple[AgentEventEnvelope, ...],
) -> tuple[AgentEventEnvelope, ...]:
    """把一段已捕获的事件流原样重放（断线重连场景的最小夹具）。

    N Phase 3A 验收「断线重连可重放」：reducer 对同一批 Envelope 重放两次的结果
    必须与只处理一次相同（靠 ``eventId`` 去重）。
    """
    return tuple(envelopes)


__all__ = [
    "DEFAULT_CAPABILITIES",
    "DEFAULT_MODELS",
    "EPOCH",
    "MOCK_BRANCH_NOTE",
    "MOCK_USAGE_NOTE",
    "MockDriver",
    "replay_envelopes",
]
