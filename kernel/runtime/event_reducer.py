"""Runtime Reducer：把 AgentEventEnvelope 流归并为可渲染的 Timeline 状态。

职责
----
纯函数 reducer：``(TimelineState, AgentEventEnvelope) -> TimelineState``。
不做 IO、不依赖时钟、对同一输入序列永远产出同一结果，因此断线重连时
「重放一遍已存事件」与「首次实时收到」结果完全一致。

落实 N §7.3 的四条硬规则
-----------------------
1. **规则 1（稳定 ID 更新同一卡）**：每个可增量更新的对象用它自己的稳定 ID
   作为 Timeline item 的 key —— ``messageId`` / ``callId`` / ``terminalId`` /
   ``requestId``。同一 ID 的后续事件更新同一个 item，不新增。
2. **规则 2（tool.updated 更新同一卡）**：``tool.started`` 建卡，
   ``tool.updated`` 就地更新 progress/output，``tool.completed`` 收敛为终态。
3. **规则 3（delta 只更新对应 message）**：``message.delta`` 严格按
   ``messageId`` 定位，**绝不**回退到「最后一条助手消息」。未见过的 messageId
   会先隐式建卡（Backend 可能省略 ``message.started``），而不是错误地写进别的消息。
4. **规则 9（断线重连 / 重复事件 / 乱序保护 / 终态收敛）**：
   - 重复：``eventId`` 已见过 → 整条丢弃（幂等）。
   - 乱序：每个 item 记 ``last_sequence``；``sequence`` 不大于它的事件视为迟到，
     不覆盖较新的状态。文本 delta 例外——它按 ``sequence`` 装进桶里再排序拼接，
     所以迟到的 delta 也能落到正确位置。
   - 终态收敛：item 进入终态（message completed / tool completed / terminal
     completed / interaction resolved）后，非终态更新一律忽略；
     run 进入 ``completed | interrupted | failed`` 后不再被更早的事件拉回运行态。

AD-27 的两项补全
----------------
- **``tool.updated`` 的两种合并语义**：``cumulative=False``（默认）时
  ``output`` 是增量，追加到工具卡已有输出之后；``cumulative=True`` 时是全量，
  整体替换。与 ``terminal.updated`` 完全对称。追加只对文本有定义——非文本
  ``output`` 一律整体替换（见 :func:`_merge_tool_output`）。
  批次十六第 3 件补一条：全量替换**允许落在已终态的工具卡上**（迟到的补账，
  例如回合结束后才从原生历史读到的工具输出与完整入参）；增量不允许。
- **``reasoning.delta``**：按 ``messageId`` 累积到**对应消息**的 reasoning 区
  （:attr:`MessageItem.reasoning_chunks` / :attr:`MessageItem.reasoning_text`），
  和 ``message.delta`` 用同一套「按 sequence 装桶再排序拼接」的规则，因此迟到的
  思考片段也能归位。``reasoning.status`` 语义不变，仍然是 run 粒度的
  :class:`ReasoningItem`，两者互不覆盖。

用户消息进时间线（批次八第 6 件）
--------------------------------
卡片时间线此前只有 Backend 吐出来的东西，用户自己发的那句话不在里面——草稿页
打的第一句话，跳转到会话页之后就消失了。修法**不动冻结的事件清单**
（AgentEventEnvelope v1.1 的 30 条 union 已于 2026-09-02 冻结）：

- 不新增事件类型：那是 v1.2 的事（`runtime/ENVELOPE_CHANGELOG.md` 变更规则 1）；
- 也不复用 ``message.started`` / ``message.delta``：``MessageStarted.role`` 的
  类型是 ``Literal["assistant"]``，把它放宽成 ``assistant | user`` 是**改一个
  已发布字段的类型**，不是「加一个可选字段」（规则 2 允许的只有后者）；而且
  ``messageId`` 属于 Backend 的命名空间，Session Host 自己造一个进去，早晚和
  真 Driver 的 id 撞上；
- 走 ``extension.event``（namespace ``kaus``、name ``user.message``）——N §7.3
  规则 7 与裁决表 #5 说得很清楚，那是原生/实验事件的**唯一合法出口**，
  ``run.spawned`` 进 union 之前走的也是这条路。

Reducer 认得这一对 namespace/name，把它归成一条 ``role="user"`` 的
:class:`MessageItem`（而不是通用事件卡）。事件本身走 Session Host 与 Driver
事件**完全相同**的入库路径，所以 ``?after=`` 重放同样能拿到它。

注意 :attr:`MessageItem.role` 因此有了 ``user`` 这个取值——那是**渲染状态**的
模型，不是信封；冻结管的是 union，不管 Reducer 的输出形状。

AD-08 的两项补全
----------------
- **认证闭环**：``authentication.resolved`` 与 ``permission.resolved`` /
  ``question.resolved`` 走同一条收敛路径，认证卡不再永远停在 pending；
  结果写进 :attr:`InteractionItem.outcome`（认证是 ``outcome``，
  权限是 ``decision``——两者语义不同，不合并成一个字段）。
- **子 run 归属**：信封头带 ``parentRunId`` 的事件属于一个被派生出来的子 run。
  它们照常建卡（委派出去的活儿也要看得见），但卡片记下自己属于哪个 run；
  子 run 的 ``run.*`` 生命周期事件只更新 :attr:`TimelineState.child_runs`，
  **不**改变顶层 ``run_state`` / ``active_run_id`` / ``error``——
  否则一个子 run 失败会把整轮对话显示成失败。
  ``usage.updated`` 仍按 Conversation 级累计（事件本身不带 run 维度），
  「按 run 拆分用量」留给后续版本，见收尾报告未决问题。

对应规范
--------
- N §7.3 事件设计规则 1、2、3、9（本任务明确要求的四条）。
- AD-08：``authentication.resolved`` 进公共 union；信封头 ``parentRunId``
  取代「``run.spawned`` 走 ``extension.event``」的旧表达。
- N §6：``AgentEventEnvelope → Runtime Reducer / Event Store → Card Renderer``。
- N §8.1：Timeline item 的种类与 Card 的映射（message / reasoning / plan / tool /
  terminal / file / artifact / permission / question / authentication / usage /
  lifecycle / extension）。
- N §8.2：未知但可显示的事件 → Generic Event Card（这里是 ``extension`` item），
  不得让页面崩溃。
- 裁决表 #2：Event Store 是「短期重放缓冲 + 渲染缓存」，原生 Session 历史仍是
  唯一权威账本；因此本 reducer 的输出定位为**渲染状态**，不是持久账。
"""

from __future__ import annotations

from typing import Annotated, Any, Iterable, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field

from runtime.event_envelope import (
    AgentError,
    AgentEventEnvelope,
    ArtifactRef,
    AuthenticationOutcome,
    AuthenticationRequest,
    PermissionRequest,
    PlanEntry,
    QuestionRequest,
    UsageSnapshot,
)

TimelineItemKind = Literal[
    "message",
    "reasoning",
    "plan",
    "tool",
    "terminal",
    "file",
    "artifact",
    "interaction",
    "diagnostic",
    "lifecycle",
    "extension",
]
"""N §8.1 通用卡片映射对应的 item 种类。"""

RunState = Literal["idle", "running", "completed", "interrupted", "failed"]


class _StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


# --------------------------------------------------------------------------- #
# Timeline items
# --------------------------------------------------------------------------- #


class TimelineItem(_StateModel):
    """所有 Timeline item 的公共部分。

    ``item_id`` 就是 N §7.3 规则 1 说的稳定 ID；``order`` 是首次出现时的
    sequence，用于渲染排序（后续更新不改变卡片位置）。

    ``run_id`` / ``parent_run_id``（AD-08）记录这张卡属于哪一轮：
    ``parent_run_id`` 非空表示它来自一个被派生出来的子 run，UI 可以据此把
    委派出去的工作折叠在发起它的那一轮下面。两者都由 :func:`_append` 从信封头
    统一填入，各 handler 不必关心。
    """

    kind: TimelineItemKind
    item_id: str
    order: int
    last_sequence: int
    terminal: bool = False
    run_id: str | None = None
    parent_run_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.terminal

    @property
    def is_from_child_run(self) -> bool:
        """AD-08：这张卡是否来自被派生出来的子 run。"""
        return self.parent_run_id is not None


class MessageItem(TimelineItem):
    kind: Literal["message"] = "message"
    #: ``user`` 是批次八第 6 件加的：用户自己发的那句话也是时间线上的一条消息。
    #: 它由 ``extension.event``（``kaus`` / ``user.message``）产生，见模块 docstring。
    role: Literal["assistant", "user"] = "assistant"
    phase: Literal["commentary", "final_answer"] | None = None
    #: sequence -> delta 文本。按 key 排序拼接，因此迟到的 delta 也能归位。
    delta_chunks: dict[int, str] = Field(default_factory=dict)
    final_text: str | None = None
    #: AD-27：sequence -> reasoning.delta 文本，即这条消息的**思考区**。
    #: 与正文分开存：思考不是消息正文，UI 折叠它、复制正文时不该带上。
    reasoning_chunks: dict[int, str] = Field(default_factory=dict)
    #: R6（批次三十七）：这条**用户**消息有没有被引擎接下。``failed`` = 它已经
    #: 落在时间线上，但引擎在接受之前就拒了这一句（上一轮还在跑、未登录、
    #: 网关拒绝……）。AD-105 的口径不变——**不回滚**已经落库的那条正文；补的是
    #: 一个可持久化的状态，让刷新之后它仍然是「这句话没被处理」而不是普通历史。
    #: 字段名不叫 ``status``：那个名字在本类上已经被「流式到哪了」占着
    #: （``streaming`` / ``completed``），两个 status 挤在一张卡上，读的人只能靠猜。
    delivery_status: Literal["ok", "failed"] = "ok"
    #: 失败时引擎/接入层给的稳定 code 与人话（``delivery_status == "ok"`` 时为 ``None``）。
    failure_code: str | None = None
    failure_message: str | None = None
    #: AD-94 的对账编号（用户消息才有）。R6 用它把失败事件配回原来那条消息，
    #: 而不是靠「最后一条」这种会在并发下认错人的猜法。
    client_ref: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()

    @property
    def text(self) -> str:
        if self.final_text is not None:
            return self.final_text
        return "".join(self.delta_chunks[key] for key in sorted(self.delta_chunks))

    @property
    def reasoning_text(self) -> str:
        """AD-27：这条消息累积到的思考文本（没有则空串）。"""
        return "".join(
            self.reasoning_chunks[key] for key in sorted(self.reasoning_chunks)
        )

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning_chunks)

    @property
    def status(self) -> Literal["streaming", "completed"]:
        return "completed" if self.terminal else "streaming"


class ReasoningItem(TimelineItem):
    kind: Literal["reasoning"] = "reasoning"
    status: str
    summary: str | None = None


class PlanItem(TimelineItem):
    kind: Literal["plan"] = "plan"
    entries: tuple[PlanEntry, ...] = ()


class ToolItem(TimelineItem):
    kind: Literal["tool"] = "tool"
    name: str
    input: Any = None
    output: Any = None
    progress: Any = None
    status: Literal["running", "completed", "failed"] = "running"


class TerminalItem(TimelineItem):
    kind: Literal["terminal"] = "terminal"
    command: str | None = None
    output: str = ""
    exit_code: int | None = None
    status: Literal["running", "completed"] = "running"


class FileChangeItem(TimelineItem):
    kind: Literal["file"] = "file"
    path: str
    diff: str | None = None
    operation: str | None = None


class ArtifactItem(TimelineItem):
    kind: Literal["artifact"] = "artifact"
    artifact: ArtifactRef


class InteractionItem(TimelineItem):
    """N §7.3 规则 4：Permission / Question / Authentication 是交互请求，
    与 Tool Result 分开建卡。

    三种请求各有自己的闭环字段：权限是 ``decision``（用户选了哪个选项），
    认证是 ``outcome``（AD-08 的封闭结果集），提问只需要 ``status``。
    """

    kind: Literal["interaction"] = "interaction"
    interaction_kind: Literal["permission", "question", "authentication"]
    permission: PermissionRequest | None = None
    question: QuestionRequest | None = None
    authentication: AuthenticationRequest | None = None
    status: Literal["pending", "resolved"] = "pending"
    decision: str | None = None
    #: AD-08：``authentication.resolved`` 的结果；其它两种交互恒为 None。
    outcome: AuthenticationOutcome | None = None


class DiagnosticItem(TimelineItem):
    kind: Literal["diagnostic"] = "diagnostic"
    level: str
    message: str


class LifecycleItem(TimelineItem):
    kind: Literal["lifecycle"] = "lifecycle"
    event_type: str
    detail: str | None = None


class ExtensionItem(TimelineItem):
    """N §8.2：未注册的扩展事件 → Generic Event Card。"""

    kind: Literal["extension"] = "extension"
    namespace: str
    name: str
    data: Any = None


AnyTimelineItem = Annotated[
    Union[
        MessageItem,
        ReasoningItem,
        PlanItem,
        ToolItem,
        TerminalItem,
        FileChangeItem,
        ArtifactItem,
        InteractionItem,
        DiagnosticItem,
        LifecycleItem,
        ExtensionItem,
    ],
    Field(discriminator="kind"),
]
"""按 ``kind`` 判别的 Timeline item union（N §8.1 的卡片种类）。"""


# --------------------------------------------------------------------------- #
# Timeline state
# --------------------------------------------------------------------------- #

_RUN_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "interrupted", "failed"})


class ChildRun(_StateModel):
    """AD-08：一个被派生出来的子 run 的归属与状态。

    它**不是**顶层 run：父 run 的 ``run_state`` 不受子 run 影响，反之亦然。
    """

    run_id: str
    parent_run_id: str
    state: RunState = "running"
    #: 首次出现的 sequence，用于渲染排序。
    order: int
    error: AgentError | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _RUN_TERMINAL_STATES


class TimelineState(_StateModel):
    """一条 Conversation 的可渲染时间线状态。"""

    conversation_id: str
    items: tuple[AnyTimelineItem, ...] = ()
    run_state: RunState = "idle"
    active_run_id: str | None = None
    #: AD-08：由当前对话派生出来的子 run，按首次出现顺序。
    child_runs: tuple[ChildRun, ...] = ()
    last_sequence: int = -1
    seen_event_ids: frozenset[str] = frozenset()
    error: AgentError | None = None
    usage: UsageSnapshot | None = None
    #: 被丢弃的事件数（重复 / 迟到）。契约测试与调试用。
    dropped_duplicates: int = 0
    dropped_stale: int = 0

    @classmethod
    def initial(cls, conversation_id: str) -> TimelineState:
        return cls(conversation_id=conversation_id)

    def item(self, item_id: str) -> AnyTimelineItem | None:
        for existing in self.items:
            if existing.item_id == item_id:
                return existing
        return None

    def items_of(self, kind: TimelineItemKind) -> tuple[AnyTimelineItem, ...]:
        return tuple(existing for existing in self.items if existing.kind == kind)

    def message_text(self, message_id: str) -> str | None:
        found = self.item(_message_key(message_id))
        return found.text if isinstance(found, MessageItem) else None

    def child_run(self, run_id: str) -> ChildRun | None:
        for child in self.child_runs:
            if child.run_id == run_id:
                return child
        return None

    def child_runs_of(self, parent_run_id: str) -> tuple[ChildRun, ...]:
        """AD-08：某一轮派生出来的全部子 run。"""
        return tuple(c for c in self.child_runs if c.parent_run_id == parent_run_id)

    def items_of_run(self, run_id: str) -> tuple[AnyTimelineItem, ...]:
        """AD-08：归属于某个 run（含子 run）的卡片。"""
        return tuple(existing for existing in self.items if existing.run_id == run_id)

    @property
    def is_run_terminal(self) -> bool:
        return self.run_state in _RUN_TERMINAL_STATES


# --------------------------------------------------------------------------- #
# item key：不同种类各自命名空间，避免 messageId 与 callId 撞车
# --------------------------------------------------------------------------- #


#: 用户消息借用的扩展事件坐标（批次八第 6 件；见模块 docstring 的理由）。
#: ``namespace`` 是产品自己的命名空间，不是任何一家 Backend 的。
USER_MESSAGE_NAMESPACE = "kaus"
USER_MESSAGE_NAME = "user.message"

#: R6（批次三十七）：引擎在接受之前就拒了这一句时补的那条状态事件。
#: 同一个 namespace、同一个出口（``extension.event``），信封 v1.1 一个字没动。
USER_MESSAGE_FAILED_NAME = "user.message.failed"

#: AD-155：模型快照过期时「采纳引擎当前模型」的那条通知（批次三十一）。
#: 同一个产品命名空间，同样借道 ``extension.event``（信封 v1.1 仍冻结）。
MODEL_ADOPTED_NAMESPACE = "kaus"
MODEL_ADOPTED_NAME = "model.adopted"
#: 采纳原因：这台引擎的回合接口不接受按回合指定模型（规格 §2.5）。
MODEL_ADOPTED_REASON_NOT_SCOPED = "engine_not_conversation_scoped"


def _message_key(message_id: str) -> str:
    return f"message:{message_id}"


def _tool_key(call_id: str) -> str:
    return f"tool:{call_id}"


def _terminal_key(terminal_id: str) -> str:
    return f"terminal:{terminal_id}"


def _interaction_key(request_id: str) -> str:
    return f"interaction:{request_id}"


def _plan_key(plan_id: str | None) -> str:
    return f"plan:{plan_id or 'default'}"


def _reasoning_key(run_id: str | None) -> str:
    return f"reasoning:{run_id or 'default'}"


def _event_key(prefix: str, envelope: AgentEventEnvelope) -> str:
    return f"{prefix}:{envelope.event_id}"


# --------------------------------------------------------------------------- #
# reducer
# --------------------------------------------------------------------------- #


def reduce_event(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
    """把一条 Envelope 归并进状态。纯函数：不修改入参。"""
    # --- N §7.3 规则 9：重复事件（断线重放）幂等丢弃 ---------------------- #
    if envelope.event_id in state.seen_event_ids:
        return state.model_copy(
            update={"dropped_duplicates": state.dropped_duplicates + 1}
        )

    event = envelope.event
    handler = _HANDLERS.get(event.type)
    if handler is None:  # pragma: no cover - union 已穷尽，属防御分支
        updated = _append(
            state,
            ExtensionItem(
                item_id=_event_key("unknown", envelope),
                order=envelope.sequence,
                last_sequence=envelope.sequence,
                terminal=True,
                namespace="unknown",
                name=event.type,
                data=None,
            ),
            envelope,
        )
        return _finalize(updated, envelope)

    updated = handler(state, envelope)
    return _finalize(updated, envelope)


def reduce_events(
    state: TimelineState, envelopes: Iterable[AgentEventEnvelope]
) -> TimelineState:
    """顺序归并一批 Envelope。"""
    current = state
    for envelope in envelopes:
        current = reduce_event(current, envelope)
    return current


def _finalize(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
    return state.model_copy(
        update={
            "seen_event_ids": state.seen_event_ids | {envelope.event_id},
            "last_sequence": max(state.last_sequence, envelope.sequence),
        }
    )


# --- item 增改工具 ---------------------------------------------------------- #


def _append(
    state: TimelineState, item: AnyTimelineItem, envelope: AgentEventEnvelope
) -> TimelineState:
    """建卡。AD-08：run 归属在这里统一从信封头盖上去，各 handler 不必重复。"""
    stamped = item.model_copy(
        update={"run_id": envelope.run_id, "parent_run_id": envelope.parent_run_id}
    )
    return state.model_copy(update={"items": (*state.items, stamped)})


def _replace(state: TimelineState, item: AnyTimelineItem) -> TimelineState:
    items = tuple(item if e.item_id == item.item_id else e for e in state.items)
    return state.model_copy(update={"items": items})


def _is_stale(existing: AnyTimelineItem, envelope: AgentEventEnvelope) -> bool:
    """N §7.3 规则 9：迟到事件不得覆盖更新的 item 状态。"""
    return envelope.sequence <= existing.last_sequence


def _drop_stale(state: TimelineState) -> TimelineState:
    return state.model_copy(update={"dropped_stale": state.dropped_stale + 1})


def _update_item(
    state: TimelineState,
    item_id: str,
    envelope: AgentEventEnvelope,
    changes: Mapping[str, Any],
    *,
    terminal: bool = False,
    allow_after_terminal: bool = False,
) -> TimelineState | None:
    """就地更新已存在的 item；返回 ``None`` 表示 item 不存在。

    终态收敛：item 已 terminal 且本次不是 terminal 更新时直接忽略。
    """
    existing = state.item(item_id)
    if existing is None:
        return None
    if _is_stale(existing, envelope):
        return _drop_stale(state)
    if existing.terminal and not (terminal or allow_after_terminal):
        return _drop_stale(state)
    payload = dict(changes)
    payload["last_sequence"] = envelope.sequence
    if terminal:
        payload["terminal"] = True
    return _replace(state, existing.model_copy(update=payload))


# --- 各事件的 handler -------------------------------------------------------- #


def _handle_session_lifecycle(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    detail = getattr(event, "session_id", None) or getattr(event, "state", None)
    return _append(
        state,
        LifecycleItem(
            item_id=_event_key("lifecycle", envelope),
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            event_type=event.type,
            detail=detail,
        ),
        envelope,
    )


def _upsert_child_run(
    state: TimelineState,
    envelope: AgentEventEnvelope,
    *,
    run_state: RunState,
    error: AgentError | None = None,
) -> TimelineState:
    """AD-08：更新子 run 的状态。父 run 的 ``run_state`` 一律不受影响。"""
    assert envelope.parent_run_id is not None  # noqa: S101 - 调用点已判定
    run_id = envelope.run_id or ""
    existing = state.child_run(run_id)
    if existing is None:
        child = ChildRun(
            run_id=run_id,
            parent_run_id=envelope.parent_run_id,
            state=run_state,
            order=envelope.sequence,
            error=error,
        )
        return state.model_copy(update={"child_runs": (*state.child_runs, child)})
    if existing.is_terminal:
        # 终态收敛同样适用于子 run。
        return _drop_stale(state)
    updated = existing.model_copy(
        update={"state": run_state, "error": error if error is not None else existing.error}
    )
    return state.model_copy(
        update={
            "child_runs": tuple(
                updated if c.run_id == run_id else c for c in state.child_runs
            )
        }
    )


def _handle_run_started(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    appended = _handle_session_lifecycle(state, envelope)
    if envelope.is_child_run:
        # AD-08：子 run 开始 ≠ 本轮重新开始；只登记归属。
        return _upsert_child_run(appended, envelope, run_state="running")
    # 终态收敛：run 已结束后，迟到的 run.started 不得把状态拉回 running。
    if state.is_run_terminal and state.active_run_id == event.run_id:
        return _drop_stale(appended)
    return appended.model_copy(
        update={"run_state": "running", "active_run_id": event.run_id, "error": None}
    )


def _run_terminal_handler(target_state: RunState):
    def handler(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
        event = envelope.event
        appended = _handle_session_lifecycle(state, envelope)
        error = getattr(event, "error", None)
        if envelope.is_child_run:
            # AD-08：子 run 结束（哪怕是失败）不得把父 run 拖进终态。
            return _upsert_child_run(
                appended, envelope, run_state=target_state, error=error
            )
        if state.is_run_terminal:
            # 已经是终态：只记录 lifecycle marker，不再改变 run_state（收敛）。
            return _drop_stale(appended)
        update: dict[str, Any] = {"run_state": target_state}
        if error is not None:
            update["error"] = error
        return appended.model_copy(update=update)

    return handler


def _handle_message_started(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _message_key(event.message_id)
    if state.item(key) is not None:
        updated = _update_item(state, key, envelope, {})
        return updated if updated is not None else state
    return _append(
        state,
        MessageItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            role=event.role,
            phase=event.phase,
        ),
        envelope,
    )


def _ensure_message(
    state: TimelineState, message_id: str, envelope: AgentEventEnvelope
) -> TimelineState:
    """N §7.3 规则 3：delta 只认自己的 messageId；缺 started 就隐式建卡，
    绝不写进「最后一条助手消息」。"""
    key = _message_key(message_id)
    if state.item(key) is not None:
        return state
    return _append(
        state,
        MessageItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
        ),
        envelope,
    )


def _append_text_chunk(
    state: TimelineState, envelope: AgentEventEnvelope, *, field: str
) -> TimelineState:
    """把一片文本装进某条消息的桶里（正文 ``delta_chunks`` 或思考 ``reasoning_chunks``）。

    两个桶共用同一套规则（N §7.3 规则 3 + 规则 9）：按 ``messageId`` 定位、
    缺 ``message.started`` 就隐式建卡、按 ``sequence`` 装桶排序拼接（迟到片段
    也能归位）、同 sequence 视为重复投递、消息终态后丢弃。
    """
    event = envelope.event
    prepared = _ensure_message(state, event.message_id, envelope)
    key = _message_key(event.message_id)
    existing = prepared.item(key)
    assert isinstance(existing, MessageItem)  # noqa: S101 - 内部不变量
    if existing.terminal:
        # 终态收敛：message.completed 之后的 delta 丢弃。
        return _drop_stale(prepared)
    chunks = dict(getattr(existing, field))
    if envelope.sequence in chunks:
        # 同 sequence 不同 eventId：视为重复投递，保持幂等。
        return _drop_stale(prepared)
    chunks[envelope.sequence] = event.text
    return _replace(
        prepared,
        existing.model_copy(
            update={
                field: chunks,
                "phase": getattr(event, "phase", None) or existing.phase,
                # delta 按 sequence 排序拼接，因此迟到 delta 也能归位；
                # last_sequence 只向前推进，用于其它更新的乱序判定。
                "last_sequence": max(existing.last_sequence, envelope.sequence),
            }
        ),
    )


def _handle_message_delta(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    return _append_text_chunk(state, envelope, field="delta_chunks")


def _handle_reasoning_delta(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    """AD-27：思考增量累积到**对应消息**的 reasoning 区，不碰消息正文。"""
    return _append_text_chunk(state, envelope, field="reasoning_chunks")


def _handle_message_completed(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    prepared = _ensure_message(state, event.message_id, envelope)
    key = _message_key(event.message_id)
    existing = prepared.item(key)
    assert isinstance(existing, MessageItem)  # noqa: S101
    if existing.terminal:
        return _drop_stale(prepared)
    final_text = event.text if event.text is not None else existing.text
    return _replace(
        prepared,
        existing.model_copy(
            update={
                "final_text": final_text,
                "phase": event.phase or existing.phase,
                "terminal": True,
                "last_sequence": max(existing.last_sequence, envelope.sequence),
            }
        ),
    )


def _handle_reasoning(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _reasoning_key(envelope.run_id)
    updated = _update_item(
        state, key, envelope, {"status": event.status, "summary": event.summary}
    )
    if updated is not None:
        return updated
    return _append(
        state,
        ReasoningItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            status=event.status,
            summary=event.summary,
        ),
        envelope,
    )


def _handle_plan(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
    event = envelope.event
    key = _plan_key(event.plan_id)
    updated = _update_item(state, key, envelope, {"entries": tuple(event.entries)})
    if updated is not None:
        return updated
    return _append(
        state,
        PlanItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            entries=tuple(event.entries),
        ),
        envelope,
    )


def _handle_tool_started(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _tool_key(event.call_id)
    if state.item(key) is not None:
        updated = _update_item(state, key, envelope, {"name": event.name, "input": event.input})
        return updated if updated is not None else state
    return _append(
        state,
        ToolItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            name=event.name,
            input=event.input,
        ),
        envelope,
    )


def _ensure_tool(
    state: TimelineState, call_id: str, envelope: AgentEventEnvelope
) -> TimelineState:
    key = _tool_key(call_id)
    if state.item(key) is not None:
        return state
    return _append(
        state,
        ToolItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            name="unknown",
        ),
        envelope,
    )


def _merge_tool_output(existing: Any, incoming: Any, *, cumulative: bool) -> Any:
    """AD-27：``tool.updated`` 的两种合并语义（与 ``terminal.updated`` 对称）。

    - ``cumulative=True``：``incoming`` 是到目前为止的全量 → 整体替换；
    - ``cumulative=False``（默认）：``incoming`` 是增量 → 追加。

    追加只对**文本**有定义。已有输出为空时直接取 ``incoming``；两侧都是 ``str``
    时拼接；其余情况（dict / list 等结构化输出）没有"追加"这个概念，整体替换，
    想分片的 Backend 必须发文本。
    """
    if cumulative:
        return incoming
    if existing is None:
        return incoming
    if isinstance(existing, str) and isinstance(incoming, str):
        return existing + incoming
    return incoming


def _handle_tool_updated(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    """N §7.3 规则 2：更新同一张卡，不产生新卡。

    AD-27：``output`` 的合并按 ``cumulative`` 分两种语义，见 :func:`_merge_tool_output`。
    """
    event = envelope.event
    prepared = _ensure_tool(state, event.call_id, envelope)
    changes: dict[str, Any] = {}
    if event.output is not None:
        current = prepared.item(_tool_key(event.call_id))
        changes["output"] = _merge_tool_output(
            getattr(current, "output", None),
            event.output,
            cumulative=event.cumulative,
        )
    if event.progress is not None:
        changes["progress"] = event.progress
    updated = _update_item(
        prepared,
        _tool_key(event.call_id),
        envelope,
        changes,
        # 批次十六第 3 件：``cumulative=True`` 是**全量替换**，因此允许落在
        # 已终态的工具卡上——这正是「回合结束后从原生历史补齐工具输出」这条路
        # （规格 §3.2-D：SSE 的 tool.completed 不带输出，输出只能事后补）。
        # 增量（``cumulative=False``）仍然不许在终态后追加：那会把一张已经收敛的
        # 卡重新拉成半截，也无法判断该接在哪一段之后。
        allow_after_terminal=event.cumulative,
    )
    return updated if updated is not None else prepared


def _handle_tool_completed(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    prepared = _ensure_tool(state, event.call_id, envelope)
    current = prepared.item(_tool_key(event.call_id))
    # AD-27：终态自带 output 就以它为准（它是最终结果）；不带 output 时保留
    # 已经增量累积起来的内容，否则流式工具一收尾输出就被清空。
    output = event.output if event.output is not None else getattr(current, "output", None)
    updated = _update_item(
        prepared,
        _tool_key(event.call_id),
        envelope,
        {
            "output": output,
            "status": "failed" if event.is_error else "completed",
        },
        terminal=True,
    )
    return updated if updated is not None else prepared


def _handle_terminal_started(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _terminal_key(event.terminal_id)
    if state.item(key) is not None:
        updated = _update_item(state, key, envelope, {"command": event.command})
        return updated if updated is not None else state
    return _append(
        state,
        TerminalItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            command=event.command,
        ),
        envelope,
    )


def _ensure_terminal(
    state: TimelineState, terminal_id: str, envelope: AgentEventEnvelope
) -> TimelineState:
    key = _terminal_key(terminal_id)
    if state.item(key) is not None:
        return state
    return _append(
        state,
        TerminalItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
        ),
        envelope,
    )


def _handle_terminal_updated(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    prepared = _ensure_terminal(state, event.terminal_id, envelope)
    key = _terminal_key(event.terminal_id)
    existing = prepared.item(key)
    assert isinstance(existing, TerminalItem)  # noqa: S101
    if _is_stale(existing, envelope) or existing.terminal:
        return _drop_stale(prepared)
    output = event.output if event.cumulative else existing.output + event.output
    return _replace(
        prepared,
        existing.model_copy(update={"output": output, "last_sequence": envelope.sequence}),
    )


def _handle_terminal_completed(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    prepared = _ensure_terminal(state, event.terminal_id, envelope)
    updated = _update_item(
        prepared,
        _terminal_key(event.terminal_id),
        envelope,
        {"exit_code": event.exit_code, "status": "completed"},
        terminal=True,
    )
    return updated if updated is not None else prepared


def _handle_file_changed(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    return _append(
        state,
        FileChangeItem(
            item_id=_event_key("file", envelope),
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            path=event.path,
            diff=event.diff,
            operation=event.operation,
        ),
        envelope,
    )


def _handle_artifact(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
    event = envelope.event
    key = f"artifact:{event.artifact.artifact_id}"
    updated = _update_item(state, key, envelope, {"artifact": event.artifact})
    if updated is not None:
        return updated
    return _append(
        state,
        ArtifactItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            artifact=event.artifact,
        ),
        envelope,
    )


def _handle_permission_requested(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _interaction_key(event.request.request_id)
    if state.item(key) is not None:
        return _drop_stale(state)
    return _append(
        state,
        InteractionItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            interaction_kind="permission",
            permission=event.request,
        ),
        envelope,
    )


def _handle_question_requested(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _interaction_key(event.request.request_id)
    if state.item(key) is not None:
        return _drop_stale(state)
    return _append(
        state,
        InteractionItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            interaction_kind="question",
            question=event.request,
        ),
        envelope,
    )


def _handle_authentication_requested(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    key = _interaction_key(event.request.request_id)
    if state.item(key) is not None:
        return _drop_stale(state)
    return _append(
        state,
        InteractionItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            interaction_kind="authentication",
            authentication=event.request,
        ),
        envelope,
    )


#: ``*.resolved`` 事件类型 → 它闭合的交互种类（AD-08 起三种齐全）。
_RESOLVED_INTERACTION_KINDS: Mapping[str, str] = {
    "permission.resolved": "permission",
    "question.resolved": "question",
    "authentication.resolved": "authentication",
}


def _handle_interaction_resolved(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    """Permission / Question / Authentication 三种交互的统一闭环（AD-08）。"""
    event = envelope.event
    key = _interaction_key(event.request_id)
    changes: dict[str, Any] = {"status": "resolved"}
    decision = getattr(event, "decision", None)
    if decision is not None:
        changes["decision"] = decision
    outcome = getattr(event, "outcome", None)
    if outcome is not None:
        changes["outcome"] = outcome
    updated = _update_item(state, key, envelope, changes, terminal=True)
    if updated is not None:
        return updated
    # 请求事件丢失（例如从中途重连）时，仍要留下一张已解决的卡，不能崩。
    kind = _RESOLVED_INTERACTION_KINDS[event.type]
    return _append(
        state,
        InteractionItem(
            item_id=key,
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            interaction_kind=kind,
            status="resolved",
            decision=decision,
            outcome=outcome,
        ),
        envelope,
    )


def _handle_usage(state: TimelineState, envelope: AgentEventEnvelope) -> TimelineState:
    event = envelope.event
    if envelope.sequence < state.last_sequence:
        return _drop_stale(state)
    return state.model_copy(update={"usage": event.usage})


def _handle_diagnostic(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    return _append(
        state,
        DiagnosticItem(
            item_id=_event_key("diagnostic", envelope),
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            level=event.level,
            message=event.message,
        ),
        envelope,
    )


def _handle_user_message(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    """``kaus`` / ``user.message`` → 一条 ``role="user"`` 的消息卡。

    直接建成终态：用户消息没有流式增量，发出去就是完整的一句。
    ``item_id`` 用事件自己的 id 派生——Session Host 生成的 ``eventId`` 已经
    全局唯一，不必再造第二套 id，也就不会和 Backend 的 ``messageId`` 撞。
    """
    data = envelope.event.data
    text = ""
    client_ref = None
    attachments: tuple[dict[str, Any], ...] = ()
    if isinstance(data, Mapping):
        text = str(data.get("text") or "")
        raw_ref = data.get("clientRef")
        client_ref = str(raw_ref) if raw_ref is not None else None
        raw_attachments = data.get("attachments")
        if isinstance(raw_attachments, list):
            attachments = tuple(dict(a) for a in raw_attachments if isinstance(a, Mapping))
    return _append(
        state,
        MessageItem(
            item_id=_event_key("message", envelope),
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            role="user",
            final_text=text,
            client_ref=client_ref,
            attachments=attachments,
        ),
        envelope,
    )


def _handle_user_message_failed(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    """``kaus`` / ``user.message.failed`` → 把对应那条用户消息标成 ``failed``。

    R6（批次三十七）：Host 在引擎接受之前就把用户那句话落库并广播了（顺序是对
    的——它是**发起**下一轮的东西），但引擎随后拒绝时，此前没有任何持久化的
    状态跟上去：刷新之后那句话看起来和被正常处理过的一模一样，重发还会显示两遍。

    这条事件**不新建卡片**，只改已有那张卡的状态——所以它在时间线上不占位置，
    也不会让「这句话说了几遍」变得答不出来。

    配对规则：有 ``clientRef`` 就按 ``clientRef`` 找（AD-94 的对账编号，前端本来
    就在用它换本地占位），没有就退回「最后一条还没被标失败的用户消息」。**找不到
    就什么都不做**——凭空标一条别的消息比不标更糟。
    """
    data = envelope.event.data if isinstance(envelope.event.data, Mapping) else {}
    client_ref = data.get("clientRef")
    target: MessageItem | None = None
    for existing in reversed(state.items):
        if not isinstance(existing, MessageItem) or existing.role != "user":
            continue
        if client_ref is not None:
            if existing.client_ref == client_ref:
                target = existing
                break
            continue
        if existing.delivery_status == "ok":
            target = existing
            break
    if target is None:
        return state
    return _replace(
        state,
        target.model_copy(
            update={
                "delivery_status": "failed",
                "failure_code": str(data.get("code") or "") or None,
                "failure_message": str(data.get("message") or "") or None,
                "last_sequence": max(target.last_sequence, envelope.sequence),
            }
        ),
    )


def _handle_extension(
    state: TimelineState, envelope: AgentEventEnvelope
) -> TimelineState:
    event = envelope.event
    if event.namespace == "kaus" and event.name == "runtime.workspace":
        return state
    if event.namespace == USER_MESSAGE_NAMESPACE and event.name == USER_MESSAGE_NAME:
        return _handle_user_message(state, envelope)
    if (
        event.namespace == USER_MESSAGE_NAMESPACE
        and event.name == USER_MESSAGE_FAILED_NAME
    ):
        return _handle_user_message_failed(state, envelope)
    return _append(
        state,
        ExtensionItem(
            item_id=_event_key("extension", envelope),
            order=envelope.sequence,
            last_sequence=envelope.sequence,
            terminal=True,
            namespace=event.namespace,
            name=event.name,
            data=event.data,
        ),
        envelope,
    )


_HANDLERS: Mapping[str, Any] = {
    "session.created": _handle_session_lifecycle,
    "session.resumed": _handle_session_lifecycle,
    "session.state": _handle_session_lifecycle,
    "run.started": _handle_run_started,
    "run.completed": _run_terminal_handler("completed"),
    "run.interrupted": _run_terminal_handler("interrupted"),
    "run.failed": _run_terminal_handler("failed"),
    "message.started": _handle_message_started,
    "message.delta": _handle_message_delta,
    "message.completed": _handle_message_completed,
    "reasoning.delta": _handle_reasoning_delta,
    "reasoning.status": _handle_reasoning,
    "plan.updated": _handle_plan,
    "tool.started": _handle_tool_started,
    "tool.updated": _handle_tool_updated,
    "tool.completed": _handle_tool_completed,
    "terminal.started": _handle_terminal_started,
    "terminal.updated": _handle_terminal_updated,
    "terminal.completed": _handle_terminal_completed,
    "file.changed": _handle_file_changed,
    "artifact.created": _handle_artifact,
    "permission.requested": _handle_permission_requested,
    "permission.resolved": _handle_interaction_resolved,
    "question.requested": _handle_question_requested,
    "question.resolved": _handle_interaction_resolved,
    "authentication.requested": _handle_authentication_requested,
    "authentication.resolved": _handle_interaction_resolved,
    "usage.updated": _handle_usage,
    "diagnostic.notice": _handle_diagnostic,
    "extension.event": _handle_extension,
}


__all__ = [
    "USER_MESSAGE_FAILED_NAME",
    "USER_MESSAGE_NAME",
    "USER_MESSAGE_NAMESPACE",
    "AnyTimelineItem",
    "ArtifactItem",
    "ChildRun",
    "DiagnosticItem",
    "ExtensionItem",
    "FileChangeItem",
    "InteractionItem",
    "LifecycleItem",
    "MessageItem",
    "PlanItem",
    "ReasoningItem",
    "RunState",
    "TerminalItem",
    "TimelineItem",
    "TimelineItemKind",
    "TimelineState",
    "ToolItem",
    "reduce_event",
    "reduce_events",
]
