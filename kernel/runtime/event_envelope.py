"""AgentEventEnvelope v1.1 与公共事件 union（N §7）。

职责
----
用 pydantic v2 完整建模 N §7.1 的 Envelope 与 N §7.2 的公共事件分类，作为
Driver Translator 的唯一输出类型、Runtime Reducer 的唯一输入类型。

wire 形态
---------
N §7.1/§7.2 用 TypeScript 写成驼峰，因此这里 Python 侧用蛇形、序列化用驼峰
（``alias_generator=to_camel`` + ``populate_by_name=True``）。
``model_dump(by_alias=True)`` / ``model_dump_json(by_alias=True)`` 产出的
JSON 与 N §7 的接口逐字段一致。

对应规范
--------
- N §7.1 Event Envelope：schemaVersion / eventId / projectId / conversationId /
  agentBindingId / backendId / nativeSessionId? / runId? / nativeEventId? /
  sequence / occurredAt / source{driverKind, driverVersion?, backendVersion?} / event。
- N §7.2 公共事件分类：session/run 生命周期、消息内容、动作与工具、
  human-in-the-loop、usage 与诊断、以及 ``extension.event`` 优雅扩展通道。
- N §7.3 规则 1：每条可增量更新的对象必须有稳定 ID（messageId / callId /
  terminalId / requestId）。
- N §7.3 规则 4：Permission / Question / Authentication 是**交互请求**，
  不得当成普通 Tool Result。
- N §7.3 规则 6：Usage 允许不同 Backend 报告不同精度，公共层不得伪造缺失字段
  → :class:`UsageSnapshot` 全字段可空且无数值默认值。
- N §7.3 规则 7：Native/experimental 信息进入 ``extension.event``，
  不得立即污染公共 union。
- **AD-08（2026-09-02 裁决，冻结前的最后两项补全）**：
  1. 公共事件 union 增加 :class:`AuthenticationResolved`
     （``requestId`` + ``outcome``），让认证请求卡与 Permission / Question 一样
     有明确闭环，而不是靠 ``extension.event`` 兜底；
  2. 信封头增加可选 :attr:`AgentEventEnvelope.parent_run_id`，用来表达
     委派 / 子 run 的归属。因此 ``run.spawned`` **不再**走 ``extension.event``：
     子 run 直接发自己的 ``run.started``，并在信封头写上 ``parentRunId``。
  版本号保持 ``1.1``（这是冻结前的补全，不是破坏性变更），Phase 3A 冻结。
- **AD-27（2026-09-02 裁决，冻结前的最后两处补全）**：
  1. :class:`ToolUpdated` 增加可选 ``cumulative``，与 :class:`TerminalUpdated`
     对称：默认 ``False`` = ``output`` 是**增量**（分片 stdout 追加到已有内容），
     ``True`` = ``output`` 是到目前为止的全量（整体替换）。冻结前不补这一位，
     分片输出的工具卡只能整体替换，中间片段会被后一片抹掉；
  2. 增加 :class:`ReasoningDelta`（``messageId`` + ``text``）：两家参考实现的
     思考过程都是流式的，只有 :class:`ReasoningStatus` 时增量文本无处可放，
     只能整条塞进 ``summary`` 反复覆盖。``reasoning.status`` 语义不变
     （仍然只表达 Backend 明确给出的状态/摘要）。
  ``context.compacted`` / ``input.consumed`` / ``tool_group`` 暂经
  ``extension.event``，列 v1.2 候选。补完即冻结（见
  ``runtime/ENVELOPE_CHANGELOG.md``），版本号仍是 ``1.1``。
- 裁决表 #5：Envelope 已涵盖 R-08（seq / turnId / run.spawned）全部诉求，R-08 作废；
  其中 ``run.spawned`` 的表达方式由 AD-08 改为 ``parentRunId``。
- N §3：本模块不得出现任何具体 Agent 的字段或协议名。
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from runtime.capability_matrix import DriverKind

SCHEMA_VERSION: Literal["1.1"] = "1.1"


class _EventModel(BaseModel):
    """公共事件层基类：驼峰 wire 名、禁止未知字段、不可变。

    ``extra="forbid"`` 是 N §3「公共层不得出现 Agent 私有字段」的机械保障：
    Driver 想塞私有字段只能走 ``extension.event``。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
    )


# --------------------------------------------------------------------------- #
# 事件负载中的结构体
# --------------------------------------------------------------------------- #


class AgentError(_EventModel):
    """结构化错误（N §7.2 ``run.failed``）。"""

    code: str
    message: str
    retriable: bool | None = None
    detail: dict[str, Any] | None = None


class PlanEntry(_EventModel):
    """计划条目（N §7.2 ``plan.updated``）。"""

    entry_id: str | None = None
    content: str
    status: Literal["pending", "in_progress", "completed", "blocked"] | None = None
    priority: str | None = None


class InteractionOption(_EventModel):
    """交互请求的候选项（Permission / Question 共用）。"""

    option_id: str
    label: str
    kind: str | None = None


class PermissionRequest(_EventModel):
    """权限请求（N §7.3 规则 4：不是 Tool Result）。"""

    request_id: str
    title: str
    detail: str | None = None
    #: 该请求指向的工具调用；没有对应工具调用时为 None。
    tool_call_id: str | None = None
    options: tuple[InteractionOption, ...] = ()


class QuestionRequest(_EventModel):
    """向用户提问（N §7.3 规则 4）。"""

    request_id: str
    prompt: str
    options: tuple[InteractionOption, ...] = ()
    allow_free_text: bool = False
    allow_multiple: bool = False


class AuthenticationRequest(_EventModel):
    """认证请求（N §4.5 ACP Authentication / N §8.1 Authentication Card）。"""

    request_id: str
    message: str
    methods: tuple[str, ...] = ()


AuthenticationOutcome = Literal["authenticated", "declined", "cancelled", "failed"]
"""AD-08：``authentication.resolved`` 的闭环结果。

刻意是**封闭集合**而不是自由字符串：认证只有「成功 / 用户拒绝 / 用户取消 /
后端报失败」四种对卡片有意义的收尾，Driver 必须把原生结果映射到其中之一，
原生细节留在 ``extension.event``（N §3：私有语义不得靠自由文本渗进公共层）。
"""


class UsageSnapshot(_EventModel):
    """用量快照。

    N §7.3 规则 6：不同 Backend 精度不同，公共层不得伪造缺失字段——所以全部
    可空且**没有** 0 默认值；``None`` 明确表示「该 Backend 没报」。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    context_window: int | None = None
    context_used: int | None = None


class ArtifactRef(_EventModel):
    """产物引用（N §7.2 ``artifact.created``）。"""

    artifact_id: str
    kind: str
    title: str | None = None
    uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


# --------------------------------------------------------------------------- #
# N §7.2 公共事件 union
# --------------------------------------------------------------------------- #

# --- Session / runtime lifecycle ------------------------------------------- #


class SessionCreated(_EventModel):
    type: Literal["session.created"] = "session.created"
    session_id: str


class SessionResumed(_EventModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str


class SessionState(_EventModel):
    type: Literal["session.state"] = "session.state"
    state: str


class RunStarted(_EventModel):
    type: Literal["run.started"] = "run.started"
    run_id: str


class RunCompleted(_EventModel):
    type: Literal["run.completed"] = "run.completed"
    run_id: str


class RunInterrupted(_EventModel):
    type: Literal["run.interrupted"] = "run.interrupted"
    run_id: str
    reason: str | None = None


class RunFailed(_EventModel):
    type: Literal["run.failed"] = "run.failed"
    run_id: str | None = None
    error: AgentError


# --- User-visible content --------------------------------------------------- #


class MessageStarted(_EventModel):
    type: Literal["message.started"] = "message.started"
    message_id: str
    role: Literal["assistant"] = "assistant"
    phase: Literal["commentary", "final_answer"] | None = None


class MessageDelta(_EventModel):
    type: Literal["message.delta"] = "message.delta"
    message_id: str
    text: str
    phase: Literal["commentary", "final_answer"] | None = None


class MessageCompleted(_EventModel):
    type: Literal["message.completed"] = "message.completed"
    message_id: str
    text: str | None = None
    phase: Literal["commentary", "final_answer"] | None = None


class ReasoningDelta(_EventModel):
    """AD-27：流式思考的增量文本，按 ``messageId`` 归到对应消息的 reasoning 区。

    为什么带 ``messageId`` 而不是 ``runId``：N §7.3 规则 1 要求每个可增量更新的
    对象有稳定 ID，而思考文本是**某一条助手消息**的一部分（一轮里可能有多条
    消息各自带自己的思考），用 run 粒度会把两段思考并成一段。

    与 :class:`ReasoningStatus` 的分工：``reasoning.status`` 是「Backend 明确
    给出的状态/摘要」（N §7.3 规则 5，语义不变），``reasoning.delta`` 是逐片
    追加的原文；两者可以同时存在，互不覆盖。
    """

    type: Literal["reasoning.delta"] = "reasoning.delta"
    message_id: str
    text: str


class ReasoningStatus(_EventModel):
    """N §7.3 规则 5：只展示 Backend 明确提供的状态或摘要。"""

    type: Literal["reasoning.status"] = "reasoning.status"
    status: str
    summary: str | None = None


class PlanUpdated(_EventModel):
    type: Literal["plan.updated"] = "plan.updated"
    plan_id: str | None = None
    entries: tuple[PlanEntry, ...] = ()


# --- Actions, tools and artifacts ------------------------------------------ #


class ToolStarted(_EventModel):
    type: Literal["tool.started"] = "tool.started"
    call_id: str
    name: str
    input: Any = None


class ToolUpdated(_EventModel):
    """N §7.3 规则 2：更新同一张卡，不得每次产生新卡。

    AD-27：``cumulative`` 与 :class:`TerminalUpdated` 对称——
    ``False``（默认）表示 ``output`` 是**增量**，追加到卡片已有输出之后；
    ``True`` 表示 ``output`` 是到目前为止的全量，整体替换。
    追加只对文本有定义；非文本 ``output``（dict / list 等）无论 ``cumulative``
    取值都是整体替换，Backend 想分片就得自己发文本。
    """

    type: Literal["tool.updated"] = "tool.updated"
    call_id: str
    output: Any = None
    progress: Any = None
    #: AD-27：True 表示 ``output`` 是到目前为止的全量输出，而不是增量。
    cumulative: bool = False


class ToolCompleted(_EventModel):
    type: Literal["tool.completed"] = "tool.completed"
    call_id: str
    output: Any = None
    is_error: bool = False


class TerminalStarted(_EventModel):
    type: Literal["terminal.started"] = "terminal.started"
    terminal_id: str
    command: str | None = None


class TerminalUpdated(_EventModel):
    type: Literal["terminal.updated"] = "terminal.updated"
    terminal_id: str
    output: str
    #: True 表示 ``output`` 是到目前为止的全量输出，而不是增量。
    cumulative: bool | None = None


class TerminalCompleted(_EventModel):
    type: Literal["terminal.completed"] = "terminal.completed"
    terminal_id: str
    exit_code: int | None = None


class FileChanged(_EventModel):
    type: Literal["file.changed"] = "file.changed"
    path: str
    diff: str | None = None
    operation: str | None = None


class ArtifactCreated(_EventModel):
    type: Literal["artifact.created"] = "artifact.created"
    artifact: ArtifactRef


# --- Human-in-the-loop ------------------------------------------------------ #


class PermissionRequested(_EventModel):
    type: Literal["permission.requested"] = "permission.requested"
    request: PermissionRequest


class PermissionResolved(_EventModel):
    type: Literal["permission.resolved"] = "permission.resolved"
    request_id: str
    decision: str


class QuestionRequested(_EventModel):
    type: Literal["question.requested"] = "question.requested"
    request: QuestionRequest


class QuestionResolved(_EventModel):
    type: Literal["question.resolved"] = "question.resolved"
    request_id: str


class AuthenticationRequested(_EventModel):
    type: Literal["authentication.requested"] = "authentication.requested"
    request: AuthenticationRequest


class AuthenticationResolved(_EventModel):
    """AD-08：认证请求的闭环，与 ``permission.resolved`` / ``question.resolved`` 并列。

    N §7.3 规则 4 把 Permission / Question / Authentication 并称为「交互请求」，
    但 v1.1 冻结前只有前两者有 ``*.resolved``，认证卡因此永远停在 pending。
    本事件补上这一半。
    """

    type: Literal["authentication.resolved"] = "authentication.resolved"
    request_id: str
    outcome: AuthenticationOutcome


# --- Usage and diagnostics -------------------------------------------------- #


class UsageUpdated(_EventModel):
    type: Literal["usage.updated"] = "usage.updated"
    usage: UsageSnapshot


class DiagnosticNotice(_EventModel):
    type: Literal["diagnostic.notice"] = "diagnostic.notice"
    level: str
    message: str


# --- Graceful extension path ------------------------------------------------ #


class ExtensionEvent(_EventModel):
    """N §7.3 规则 7 与裁决表 #5：原生/实验事件的唯一合法出口。

    公共 union 不为任何单个 Backend 增加成员；``run.spawned`` 等尚未进入
    公共 union 的事件也走这里。
    """

    type: Literal["extension.event"] = "extension.event"
    namespace: str
    name: str
    data: Any = None


AgentEvent = Annotated[
    Union[
        SessionCreated,
        SessionResumed,
        SessionState,
        RunStarted,
        RunCompleted,
        RunInterrupted,
        RunFailed,
        MessageStarted,
        MessageDelta,
        MessageCompleted,
        ReasoningDelta,
        ReasoningStatus,
        PlanUpdated,
        ToolStarted,
        ToolUpdated,
        ToolCompleted,
        TerminalStarted,
        TerminalUpdated,
        TerminalCompleted,
        FileChanged,
        ArtifactCreated,
        PermissionRequested,
        PermissionResolved,
        QuestionRequested,
        QuestionResolved,
        AuthenticationRequested,
        AuthenticationResolved,
        UsageUpdated,
        DiagnosticNotice,
        ExtensionEvent,
    ],
    Field(discriminator="type"),
]
"""N §7.2 的公共事件 union（按 ``type`` 判别）。"""


#: 所有公共事件的 type 字面量，供契约测试与 Card Renderer 映射表使用。
AGENT_EVENT_TYPES: tuple[str, ...] = (
    "session.created",
    "session.resumed",
    "session.state",
    "run.started",
    "run.completed",
    "run.interrupted",
    "run.failed",
    "message.started",
    "message.delta",
    "message.completed",
    # AD-27：流式思考的增量文本（reasoning.status 语义不变）。
    "reasoning.delta",
    "reasoning.status",
    "plan.updated",
    "tool.started",
    "tool.updated",
    "tool.completed",
    "terminal.started",
    "terminal.updated",
    "terminal.completed",
    "file.changed",
    "artifact.created",
    "permission.requested",
    "permission.resolved",
    "question.requested",
    "question.resolved",
    "authentication.requested",
    "authentication.resolved",
    "usage.updated",
    "diagnostic.notice",
    "extension.event",
)

#: N §7.2 中标记为运行终态的事件类型（reducer 的终态收敛依据，N §7.3 规则 9）。
TERMINAL_RUN_EVENT_TYPES: frozenset[str] = frozenset(
    {"run.completed", "run.interrupted", "run.failed"}
)


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


class EventSource(_EventModel):
    """N §7.1 ``source``：调试时可追溯到具体 Driver 与 Backend 版本。"""

    driver_kind: DriverKind
    driver_version: str | None = None
    backend_version: str | None = None


class AgentEventEnvelope(_EventModel):
    """N §7.1 Event Envelope v1.1。

    ``sequence`` 在一条 Conversation 内单调递增，是断线续传（``?after=<seq>``）
    与乱序保护的依据；``event_id`` 全局唯一，是重放去重的依据。

    ``parent_run_id``（AD-08）表达委派：一个 run 派生出子 run 时，子 run 的每条
    事件都在信封头写上父 run 的 id。公共层据此把子 run 的卡片归到发起它的那一轮
    之下，并且**不让子 run 的终态事件把父 run 拉进终态**（见
    :mod:`runtime.event_reducer`）。
    """

    schema_version: Literal["1.1"] = SCHEMA_VERSION
    event_id: str

    project_id: str
    conversation_id: str
    agent_binding_id: str
    backend_id: str

    native_session_id: str | None = None
    run_id: str | None = None
    #: AD-08：委派/子 run 归属。非空表示本条事件属于 ``run_id`` 这个**子 run**，
    #: 它由 ``parent_run_id`` 那一轮派生。
    parent_run_id: str | None = None
    native_event_id: str | None = None

    sequence: int = Field(ge=0)
    occurred_at: datetime

    source: EventSource
    event: AgentEvent

    @model_validator(mode="after")
    def _check_parent_run(self) -> AgentEventEnvelope:
        if self.parent_run_id is None:
            return self
        if self.run_id is None:
            raise ValueError(
                "带 parentRunId 的事件必须自报 runId：子 run 需要有自己的身份（AD-08）"
            )
        if self.parent_run_id == self.run_id:
            raise ValueError("parentRunId 不能等于 runId：run 不能是自己的父 run（AD-08）")
        return self

    @property
    def event_type(self) -> str:
        return self.event.type

    @property
    def is_child_run(self) -> bool:
        """AD-08：本条事件是否属于一个被派生出来的子 run。"""
        return self.parent_run_id is not None


def new_event_id() -> str:
    return str(_uuid.uuid4())


def make_envelope(
    *,
    event: AgentEvent,
    project_id: str,
    conversation_id: str,
    agent_binding_id: str,
    backend_id: str,
    sequence: int,
    source: EventSource,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
    native_session_id: str | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    native_event_id: str | None = None,
) -> AgentEventEnvelope:
    """构造 Envelope 的便捷函数（Driver Translator 的统一出口）。"""
    return AgentEventEnvelope(
        event_id=event_id or new_event_id(),
        project_id=project_id,
        conversation_id=conversation_id,
        agent_binding_id=agent_binding_id,
        backend_id=backend_id,
        native_session_id=native_session_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        native_event_id=native_event_id,
        sequence=sequence,
        occurred_at=occurred_at or datetime.now(tz=timezone.utc),
        source=source,
        event=event,
    )


__all__ = [
    "AGENT_EVENT_TYPES",
    "AgentError",
    "AgentEvent",
    "AgentEventEnvelope",
    "ArtifactCreated",
    "ArtifactRef",
    "AuthenticationOutcome",
    "AuthenticationRequest",
    "AuthenticationRequested",
    "AuthenticationResolved",
    "DiagnosticNotice",
    "EventSource",
    "ExtensionEvent",
    "FileChanged",
    "InteractionOption",
    "MessageCompleted",
    "MessageDelta",
    "MessageStarted",
    "PermissionRequest",
    "PermissionRequested",
    "PermissionResolved",
    "PlanEntry",
    "PlanUpdated",
    "QuestionRequest",
    "QuestionRequested",
    "QuestionResolved",
    "ReasoningDelta",
    "ReasoningStatus",
    "RunCompleted",
    "RunFailed",
    "RunInterrupted",
    "RunStarted",
    "SCHEMA_VERSION",
    "SessionCreated",
    "SessionResumed",
    "SessionState",
    "TERMINAL_RUN_EVENT_TYPES",
    "TerminalCompleted",
    "TerminalStarted",
    "TerminalUpdated",
    "ToolCompleted",
    "ToolStarted",
    "ToolUpdated",
    "UsageSnapshot",
    "UsageUpdated",
    "make_envelope",
    "new_event_id",
]
