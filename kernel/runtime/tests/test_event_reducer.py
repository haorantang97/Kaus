"""Runtime Reducer 测试：N §7.3 规则 1 / 2 / 3 / 9 + AD-08。

分五组：
- 规则 1：稳定 ID 更新同一张卡；
- 规则 2：``tool.updated`` 更新同一张卡；
- 规则 3：``message.delta`` 只更新对应 message，不猜「最后一条助手消息」；
- 规则 9：去重、乱序保护、终态收敛（含断线重连重放）；
- AD-08：认证请求卡的闭环，以及子 run（``parentRunId``）的归属与隔离；
- AD-27：``tool.updated`` 的增量/全量两种合并语义，与 ``reasoning.delta``
  按 ``messageId`` 累积到对应消息的 reasoning 区。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    ArtifactCreated,
    ArtifactRef,
    AuthenticationRequest,
    AuthenticationRequested,
    AuthenticationResolved,
    DiagnosticNotice,
    EventSource,
    ExtensionEvent,
    FileChanged,
    InteractionOption,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    PlanEntry,
    PlanUpdated,
    QuestionRequest,
    QuestionRequested,
    QuestionResolved,
    ReasoningDelta,
    ReasoningStatus,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    SessionCreated,
    TerminalCompleted,
    TerminalStarted,
    TerminalUpdated,
    ToolCompleted,
    ToolStarted,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
    make_envelope,
)
from runtime.event_reducer import (
    ArtifactItem,
    DiagnosticItem,
    ExtensionItem,
    FileChangeItem,
    InteractionItem,
    LifecycleItem,
    MessageItem,
    PlanItem,
    ReasoningItem,
    TerminalItem,
    TimelineState,
    ToolItem,
    reduce_event,
    reduce_events,
)

CONVERSATION = "conversation:22222222-2222-4222-8222-222222222222"
EPOCH = datetime(2026, 9, 2, tzinfo=timezone.utc)
SOURCE = EventSource(driver_kind="mock")


def env(
    event: AgentEvent,
    sequence: int,
    *,
    event_id: str | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
) -> AgentEventEnvelope:
    return make_envelope(
        event=event,
        project_id="project:pronto",
        conversation_id=CONVERSATION,
        agent_binding_id="binding:pronto:acme",
        backend_id="backend:acme",
        sequence=sequence,
        run_id=run_id,
        parent_run_id=parent_run_id,
        source=SOURCE,
        occurred_at=EPOCH + timedelta(milliseconds=sequence),
        event_id=event_id or f"evt-{sequence}",
    )


def initial() -> TimelineState:
    return TimelineState.initial(CONVERSATION)


# =========================================================================== #
# 规则 1：每条可增量更新的对象有稳定 ID，更新同一张卡
# =========================================================================== #


def test_rule1_terminal_updates_same_card() -> None:
    state = reduce_events(
        initial(),
        [
            env(TerminalStarted(terminal_id="t1", command="pytest -q"), 0),
            env(TerminalUpdated(terminal_id="t1", output="a"), 1),
            env(TerminalUpdated(terminal_id="t1", output="b"), 2),
            env(TerminalCompleted(terminal_id="t1", exit_code=0), 3),
        ],
    )
    terminals = [i for i in state.items if isinstance(i, TerminalItem)]
    assert len(terminals) == 1
    assert terminals[0].output == "ab"
    assert terminals[0].exit_code == 0
    assert terminals[0].is_terminal


def test_rule1_cumulative_terminal_output_replaces() -> None:
    state = reduce_events(
        initial(),
        [
            env(TerminalStarted(terminal_id="t1"), 0),
            env(TerminalUpdated(terminal_id="t1", output="abc", cumulative=False), 1),
            env(TerminalUpdated(terminal_id="t1", output="abcdef", cumulative=True), 2),
        ],
    )
    assert [i for i in state.items if isinstance(i, TerminalItem)][0].output == "abcdef"


def test_rule1_interaction_request_id_is_the_card_key() -> None:
    request = PermissionRequest(
        request_id="req-1",
        title="允许写入？",
        options=(InteractionOption(option_id="allow", label="允许"),),
    )
    state = reduce_events(
        initial(),
        [
            env(PermissionRequested(request=request), 0),
            env(PermissionResolved(request_id="req-1", decision="allow"), 1),
        ],
    )
    interactions = [i for i in state.items if isinstance(i, InteractionItem)]
    assert len(interactions) == 1, "请求与解决必须落在同一张卡"
    assert interactions[0].status == "resolved"
    assert interactions[0].decision == "allow"
    assert interactions[0].interaction_kind == "permission"


def test_rule1_plan_and_artifact_update_in_place() -> None:
    state = reduce_events(
        initial(),
        [
            env(PlanUpdated(plan_id="p", entries=(PlanEntry(content="一"),)), 0),
            env(
                PlanUpdated(
                    plan_id="p",
                    entries=(PlanEntry(content="一", status="completed"), PlanEntry(content="二")),
                ),
                1,
            ),
            env(ArtifactCreated(artifact=ArtifactRef(artifact_id="a1", kind="file")), 2),
            env(
                ArtifactCreated(
                    artifact=ArtifactRef(artifact_id="a1", kind="file", title="改名了")
                ),
                3,
            ),
        ],
    )
    plans = [i for i in state.items if isinstance(i, PlanItem)]
    artifacts = [i for i in state.items if isinstance(i, ArtifactItem)]
    assert len(plans) == 1 and len(plans[0].entries) == 2
    assert len(artifacts) == 1 and artifacts[0].artifact.title == "改名了"


def test_rule1_reasoning_status_is_a_rolling_card() -> None:
    state = reduce_events(
        initial(),
        [
            env(ReasoningStatus(status="thinking"), 0, run_id="run-1"),
            env(ReasoningStatus(status="planning", summary="拆任务"), 1, run_id="run-1"),
        ],
    )
    reasoning = [i for i in state.items if isinstance(i, ReasoningItem)]
    assert len(reasoning) == 1
    assert reasoning[0].status == "planning" and reasoning[0].summary == "拆任务"


def test_append_only_events_each_get_their_own_card() -> None:
    """没有稳定业务 ID 的事件按 eventId 追加，不互相覆盖。"""
    state = reduce_events(
        initial(),
        [
            env(FileChanged(path="a.txt", operation="modified"), 0),
            env(FileChanged(path="a.txt", operation="modified"), 1),
            env(DiagnosticNotice(level="warn", message="注意"), 2),
            env(ExtensionEvent(namespace="acme", name="native.x", data={"k": 1}), 3),
            env(SessionCreated(session_id="s1"), 4),
        ],
    )
    assert len([i for i in state.items if isinstance(i, FileChangeItem)]) == 2
    assert len([i for i in state.items if isinstance(i, DiagnosticItem)]) == 1
    assert len([i for i in state.items if isinstance(i, ExtensionItem)]) == 1
    assert len([i for i in state.items if isinstance(i, LifecycleItem)]) == 1


# =========================================================================== #
# 规则 2：tool.updated 更新同一张卡，不得每次产生新卡
# =========================================================================== #


def test_rule2_tool_updated_never_creates_new_card() -> None:
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read_file", input={"p": "a"}), 0),
            env(ToolUpdated(call_id="c1", progress={"pct": 10}), 1),
            env(ToolUpdated(call_id="c1", progress={"pct": 50}), 2),
            env(ToolUpdated(call_id="c1", output={"partial": True}), 3),
            env(ToolCompleted(call_id="c1", output={"done": True}), 4),
        ],
    )
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "read_file"
    assert tool.progress == {"pct": 50}
    assert tool.output == {"done": True}
    assert tool.status == "completed" and tool.is_terminal
    assert tool.order == 0, "卡片位置由首次出现的 sequence 决定，不因更新而跳动"


def test_rule2_two_tool_calls_are_two_cards() -> None:
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read"), 0),
            env(ToolStarted(call_id="c2", name="write"), 1),
            env(ToolUpdated(call_id="c1", progress=1), 2),
            env(ToolUpdated(call_id="c2", progress=2), 3),
        ],
    )
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(tools) == 2
    assert {t.item_id for t in tools} == {"tool:c1", "tool:c2"}
    assert [t.progress for t in tools] == [1, 2]


def test_rule2_tool_error_marks_failed_status() -> None:
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read"), 0),
            env(ToolCompleted(call_id="c1", output="boom", is_error=True), 1),
        ],
    )
    tool = [i for i in state.items if isinstance(i, ToolItem)][0]
    assert tool.status == "failed" and tool.is_terminal


def test_rule2_tool_update_without_start_still_lands_on_one_card() -> None:
    """从中途重连时可能只收到 update；仍然只应有一张卡。"""
    state = reduce_events(
        initial(),
        [
            env(ToolUpdated(call_id="c1", progress=1), 0),
            env(ToolUpdated(call_id="c1", progress=2), 1),
            env(ToolCompleted(call_id="c1", output="ok"), 2),
        ],
    )
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(tools) == 1 and tools[0].status == "completed"


# =========================================================================== #
# 规则 3：message.delta 只更新对应 message
# =========================================================================== #


def test_batch16_cumulative_backfill_lands_on_a_finished_tool_card() -> None:
    """批次十六第 3 件：回合结束后补的**全量**输出要落得上去。

    这条链路上工具输出只有事后才读得到（事件流不带），补账必然发生在
    ``tool.completed`` 之后。``cumulative=True`` = 整体替换，因此允许写终态卡。
    """
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="terminal", input={"preview": "echo h"}), 0),
            env(ToolCompleted(call_id="c1", output=None), 1),
            env(
                ToolUpdated(
                    call_id="c1",
                    output={"output": "hi", "exit_code": 0},
                    progress={"input": {"command": "echo hi"}},
                    cumulative=True,
                ),
                2,
            ),
        ],
    )
    tool = [i for i in state.items if isinstance(i, ToolItem)][0]
    assert tool.output == {"output": "hi", "exit_code": 0}
    assert tool.progress == {"input": {"command": "echo hi"}}
    assert tool.status == "completed" and tool.is_terminal


def test_batch16_incremental_update_after_terminal_is_still_dropped() -> None:
    """增量（``cumulative=False``）仍然不许写终态卡：没法判断该接在哪一段后面。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="terminal"), 0),
            env(ToolCompleted(call_id="c1", output="done"), 1),
            env(ToolUpdated(call_id="c1", output="late"), 2),
        ],
    )
    tool = [i for i in state.items if isinstance(i, ToolItem)][0]
    assert tool.output == "done"
    assert state.dropped_stale == 1


def test_rule3_deltas_are_routed_by_message_id() -> None:
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageStarted(message_id="m2"), 1),
            env(MessageDelta(message_id="m1", text="A1"), 2),
            env(MessageDelta(message_id="m2", text="B1"), 3),
            env(MessageDelta(message_id="m1", text="A2"), 4),
            env(MessageDelta(message_id="m2", text="B2"), 5),
        ],
    )
    assert state.message_text("m1") == "A1A2"
    assert state.message_text("m2") == "B1B2"
    assert len([i for i in state.items if isinstance(i, MessageItem)]) == 2


def test_rule3_delta_never_falls_back_to_last_assistant_message() -> None:
    """收到未知 messageId 时必须新建卡，而不是写进上一条助手消息。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageDelta(message_id="m1", text="属于 m1"), 1),
            env(MessageDelta(message_id="m-unknown", text="属于未知"), 2),
        ],
    )
    assert state.message_text("m1") == "属于 m1"
    assert state.message_text("m-unknown") == "属于未知"
    assert len([i for i in state.items if isinstance(i, MessageItem)]) == 2


def test_rule3_completed_text_wins_over_accumulated_deltas() -> None:
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageDelta(message_id="m1", text="部分"), 1),
            env(MessageCompleted(message_id="m1", text="完整文本"), 2),
        ],
    )
    message = [i for i in state.items if isinstance(i, MessageItem)][0]
    assert message.text == "完整文本"
    assert message.status == "completed"


def test_rule3_completed_without_text_keeps_accumulated_deltas() -> None:
    state = reduce_events(
        initial(),
        [
            env(MessageDelta(message_id="m1", text="a"), 0),
            env(MessageDelta(message_id="m1", text="b"), 1),
            env(MessageCompleted(message_id="m1"), 2),
        ],
    )
    assert state.message_text("m1") == "ab"


# =========================================================================== #
# 规则 9：去重 / 乱序保护 / 终态收敛
# =========================================================================== #


def test_rule9_duplicate_event_ids_are_dropped() -> None:
    duplicate = env(MessageDelta(message_id="m1", text="x"), 0, event_id="evt-dup")
    state = reduce_events(initial(), [duplicate, duplicate, duplicate])
    assert state.message_text("m1") == "x"
    assert state.dropped_duplicates == 2


def test_rule9_replay_after_reconnect_is_idempotent() -> None:
    """断线重连：把已存事件重放一遍，状态必须与只处理一次相同。"""
    stream = [
        env(RunStarted(run_id="run-1"), 0),
        env(MessageStarted(message_id="m1"), 1),
        env(MessageDelta(message_id="m1", text="hello"), 2),
        env(ToolStarted(call_id="c1", name="read"), 3),
        env(ToolUpdated(call_id="c1", progress=1), 4),
        env(ToolCompleted(call_id="c1", output="ok"), 5),
        env(MessageCompleted(message_id="m1", text="hello"), 6),
        env(RunCompleted(run_id="run-1"), 7),
    ]
    once = reduce_events(initial(), stream)
    twice = reduce_events(once, stream)

    assert twice.items == once.items
    assert twice.run_state == once.run_state == "completed"
    assert twice.last_sequence == once.last_sequence == 7
    assert twice.dropped_duplicates == len(stream)


def test_rule9_late_event_does_not_clobber_newer_state() -> None:
    """乱序保护：迟到的 tool.updated 不得覆盖已经更新过的进度。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read"), 0),
            env(ToolUpdated(call_id="c1", progress={"pct": 90}), 5),
        ],
    )
    late = env(ToolUpdated(call_id="c1", progress={"pct": 10}), 2, event_id="evt-late")
    after = reduce_event(state, late)

    tool = [i for i in after.items if isinstance(i, ToolItem)][0]
    assert tool.progress == {"pct": 90}, "迟到事件被忽略"
    assert after.dropped_stale == 1


def test_rule9_out_of_order_deltas_still_assemble_correctly() -> None:
    """文本 delta 按 sequence 装桶再排序拼接，因此乱序到达也能归位。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageDelta(message_id="m1", text="三"), 3),
            env(MessageDelta(message_id="m1", text="一"), 1),
            env(MessageDelta(message_id="m1", text="二"), 2),
        ],
    )
    assert state.message_text("m1") == "一二三"


def test_rule9_message_terminal_state_converges() -> None:
    """终态收敛：completed 之后的 delta 一律丢弃。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageDelta(message_id="m1", text="正文"), 1),
            env(MessageCompleted(message_id="m1", text="正文"), 2),
            env(MessageDelta(message_id="m1", text="迟到的补充"), 3),
            env(MessageCompleted(message_id="m1", text="又一次完成"), 4),
        ],
    )
    assert state.message_text("m1") == "正文"
    assert state.dropped_stale == 2


def test_rule9_tool_terminal_state_converges() -> None:
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read"), 0),
            env(ToolCompleted(call_id="c1", output="ok"), 1),
            env(ToolUpdated(call_id="c1", progress={"pct": 10}), 2),
        ],
    )
    tool = [i for i in state.items if isinstance(i, ToolItem)][0]
    assert tool.output == "ok" and tool.progress is None
    assert state.dropped_stale == 1


def test_rule9_interaction_terminal_state_converges() -> None:
    request = QuestionRequest(request_id="q1", prompt="选哪个？")
    state = reduce_events(
        initial(),
        [
            env(QuestionRequested(request=request), 0),
            env(QuestionResolved(request_id="q1"), 1),
            env(QuestionRequested(request=request), 2),
        ],
    )
    interactions = [i for i in state.items if isinstance(i, InteractionItem)]
    assert len(interactions) == 1 and interactions[0].status == "resolved"
    assert state.dropped_stale == 1


def test_rule9_run_state_converges_on_completion() -> None:
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-1"), 0),
            env(RunCompleted(run_id="run-1"), 1),
            env(RunStarted(run_id="run-1"), 2),
        ],
    )
    assert state.run_state == "completed", "终态后迟到的 run.started 不得把状态拉回 running"
    assert state.is_run_terminal


def test_rule9_run_failure_and_interrupt_are_terminal() -> None:
    failed = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-1"), 0),
            env(RunFailed(run_id="run-1", error=AgentError(code="e", message="炸")), 1),
            env(RunCompleted(run_id="run-1"), 2),
        ],
    )
    assert failed.run_state == "failed"
    assert failed.error is not None and failed.error.code == "e"

    interrupted = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-1"), 0),
            env(RunInterrupted(run_id="run-1", reason="user"), 1),
            env(RunCompleted(run_id="run-1"), 2),
        ],
    )
    assert interrupted.run_state == "interrupted"


def test_new_run_after_terminal_starts_fresh() -> None:
    """不同 runId 的新一轮必须能重新进入 running。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-1"), 0),
            env(RunCompleted(run_id="run-1"), 1),
            env(RunStarted(run_id="run-2"), 2),
        ],
    )
    assert state.run_state == "running" and state.active_run_id == "run-2"
    assert state.error is None


# =========================================================================== #
# 其它
# =========================================================================== #


def test_usage_is_tracked_but_never_synthesised() -> None:
    state = reduce_events(
        initial(),
        [env(UsageUpdated(usage=UsageSnapshot(input_tokens=10)), 0)],
    )
    assert state.usage is not None
    assert state.usage.input_tokens == 10
    assert state.usage.output_tokens is None


def test_reducer_is_pure_and_does_not_mutate_input_state() -> None:
    before = reduce_events(initial(), [env(ToolStarted(call_id="c1", name="read"), 0)])
    snapshot = before.model_dump()
    after = reduce_event(before, env(ToolUpdated(call_id="c1", progress=1), 1))
    assert before.model_dump() == snapshot
    assert after is not before


def test_items_keep_first_seen_order() -> None:
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(ToolStarted(call_id="c1", name="read"), 1),
            env(MessageDelta(message_id="m1", text="x"), 2),
        ],
    )
    assert [i.item_id for i in state.items] == ["message:m1", "tool:c1"]


def test_full_lifecycle_produces_all_card_kinds() -> None:
    """N §8.1：一整轮下来每类事件都能落到对应卡片。"""
    state = reduce_events(
        initial(),
        [
            env(SessionCreated(session_id="s1"), 0),
            env(RunStarted(run_id="run-1"), 1),
            env(ReasoningStatus(status="thinking"), 2, run_id="run-1"),
            env(PlanUpdated(entries=(PlanEntry(content="一"),)), 3),
            env(MessageStarted(message_id="m1"), 4),
            env(ToolStarted(call_id="c1", name="read"), 5),
            env(ToolCompleted(call_id="c1", output="ok"), 6),
            env(TerminalStarted(terminal_id="t1"), 7),
            env(TerminalCompleted(terminal_id="t1", exit_code=0), 8),
            env(FileChanged(path="a.txt"), 9),
            env(ArtifactCreated(artifact=ArtifactRef(artifact_id="a1", kind="file")), 10),
            env(
                PermissionRequested(
                    request=PermissionRequest(request_id="r1", title="允许？")
                ),
                11,
            ),
            env(PermissionResolved(request_id="r1", decision="allow"), 12),
            env(DiagnosticNotice(level="info", message="ok"), 13),
            env(ExtensionEvent(namespace="acme", name="native.x"), 14),
            env(MessageCompleted(message_id="m1", text="done"), 15),
            env(UsageUpdated(usage=UsageSnapshot(total_tokens=1)), 16),
            env(RunCompleted(run_id="run-1"), 17),
        ],
    )
    kinds = {item.kind for item in state.items}
    assert kinds == {
        "lifecycle",
        "reasoning",
        "plan",
        "message",
        "tool",
        "terminal",
        "file",
        "artifact",
        "interaction",
        "diagnostic",
        "extension",
    }
    assert state.run_state == "completed"


# =========================================================================== #
# AD-08：认证闭环 + 子 run 归属
# =========================================================================== #


def test_ad08_authentication_card_is_closed_by_resolved() -> None:
    """AD-08：认证卡与 Permission / Question 一样能被闭合，不再永远 pending。"""
    request = AuthenticationRequest(
        request_id="auth-1", message="需要认证", methods=("device-code",)
    )
    pending = reduce_events(initial(), [env(AuthenticationRequested(request=request), 0)])
    card = [i for i in pending.items if isinstance(i, InteractionItem)][0]
    assert card.interaction_kind == "authentication"
    assert card.status == "pending" and card.outcome is None

    state = reduce_event(pending, env(AuthenticationResolved(request_id="auth-1", outcome="authenticated"), 1))
    cards = [i for i in state.items if isinstance(i, InteractionItem)]
    assert len(cards) == 1, "resolved 必须更新同一张卡（N §7.3 规则 1）"
    assert cards[0].status == "resolved" and cards[0].is_terminal
    assert cards[0].outcome == "authenticated"
    # outcome 与 decision 是两个语义，认证不写 decision。
    assert cards[0].decision is None


def test_ad08_authentication_resolved_without_request_still_renders() -> None:
    """从中途重连时请求事件可能已经不在缓冲里，仍要留下一张已解决的认证卡。"""
    state = reduce_events(
        initial(), [env(AuthenticationResolved(request_id="auth-2", outcome="declined"), 0)]
    )
    card = [i for i in state.items if isinstance(i, InteractionItem)][0]
    assert card.interaction_kind == "authentication"
    assert card.status == "resolved" and card.outcome == "declined"


def test_ad08_authentication_terminal_state_converges() -> None:
    """规则 9：已解决的认证卡不被后来的请求事件拉回 pending。"""
    request = AuthenticationRequest(request_id="auth-3", message="需要认证")
    state = reduce_events(
        initial(),
        [
            env(AuthenticationRequested(request=request), 0),
            env(AuthenticationResolved(request_id="auth-3", outcome="cancelled"), 1),
            env(AuthenticationRequested(request=request), 2),
        ],
    )
    cards = [i for i in state.items if isinstance(i, InteractionItem)]
    assert len(cards) == 1 and cards[0].status == "resolved"
    assert state.dropped_stale == 1


def test_ad08_child_run_does_not_terminate_the_parent_run() -> None:
    """AD-08 的核心：子 run 结束不得把父 run 判成结束。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
            env(RunStarted(run_id="run-child"), 1, run_id="run-child", parent_run_id="run-parent"),
            env(
                MessageStarted(message_id="m-child"),
                2,
                run_id="run-child",
                parent_run_id="run-parent",
            ),
            env(RunCompleted(run_id="run-child"), 3, run_id="run-child", parent_run_id="run-parent"),
        ],
    )
    assert state.run_state == "running", "父 run 还没发自己的终态事件"
    assert state.active_run_id == "run-parent"
    child = state.child_run("run-child")
    assert child is not None and child.state == "completed" and child.parent_run_id == "run-parent"
    assert state.child_runs_of("run-parent") == (child,)

    finished = reduce_event(state, env(RunCompleted(run_id="run-parent"), 4, run_id="run-parent"))
    assert finished.run_state == "completed"


def test_ad08_child_run_failure_does_not_poison_the_parent() -> None:
    """一个委派出去的子任务失败，整轮对话不该显示成失败。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
            env(RunStarted(run_id="run-child"), 1, run_id="run-child", parent_run_id="run-parent"),
            env(
                RunFailed(run_id="run-child", error=AgentError(code="child.boom", message="子任务失败")),
                2,
                run_id="run-child",
                parent_run_id="run-parent",
            ),
        ],
    )
    assert state.run_state == "running"
    assert state.error is None, "父 run 的 error 不应被子 run 的失败污染"
    child = state.child_run("run-child")
    assert child is not None and child.state == "failed"
    assert child.error is not None and child.error.code == "child.boom"


def test_ad08_cards_are_attributed_to_their_run() -> None:
    """AD-08：每张卡记住自己属于哪个 run / 哪个父 run。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
            env(MessageStarted(message_id="m-parent"), 1, run_id="run-parent"),
            env(
                MessageStarted(message_id="m-child"),
                2,
                run_id="run-child",
                parent_run_id="run-parent",
            ),
        ],
    )
    parent_card = state.item("message:m-parent")
    child_card = state.item("message:m-child")
    assert parent_card is not None and child_card is not None
    assert parent_card.run_id == "run-parent" and parent_card.parent_run_id is None
    assert parent_card.is_from_child_run is False
    assert child_card.run_id == "run-child" and child_card.parent_run_id == "run-parent"
    assert child_card.is_from_child_run is True
    assert {i.item_id for i in state.items_of_run("run-child")} == {"message:m-child"}


def test_ad08_child_run_terminal_state_converges() -> None:
    """规则 9 同样适用于子 run：终态后的迟到事件不改状态。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
            env(RunStarted(run_id="run-child"), 1, run_id="run-child", parent_run_id="run-parent"),
            env(RunCompleted(run_id="run-child"), 2, run_id="run-child", parent_run_id="run-parent"),
            env(RunStarted(run_id="run-child"), 3, run_id="run-child", parent_run_id="run-parent"),
        ],
    )
    child = state.child_run("run-child")
    assert child is not None and child.state == "completed"
    assert len(state.child_runs) == 1
    assert state.dropped_stale >= 1


def test_ad08_two_child_runs_are_tracked_independently() -> None:
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
            env(RunStarted(run_id="run-a"), 1, run_id="run-a", parent_run_id="run-parent"),
            env(RunStarted(run_id="run-b"), 2, run_id="run-b", parent_run_id="run-parent"),
            env(RunCompleted(run_id="run-a"), 3, run_id="run-a", parent_run_id="run-parent"),
        ],
    )
    assert [c.run_id for c in state.child_runs] == ["run-a", "run-b"]
    assert state.child_run("run-a").state == "completed"
    assert state.child_run("run-b").state == "running"
    assert state.run_state == "running"


def test_ad08_replay_with_child_runs_is_idempotent() -> None:
    """规则 9：带子 run 的事件流重放两次结果一致。"""
    envelopes = [
        env(RunStarted(run_id="run-parent"), 0, run_id="run-parent"),
        env(RunStarted(run_id="run-child"), 1, run_id="run-child", parent_run_id="run-parent"),
        env(RunCompleted(run_id="run-child"), 2, run_id="run-child", parent_run_id="run-parent"),
        env(RunCompleted(run_id="run-parent"), 3, run_id="run-parent"),
    ]
    once = reduce_events(initial(), envelopes)
    twice = reduce_events(once, envelopes)
    assert twice.child_runs == once.child_runs
    assert twice.items == once.items
    assert twice.run_state == once.run_state
    assert twice.dropped_duplicates == len(envelopes)


# =========================================================================== #
# AD-27：tool.updated 的增量/全量两种合并语义 + reasoning.delta
# =========================================================================== #


def test_ad27_tool_updated_appends_by_default() -> None:
    """AD-27：默认 ``cumulative=False`` = 增量追加，分片 stdout 不再被整体替换。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="run_shell"), 0),
            env(ToolUpdated(call_id="c1", output="line-1\n"), 1),
            env(ToolUpdated(call_id="c1", output="line-2\n"), 2),
            env(ToolUpdated(call_id="c1", output="line-3\n"), 3),
        ],
    )
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(tools) == 1, "AD-27 只改合并语义，规则 2「同一张卡」不变"
    assert tools[0].output == "line-1\nline-2\nline-3\n"


def test_ad27_tool_updated_cumulative_replaces() -> None:
    """AD-27：``cumulative=True`` = 到目前为止的全量，整体替换（与 terminal.updated 对称）。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="run_shell"), 0),
            env(ToolUpdated(call_id="c1", output="line-1\n", cumulative=False), 1),
            env(ToolUpdated(call_id="c1", output="line-1\nline-2\n", cumulative=True), 2),
        ],
    )
    assert [i for i in state.items if isinstance(i, ToolItem)][0].output == (
        "line-1\nline-2\n"
    )


def test_ad27_tool_and_terminal_cumulative_are_symmetric() -> None:
    """AD-27 的原话是「与 terminal.updated 对称」——同样的分片必须得到同样的结果。"""
    chunks = ("a", "b", "c")
    tool_state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="run_shell"), 0),
            *(env(ToolUpdated(call_id="c1", output=c), i + 1) for i, c in enumerate(chunks)),
        ],
    )
    terminal_state = reduce_events(
        initial(),
        [
            env(TerminalStarted(terminal_id="t1"), 0),
            *(
                env(TerminalUpdated(terminal_id="t1", output=c), i + 1)
                for i, c in enumerate(chunks)
            ),
        ],
    )
    tool = [i for i in tool_state.items if isinstance(i, ToolItem)][0]
    terminal = [i for i in terminal_state.items if isinstance(i, TerminalItem)][0]
    assert tool.output == terminal.output == "abc"


def test_ad27_non_text_tool_output_is_replaced_not_appended() -> None:
    """追加只对文本有定义：结构化输出没有「追加」这回事，整体替换。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="read_file"), 0),
            env(ToolUpdated(call_id="c1", output={"partial": True}), 1),
            env(ToolUpdated(call_id="c1", output={"partial": False}), 2),
        ],
    )
    assert [i for i in state.items if isinstance(i, ToolItem)][0].output == {
        "partial": False
    }


def test_ad27_tool_completed_without_output_keeps_accumulated_chunks() -> None:
    """流式工具收尾时不重复发全量输出，已累积的内容不得被清空。"""
    state = reduce_events(
        initial(),
        [
            env(ToolStarted(call_id="c1", name="run_shell"), 0),
            env(ToolUpdated(call_id="c1", output="hello "), 1),
            env(ToolUpdated(call_id="c1", output="world"), 2),
            env(ToolCompleted(call_id="c1"), 3),
        ],
    )
    tool = [i for i in state.items if isinstance(i, ToolItem)][0]
    assert tool.output == "hello world"
    assert tool.status == "completed" and tool.is_terminal


def test_ad27_reasoning_delta_accumulates_into_its_message() -> None:
    """AD-27：思考增量按 messageId 累积到对应消息的 reasoning 区。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(ReasoningDelta(message_id="m1", text="先"), 1),
            env(ReasoningDelta(message_id="m1", text="想一下"), 2),
            env(MessageDelta(message_id="m1", text="答案是 42"), 3),
            env(MessageCompleted(message_id="m1", text="答案是 42"), 4),
        ],
    )
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert len(messages) == 1, "思考不另建卡，它属于这条消息"
    assert messages[0].reasoning_text == "先想一下"
    assert messages[0].has_reasoning
    # 思考不得混进正文。
    assert messages[0].text == "答案是 42"


def test_ad27_reasoning_delta_is_routed_by_message_id() -> None:
    """规则 3 同样适用于思考：只认自己的 messageId，绝不落到「最后一条消息」。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(MessageStarted(message_id="m2"), 1),
            env(ReasoningDelta(message_id="m1", text="属于 m1"), 2),
            env(ReasoningDelta(message_id="m2", text="属于 m2"), 3),
        ],
    )
    by_id = {i.item_id: i for i in state.items if isinstance(i, MessageItem)}
    assert by_id["message:m1"].reasoning_text == "属于 m1"
    assert by_id["message:m2"].reasoning_text == "属于 m2"


def test_ad27_reasoning_delta_without_message_started_creates_the_card() -> None:
    """Backend 可能省略 message.started：思考片段照样隐式建卡，不得丢。"""
    state = reduce_events(
        initial(), [env(ReasoningDelta(message_id="m9", text="思考中"), 0)]
    )
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert len(messages) == 1
    assert messages[0].item_id == "message:m9"
    assert messages[0].reasoning_text == "思考中"
    assert messages[0].text == ""


def test_ad27_out_of_order_reasoning_deltas_still_assemble() -> None:
    """规则 9：迟到的思考片段按 sequence 归位，不是按到达顺序。"""
    state = reduce_events(
        initial(),
        [
            env(ReasoningDelta(message_id="m1", text="尾"), 3),
            env(ReasoningDelta(message_id="m1", text="中"), 2),
            env(ReasoningDelta(message_id="m1", text="头"), 1),
        ],
    )
    assert [i for i in state.items if isinstance(i, MessageItem)][0].reasoning_text == (
        "头中尾"
    )


def test_ad27_reasoning_delta_after_message_completed_is_dropped() -> None:
    """终态收敛对思考区一视同仁。"""
    state = reduce_events(
        initial(),
        [
            env(MessageStarted(message_id="m1"), 0),
            env(ReasoningDelta(message_id="m1", text="想"), 1),
            env(MessageCompleted(message_id="m1", text="好了"), 2),
            env(ReasoningDelta(message_id="m1", text="还想"), 3),
        ],
    )
    message = [i for i in state.items if isinstance(i, MessageItem)][0]
    assert message.reasoning_text == "想"
    assert state.dropped_stale == 1


def test_ad27_reasoning_status_semantics_are_unchanged() -> None:
    """AD-27 明确要求 ``reasoning.status`` 语义不变：仍是 run 粒度的滚动卡片。"""
    state = reduce_events(
        initial(),
        [
            env(RunStarted(run_id="r1"), 0, run_id="r1"),
            env(ReasoningStatus(status="thinking", summary="规划中"), 1, run_id="r1"),
            env(MessageStarted(message_id="m1"), 2, run_id="r1"),
            env(ReasoningDelta(message_id="m1", text="逐字思考"), 3, run_id="r1"),
            env(ReasoningStatus(status="done", summary="想好了"), 4, run_id="r1"),
        ],
    )
    reasoning_items = [i for i in state.items if isinstance(i, ReasoningItem)]
    assert len(reasoning_items) == 1, "status 仍然是同一张滚动卡"
    assert reasoning_items[0].status == "done"
    assert reasoning_items[0].summary == "想好了"
    # 两条通道互不覆盖。
    message = [i for i in state.items if isinstance(i, MessageItem)][0]
    assert message.reasoning_text == "逐字思考"


def test_ad27_replay_with_reasoning_and_streamed_tool_output_is_idempotent() -> None:
    """规则 9：新增的两种合并语义都必须经得起断线重放（否则输出会翻倍）。"""
    envelopes = [
        env(RunStarted(run_id="r1"), 0, run_id="r1"),
        env(MessageStarted(message_id="m1"), 1, run_id="r1"),
        env(ReasoningDelta(message_id="m1", text="思考"), 2, run_id="r1"),
        env(ToolStarted(call_id="c1", name="run_shell"), 3, run_id="r1"),
        env(ToolUpdated(call_id="c1", output="out-1\n"), 4, run_id="r1"),
        env(ToolUpdated(call_id="c1", output="out-2\n"), 5, run_id="r1"),
        env(ToolCompleted(call_id="c1"), 6, run_id="r1"),
        env(MessageCompleted(message_id="m1", text="完成"), 7, run_id="r1"),
        env(RunCompleted(run_id="r1"), 8, run_id="r1"),
    ]
    once = reduce_events(initial(), envelopes)
    twice = reduce_events(once, envelopes)
    assert twice.items == once.items
    assert twice.dropped_duplicates == len(envelopes)
    tool = [i for i in once.items if isinstance(i, ToolItem)][0]
    assert tool.output == "out-1\nout-2\n"
    message = [i for i in once.items if isinstance(i, MessageItem)][0]
    assert message.reasoning_text == "思考"
