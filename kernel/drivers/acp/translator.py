"""ACP 通知 → :class:`~runtime.event_envelope.AgentEventEnvelope`。

本模块是 Driver 的**唯一**翻译面：ACP 的词（``sessionUpdate`` / ``toolCallId`` /
``stopReason``）到这里为止，往上只剩 N §7.2 的公共事件。它不做 IO、不碰
subprocess，因此可以对着录下来的报文逐条单测。

================================================================
ID 合成规则（AD-37 同族；同一次 attach 内稳定）
================================================================
重连后 delta 必须落回同一张卡，所以这些规则是协议的一部分，不是实现细节：

```text
runId     = "<sessionId>:<runToken>:r<N>"  N = 该 Runtime 上第 N 次 prompt（1 起）
messageId = "<runId>:m<K>"         K = 该 run 内第 K 个消息段（1 起）
callId    = ACP 的 toolCallId 原样  （ACP 自带稳定工具调用 ID，无需合成）
requestId = "<runId>:p<M>"         M = 该 run 内第 M 个权限请求（1 起）
eventId   = uuid5(uuid5(ACP_EVENT_ID_NAMESPACE, bindingId),
                  f"{sessionId}:{runToken}:{localSequence}")
```

- **``callId`` 直接用 ACP 的 ``toolCallId``**：AD-37 之所以要合成 callId，是因为
  那条链路的事件流**不带**工具调用 ID；ACP 带，合成反而会把同一次调用拆成两张卡。
- **消息段（K）的开闭规则**（决定「一轮里出现几条消息卡」）：
  1. 收到 ``agent_message_chunk`` 或 ``agent_thought_chunk`` 时，若当前没有打开的
     消息段则**开一段**（K += 1，发 ``message.started``）；
  2. 收到 ``tool_call`` / ``tool_call_update`` / ``plan`` / 权限请求 / run 终态时
     **关闭**当前段（发 ``message.completed``）。
  思考片段与其后的正文属于**同一段**——ACP 的 ``agent_thought_chunk`` 是这条消息
  的思考，AD-27 的 ``reasoning.delta`` 正是按 ``messageId`` 归位的。
- ``localSequence`` 是本翻译器发出的第 n 条事件（0 起），同时也是 Envelope 的
  ``sequence``。同一次 attach 的同一条 ACP 报文序列翻译两次得到完全相同的
  eventId，重放天然幂等（N §7.3 规则 9）。新 attach 使用独立 runToken：
  本地计数重置后也不会与旧运行的 eventId 相撞，导致新回复被当成重放丢弃。

================================================================
明确不翻译的东西（不伪造，N §13.1 / §7.3 规则 6）
================================================================
- **token 用量**：ACP 的事件流里没有 token 字段（实测）。``usage.updated`` 只在
  ``usage_update`` 出现时发，且**只填** ``context_window`` / ``context_used``，
  token 相关字段一律留 ``None``。能力面同步声明 ``card.usage=False``
  （见 :mod:`drivers.acp.capabilities`）。
- **思考状态**：ACP 没有「思考状态/摘要」这种事件，因此本翻译器**从不**产出
  ``reasoning.status``（N §7.3 规则 5：只展示 Backend 明确给出的状态）。
- **子 run / 委派**：ACP 没有对应概念，``parentRunId`` 永远为空。
- **用户消息**：``user_message_chunk`` 是 ACP 把用户输入回显给客户端，公共 union
  的 ``message.*`` 只表达助手消息，因此它走 ``extension.event``。

================================================================
容错（乱序 / 重复 / 越界）
================================================================
- ``session/update`` 的 ``sessionId`` 与本翻译器不符：整条丢弃并计数
  （:attr:`AcpTranslator.ignored_foreign` ），绝不按「当前会话」猜。
- ``tool_call_update`` 先于 ``tool_call`` 到达：**补发** ``tool.started``
  再翻译该更新（否则工具卡永远建不出来）。
- 同一 ``toolCallId`` 第二次 ``tool_call``：丢弃并计数（幂等）。
- 已进入终态的 ``toolCallId`` 再收到更新：丢弃并计数。
- run 已进入终态后再收到 ``stopReason`` / 错误：丢弃（终态只发一次）。

``tool_call_update.content`` 的合并语义
--------------------------------------
ACP 规范说更新里「只需带发生变化的字段」，但**没有**规定 ``content`` 数组是
追加还是替换 `[未验证]`。因此本翻译器一律按**全量快照**处理，即
``tool.updated(cumulative=True)``：把不确定的东西当增量会导致重复文本，当全量
最多是多传一次，代价不对称。AD-38 的「``tool.completed`` 不带 output 时保留已
累积内容」由 Reducer 负责，本翻译器在 ACP 未给出输出时如实传 ``None``。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    ArtifactCreated,
    ArtifactRef,
    DiagnosticNotice,
    EventSource,
    ExtensionEvent,
    InteractionOption,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    PlanEntry,
    PlanUpdated,
    ReasoningDelta,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    SessionCreated,
    SessionResumed,
    ToolCompleted,
    ToolStarted,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
    make_envelope,
)

#: 原生/实验事件的 ``extension.event`` 命名空间。用协议名而不是产品名：
#: 这个 Driver 服务于所有 ACP agent，命名空间必须对它们全体成立。
ACP_EXTENSION_NAMESPACE: str = "acp"

#: eventId 的根命名空间。改它等于把所有历史事件的 ID 换掉，因此**冻结**。
ACP_EVENT_ID_NAMESPACE: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://agentclientprotocol.com/#driver-event-id/v1"
)

#: ACP 的 ``ToolCallStatus`` 中表示「已收尾」的取值。
TERMINAL_TOOL_STATUSES: frozenset[str] = frozenset({"completed", "failed"})

#: ``stopReason`` 中表示「被取消」的取值（规范给的 ``cancelled`` + 常见别名）。
CANCELLED_STOP_REASONS: frozenset[str] = frozenset({"cancelled", "canceled", "cancel"})

#: ``stopReason`` 中表示「正常收尾」的取值。其余取值仍算 run 完成，但会额外发一条
#: ``extension.event`` 把原始 stopReason 带上去（例如 ``max_tokens`` / ``refusal``），
#: 这样 UI 能解释「为什么停了」而公共 union 不必为每种理由加成员。
NORMAL_STOP_REASONS: frozenset[str] = frozenset({"end_turn", "completed", "done"})

#: ACP ``PlanEntry.status`` → 公共 :class:`PlanEntry` 的取值。
_PLAN_STATUS: Mapping[str, str] = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "completed",
    "blocked": "blocked",
}


def make_event_id(
    binding_id: str, session_id: str, local_sequence: int, *, run_token: str
) -> str:
    """Deterministic within an attachment, distinct across session reattachments."""
    binding_namespace = uuid.uuid5(ACP_EVENT_ID_NAMESPACE, binding_id)
    return str(uuid.uuid5(binding_namespace, f"{session_id}:{run_token}:{local_sequence}"))


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class AcpTranslationContext:
    """一条 Runtime 的翻译上下文（信封头需要的全部公共身份）。

    ``run_token``（batch53 / AD-174）是**这一次接上会话**的标记，由 Driver 在
    attach 时现铸，翻译器把它拼进 run id 与 eventId。它必填、没有默认值：翻译器是纯的
    （见 :class:`AcpTranslator` 的类注释），随机与时钟都是 IO，所以唯一性只能
    从外面注入；给个默认值就等于允许有人忘了传，而忘了传的后果是**跨进程撞名**
    —— 真机 MV-01 就是这么来的：后端重启后用同一条 ``native_session_id`` 续上，
    翻译器实例内的轮次计数器却从 0 重新数，于是重启前后两轮都叫 ``…:r1``。
    """

    project_id: str
    conversation_id: str
    agent_binding_id: str
    backend_id: str
    session_id: str
    #: 本次 attach 的唯一标记（Driver 侧现铸；见类 docstring 与 AD-174）。
    run_token: str
    driver_version: str | None = None
    backend_version: str | None = None


@dataclass
class PendingPermission:
    """一次尚未回执的 ``session/request_permission``。

    ``rpc_id`` 是 ACP 那条请求的 JSON-RPC id——回执必须带同一个 id，否则 agent
    会一直阻塞在权限等待上。Driver 从这里取它。
    """

    request_id: str
    rpc_id: Any
    option_ids: tuple[str, ...]
    tool_call_id: str | None = None


@dataclass
class _ToolState:
    call_id: str
    name: str
    terminal: bool = False


class AcpTranslator:
    """把一条 ACP 会话的报文流翻成 Envelope 流。

    有状态（消息段、工具、序号），但**没有 IO**：同一串输入永远产出同一串输出。

    这条纯度是**有代价的**，代价就是 :class:`AcpTranslationContext` 上那个
    ``run_token``（AD-174）：run id 需要跨进程唯一，而唯一性的来源（随机数或
    时钟）都是 IO。所以这里一个 ``uuid4()`` / ``time.time()`` 都不许有——
    唯一性从外面注入，翻译器只负责把它拼进字符串。
    """

    def __init__(
        self,
        context: AcpTranslationContext,
        *,
        clock: Callable[[], datetime] = _utcnow,
        tool_output_cumulative: bool = True,
    ) -> None:
        self.context = context
        self._clock = clock
        #: AD-49 / AD-151：``tool_call_update.content`` 是全量快照还是增量。
        #: 协议没规定，默认按全量（当增量最多闪一下，当全量会重复文本）；
        #: 某个 agent 实测发增量时由**怪癖表**把它翻成 False，公共信封不改。
        self._tool_output_cumulative = tool_output_cumulative
        self._sequence = 0
        self._run_index = 0
        self._run_id: str | None = None
        self._message_index = 0
        self._open_message_id: str | None = None
        self._open_message_phase: str | None = None
        self._artifacts_seen: set[str] = set()
        self._permission_index = 0
        self._tools: dict[str, _ToolState] = {}
        self._run_terminal = True
        self._pending_permissions: dict[str, PendingPermission] = {}
        #: 诊断计数（契约与单测断言用，不进公共事件）。
        self.ignored_foreign = 0
        self.ignored_duplicate_tool = 0
        self.ignored_after_terminal = 0
        self.unknown_updates: list[str] = []

    # ------------------------------------------------------------------ #
    # 只读视图
    # ------------------------------------------------------------------ #

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def sequence(self) -> int:
        """已经发出的事件条数（= 下一条事件的 ``sequence``）。"""
        return self._sequence

    def pending_permission(self, request_id: str) -> PendingPermission | None:
        return self._pending_permissions.get(request_id)

    def pending_permissions(self) -> tuple[PendingPermission, ...]:
        return tuple(self._pending_permissions.values())

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def session_created(self) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(SessionCreated(session_id=self.context.session_id)),)

    def session_resumed(self) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(SessionResumed(session_id=self.context.session_id)),)

    def _run_id_for(self, index: int) -> str:
        """拼一个 run id。唯一性来自 ``run_token``，这里只做字符串拼接（AD-174）。"""
        return f"{self.context.session_id}:{self.context.run_token}:r{index}"

    def begin_run(self) -> tuple[AgentEventEnvelope, ...]:
        """开一轮（对应一次 ``session/prompt``）。

        run id = ``<session_id>:<run_token>:r<N>``（AD-174）。``:rN`` 保留的是
        人读日志时要的那句「这次接上之后的第 N 轮」；跨进程的唯一性由中间那段
        ``run_token`` 负责——它每次 attach 都是新的，所以重启后续接同一条会话也
        不会和重启前那一轮同名。
        """
        self._run_index += 1
        self._run_id = self._run_id_for(self._run_index)
        self._message_index = 0
        self._open_message_id = None
        self._open_message_phase = None
        self._artifacts_seen.clear()
        self._permission_index = 0
        self._tools.clear()
        self._pending_permissions.clear()
        self._run_terminal = False
        return (self._emit(RunStarted(run_id=self._run_id)),)

    def on_stop_reason(
        self, stop_reason: Any, *, interrupted: bool = False
    ) -> tuple[AgentEventEnvelope, ...]:
        """``session/prompt`` 的结果 → run 终态。"""
        if self._run_terminal:
            self.ignored_after_terminal += 1
            return ()
        events: list[AgentEventEnvelope] = list(self._close_message())
        reason = stop_reason if isinstance(stop_reason, str) else None
        run_id = self._run_id or self._run_id_for(0)
        if interrupted or (reason or "").lower() in CANCELLED_STOP_REASONS:
            self._run_terminal = True
            events.append(
                self._emit(RunInterrupted(run_id=run_id, reason=reason or "cancelled"))
            )
            return tuple(events)
        if reason is not None and reason.lower() not in NORMAL_STOP_REASONS:
            # 非常规停止理由（max_tokens / refusal / auth_required …）没有公共
            # 成员可落，走扩展通道；公共 union 不为每种理由加字段（规则 7）。
            events.append(
                self._emit(
                    ExtensionEvent(
                        namespace=ACP_EXTENSION_NAMESPACE,
                        name="stop_reason",
                        data={"stopReason": reason},
                    )
                )
            )
        self._run_terminal = True
        events.append(self._emit(RunCompleted(run_id=run_id)))
        return tuple(events)

    def on_error(
        self, code: Any, message: str, data: Any = None
    ) -> tuple[AgentEventEnvelope, ...]:
        """``session/prompt`` 失败 → ``run.failed``。"""
        if self._run_terminal:
            self.ignored_after_terminal += 1
            return ()
        events: list[AgentEventEnvelope] = list(self._close_message())
        self._run_terminal = True
        events.append(
            self._emit(
                RunFailed(
                    run_id=self._run_id,
                    error=AgentError(
                        code=str(code),
                        message=message,
                        detail={"data": data} if data is not None else None,
                    ),
                )
            )
        )
        return tuple(events)

    # ------------------------------------------------------------------ #
    # session/update
    # ------------------------------------------------------------------ #

    def on_session_update(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        """翻译一条 ``session/update`` 通知。"""
        session_id = params.get("sessionId")
        if session_id is not None and session_id != self.context.session_id:
            # N §6.1/§6.2：会话身份不许猜。别的会话的报文一律丢弃。
            self.ignored_foreign += 1
            return ()
        update = params.get("update")
        if not isinstance(update, Mapping):
            self.unknown_updates.append("<missing update>")
            return ()
        # ACP 用 ``sessionUpdate`` 做判别字段；部分生成的 schema 文档写成 ``type``，
        # 两种都认，避免因为一个字段名把整条流丢掉。
        kind = update.get("sessionUpdate") or update.get("type")
        if not isinstance(kind, str):
            self.unknown_updates.append("<missing sessionUpdate>")
            return ()
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            self.unknown_updates.append(kind)
            return (self._extension(kind, update),)
        return handler(update)

    # --- 文本与思考 ---------------------------------------------------- #

    def _on_agent_message_chunk(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        text = _content_text(update.get("content"))
        events: list[AgentEventEnvelope] = []
        phase = update.get("phase")
        phase = phase if phase in {"commentary", "final_answer"} else None
        if text:
            if phase is not None and self._open_message_id is not None and phase != self._open_message_phase:
                events.extend(self._close_message())
            events.extend(self._open_message(phase=phase))
            events.append(self._emit(MessageDelta(message_id=self._require_message_id(), text=text, phase=phase)))
        events.extend(self._media_events(update.get("content", update.get("rawOutput"))))
        return tuple(events)

    def _on_agent_thought_chunk(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        text = _content_text(update.get("content"))
        if not text:
            return ()
        events = list(self._open_message())
        events.append(
            self._emit(ReasoningDelta(message_id=self._require_message_id(), text=text))
        )
        return tuple(events)

    def _on_user_message_chunk(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        # 公共 union 的 message.* 只表达助手消息；用户输入的回显不是助手消息。
        return (self._extension("user_message_chunk", update),)

    # --- 工具 ---------------------------------------------------------- #

    def _on_tool_call(self, update: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        call_id = _str_or_none(update.get("toolCallId"))
        if call_id is None:
            self.unknown_updates.append("tool_call<no toolCallId>")
            return (self._extension("tool_call", update),)
        if call_id in self._tools:
            self.ignored_duplicate_tool += 1
            return ()
        events = list(self._close_message())
        events.extend(self._start_tool(call_id, update))
        # 首帧的 status 已经由 tool.started 表达了，不再补一条只带 status 的
        # tool.updated（那会让每次工具调用都多出一张空更新）。终态首帧例外：
        # agent 可以一步到位地报一个已经跑完的工具调用。
        events.extend(self._apply_tool_status(call_id, update, status_only=False))
        events.extend(self._media_events(update.get("content", update.get("rawOutput"))))
        return tuple(events)

    def _on_tool_call_update(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        call_id = _str_or_none(update.get("toolCallId"))
        if call_id is None:
            self.unknown_updates.append("tool_call_update<no toolCallId>")
            return (self._extension("tool_call_update", update),)
        state = self._tools.get(call_id)
        if state is not None and state.terminal:
            self.ignored_after_terminal += 1
            return ()
        events = list(self._close_message())
        if state is None:
            # 乱序：更新先于 tool_call 到达。补一条 tool.started，否则这次调用
            # 永远建不出卡（丢一次工具调用比多一张卡严重得多）。
            events.extend(self._start_tool(call_id, update))
        events.extend(self._apply_tool_status(call_id, update))
        events.extend(self._media_events(update.get("content", update.get("rawOutput"))))
        return tuple(events)

    def _start_tool(
        self, call_id: str, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        name = (
            _str_or_none(update.get("title"))
            or _str_or_none(update.get("kind"))
            or call_id
        )
        self._tools[call_id] = _ToolState(call_id=call_id, name=name)
        return (
            self._emit(
                ToolStarted(call_id=call_id, name=name, input=update.get("rawInput"))
            ),
        )

    def _apply_tool_status(
        self, call_id: str, update: Mapping[str, Any], *, status_only: bool = True
    ) -> tuple[AgentEventEnvelope, ...]:
        """把一帧 ``tool_call`` / ``tool_call_update`` 的状态与输出翻成事件。

        ``status_only=False`` 表示「只带 status、不带输出时不发事件」——首帧的
        status 已经由 ``tool.started`` 表达过了。
        """
        state = self._tools[call_id]
        status = _str_or_none(update.get("status"))
        output = _tool_output(update)
        if status is not None and status in TERMINAL_TOOL_STATUSES:
            state.terminal = True
            return (
                self._emit(
                    ToolCompleted(
                        call_id=call_id, output=output, is_error=status == "failed"
                    )
                ),
            )
        if output is None and (status is None or not status_only):
            return ()
        return (
            self._emit(
                ToolUpdated(
                    call_id=call_id,
                    output=output,
                    progress=status,
                    # 见模块 docstring：ACP 没规定 content 是追加还是替换，
                    # 缺省按全量快照处理；发增量的实现由怪癖表登记。
                    cumulative=self._tool_output_cumulative,
                )
            ),
        )

    # --- 计划 / 用量 / 其它 --------------------------------------------- #

    def _on_plan(self, update: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        raw_entries = update.get("entries")
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
            return (self._extension("plan", update),)
        entries: list[PlanEntry] = []
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, Mapping):
                continue
            content = _str_or_none(raw.get("content")) or ""
            if not content:
                continue
            status = _PLAN_STATUS.get(str(raw.get("status")))
            entries.append(
                PlanEntry(
                    entry_id=_str_or_none(raw.get("entryId")) or f"e{index + 1}",
                    content=content,
                    status=status,  # type: ignore[arg-type]
                    priority=_str_or_none(raw.get("priority")),
                )
            )
        events = list(self._close_message())
        events.append(self._emit(PlanUpdated(plan_id=self._run_id, entries=tuple(entries))))
        return tuple(events)

    def _on_usage_update(self, update: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        window = _int_or_none(update.get("size"))
        used = _int_or_none(update.get("used"))
        if window is None and used is None:
            return (self._extension("usage_update", update),)
        # N §7.3 规则 6：没报的字段留 None，绝不用 0 或估算值填。
        return (
            self._emit(
                UsageUpdated(
                    usage=UsageSnapshot(context_window=window, context_used=used)
                )
            ),
        )

    def _on_available_commands_update(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        return (self._extension("available_commands_update", update),)

    def _on_session_info_update(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        return (self._extension("session_info_update", update),)

    def _on_current_mode_update(
        self, update: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        return (self._extension("current_mode_update", update),)

    # ------------------------------------------------------------------ #
    # 权限（ACP 里是一次真正的 RPC 往返）
    # ------------------------------------------------------------------ #

    def on_permission_request(
        self, rpc_id: Any, params: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        """``session/request_permission`` 请求 → ``permission.requested``。

        N §7.3 规则 4：这是交互请求，不是 Tool Result。ACP 给的 ``options``
        **原样**进 payload（``optionId`` / ``name`` / ``kind`` 三段），公共层
        不重排也不改名——选项是 agent 定的，前端只负责展示与回传。
        """
        session_id = params.get("sessionId")
        if session_id is not None and session_id != self.context.session_id:
            self.ignored_foreign += 1
            return ()
        self._permission_index += 1
        run_id = self._run_id or self._run_id_for(0)
        request_id = f"{run_id}:p{self._permission_index}"
        tool_call = params.get("toolCall")
        tool_call_id = None
        title = None
        if isinstance(tool_call, Mapping):
            tool_call_id = _str_or_none(tool_call.get("toolCallId"))
            title = _str_or_none(tool_call.get("title"))
        options: list[InteractionOption] = []
        raw_options = params.get("options")
        if isinstance(raw_options, Sequence) and not isinstance(raw_options, (str, bytes)):
            for raw in raw_options:
                if not isinstance(raw, Mapping):
                    continue
                option_id = _str_or_none(raw.get("optionId"))
                if option_id is None:
                    continue
                options.append(
                    InteractionOption(
                        option_id=option_id,
                        label=_str_or_none(raw.get("name")) or option_id,
                        kind=_str_or_none(raw.get("kind")),
                    )
                )
        self._pending_permissions[request_id] = PendingPermission(
            request_id=request_id,
            rpc_id=rpc_id,
            option_ids=tuple(o.option_id for o in options),
            tool_call_id=tool_call_id,
        )
        events = list(self._close_message())
        events.append(
            self._emit(
                PermissionRequested(
                    request=PermissionRequest(
                        request_id=request_id,
                        title=title or (tool_call_id or "permission required"),
                        detail=None,
                        tool_call_id=tool_call_id,
                        options=tuple(options),
                    )
                )
            )
        )
        return tuple(events)

    def take_permission(self, request_id: str) -> PendingPermission | None:
        """取出并注销一个待回执的权限请求。"""
        return self._pending_permissions.pop(request_id, None)

    def client_event(self, event: AgentEvent) -> tuple[AgentEventEnvelope, ...]:
        """Give client interactions the same sequencing and run identity."""
        return (*self._close_message(), self._emit(event))

    def on_permission_resolved(
        self, request_id: str, decision: str
    ) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(PermissionResolved(request_id=request_id, decision=decision)),)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _media_events(self, content: Any) -> tuple[AgentEventEnvelope, ...]:
        events: list[AgentEventEnvelope] = []
        for block in _media_blocks(content):
            try:
                artifact = _media_artifact(block)
            except ValueError:
                events.append(self._emit(DiagnosticNotice(level="warning", message="引擎返回的媒体内容过大或格式无效，未能预览。")))
                continue
            if artifact is None:
                continue
            digest = hashlib.sha256((artifact.uri or "").encode("utf-8")).hexdigest()[:24]
            if digest in self._artifacts_seen:
                continue
            self._artifacts_seen.add(digest)
            events.append(self._emit(ArtifactCreated(artifact=artifact.model_copy(update={"artifact_id": f"{self._run_id or self.context.session_id}:media:{digest}"}))))
        return tuple(events)

    def _open_message(self, phase: str | None = None) -> tuple[AgentEventEnvelope, ...]:
        if self._open_message_id is not None:
            return ()
        self._message_index += 1
        run_id = self._run_id or self._run_id_for(0)
        self._open_message_id = f"{run_id}:m{self._message_index}"
        self._open_message_phase = phase
        return (self._emit(MessageStarted(message_id=self._open_message_id, phase=phase)),)

    def _require_message_id(self) -> str:
        assert self._open_message_id is not None  # noqa: S101 - _open_message 已保证
        return self._open_message_id

    def _close_message(self) -> tuple[AgentEventEnvelope, ...]:
        message_id = self._open_message_id
        if message_id is None:
            return ()
        self._open_message_id = None
        phase = self._open_message_phase
        self._open_message_phase = None
        return (self._emit(MessageCompleted(message_id=message_id, phase=phase)),)

    def _extension(self, name: str, data: Any) -> AgentEventEnvelope:
        return self._emit(
            ExtensionEvent(
                namespace=ACP_EXTENSION_NAMESPACE,
                name=name,
                data=dict(data) if isinstance(data, Mapping) else data,
            )
        )

    def _emit(self, event: AgentEvent) -> AgentEventEnvelope:
        sequence = self._sequence
        self._sequence += 1
        context = self.context
        return make_envelope(
            event=event,
            project_id=context.project_id,
            conversation_id=context.conversation_id,
            agent_binding_id=context.agent_binding_id,
            backend_id=context.backend_id,
            native_session_id=context.session_id,
            run_id=self._run_id,
            sequence=sequence,
            occurred_at=self._clock(),
            event_id=make_event_id(
                context.agent_binding_id, context.session_id, sequence,
                run_token=context.run_token,
            ),
            source=EventSource(
                driver_kind="acp",
                driver_version=context.driver_version,
                backend_version=context.backend_version,
            ),
        )


# --------------------------------------------------------------------------- #
# ContentBlock 提取
# --------------------------------------------------------------------------- #


def _content_text(content: Any) -> str:
    """从 ACP ContentBlock（或其列表）里抽出文本。

    只认 ``{"type": "text", "text": ...}``；图片 / audio / resource 等非文本块
    没有公共文本归宿，返回空串（调用方会因此不发 delta，而不是把 base64 塞进
    消息正文）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        if content.get("type") == "text":
            return _str_or_none(content.get("text")) or ""
        inner = content.get("content")
        if inner is not None and inner is not content:
            return _content_text(inner)
        return ""
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return "".join(_content_text(item) for item in content)
    return ""


def _tool_output(update: Mapping[str, Any]) -> Any:
    """``tool_call`` / ``tool_call_update`` 的输出载荷。

    优先用 ``content``（全是文本块时拼成字符串，否则原样保留结构，让 Generic
    Tool Card 自己渲染）；没有 ``content`` 时退回 ``rawOutput``。两者都没有就是
    ``None`` —— 这正是 AD-38 说的「不带 output」，Reducer 会保留已累积内容。
    """
    if "content" in update:
        content = update.get("content")
        text = _content_text(content)
        if text and _only_text(content):
            return text
        if content:
            return content
        return None
    if "rawOutput" in update:
        return update.get("rawOutput")
    return None


def _only_text(content: Any) -> bool:
    if isinstance(content, str):
        return True
    if isinstance(content, Mapping):
        if content.get("type") == "text":
            return True
        if content.get("type") == "content":
            return _only_text(content.get("content"))
        return False
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return all(_only_text(item) for item in content)
    return False


def _media_blocks(content: Any):
    if isinstance(content, Mapping):
        if content.get("type") in {"image", "audio", "resource", "resource_link"}:
            yield content
        elif "content" in content:
            yield from _media_blocks(content["content"])
    elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for item in content:
            yield from _media_blocks(item)


def _media_artifact(block: Mapping[str, Any]) -> ArtifactRef | None:
    kind = block.get("type")
    resource = block.get("resource") if kind == "resource" else block
    if not isinstance(resource, Mapping):
        return None
    mime = _str_or_none(resource.get("mimeType")) or "application/octet-stream"
    name = _str_or_none(block.get("name")) or _str_or_none(resource.get("name"))
    uri = _str_or_none(resource.get("uri"))
    data = resource.get("blob") if kind == "resource" else resource.get("data")
    raw: bytes | None = None
    if isinstance(resource.get("text"), str) and kind == "resource":
        raw = resource["text"].encode("utf-8")
        mime = "text/plain"
    elif isinstance(data, str):
        if len(data) > 14 * 1024 * 1024:
            raise ValueError("media too large")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid base64") from None
    size = len(raw) if raw is not None else None
    if size is not None and size > 10 * 1024 * 1024:
        raise ValueError("media too large")
    if raw is not None:
        # Active formats are returned as text, never executable data documents.
        if mime in {"text/html", "image/svg+xml", "application/xhtml+xml"}:
            mime = "text/plain"
        if not re_full_mime(mime):
            mime = "application/octet-stream"
        uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    if uri is None:
        return None
    artifact_kind = "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "file"
    return ArtifactRef(artifact_id="pending", kind=artifact_kind, title=name or ("生成的图片" if artifact_kind == "image" else "返回的文件"), uri=uri, mime_type=mime, size_bytes=size)


def re_full_mime(value: str) -> bool:
    # Avoid importing a rendering/HTTP parser into the pure event translator.
    parts = value.split("/")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$&^_.+-")
    return len(parts) == 2 and all(part and all(char in allowed for char in part) for part in parts)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "ACP_EVENT_ID_NAMESPACE",
    "ACP_EXTENSION_NAMESPACE",
    "AcpTranslationContext",
    "AcpTranslator",
    "CANCELLED_STOP_REASONS",
    "NORMAL_STOP_REASONS",
    "PendingPermission",
    "TERMINAL_TOOL_STATUSES",
    "make_event_id",
]
