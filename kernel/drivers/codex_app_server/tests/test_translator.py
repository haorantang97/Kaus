"""Pure translator tests: notification shapes from ``codex-rs/app-server-protocol`` v2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from drivers.codex_app_server import protocol as P
from drivers.codex_app_server.translator import AppServerTranslator, TranslationContext
from runtime.event_reducer import InteractionItem, MessageItem, TimelineState, ToolItem, reduce_events

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
THREAD = "0b1f6f6e-3c2a-4e5b-9f0e-7f0c3d2a1b11"
CONTEXT = TranslationContext(project_id="project:p", conversation_id="conversation:11111111-1111-4111-8111-111111111111",
                             agent_binding_id="binding:p:codex", backend_id="backend:codex", thread_id=THREAD,
                             run_token="a1b2c3d4", driver_version="0.1.0", backend_version="0.167.0")


def make() -> AppServerTranslator:
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return EPOCH + timedelta(milliseconds=counter["n"])

    return AppServerTranslator(CONTEXT, clock=clock)


def turn(**extra):
    return {"threadId": THREAD, "turnId": "turn-1", **extra}


def test_reasoning_then_message_share_one_card_and_run_id_carries_the_token() -> None:
    t = make()
    events = list(t.begin_run())
    assert events[0].event.run_id == f"{THREAD}:a1b2c3d4:r1"
    events += t.on_notification(P.NOTE_ITEM_STARTED, turn(item={"type": "reasoning", "id": "rs1", "summary": [], "content": []}))
    events += t.on_notification(P.NOTE_REASONING_SUMMARY_DELTA, turn(itemId="rs1", delta="think", summaryIndex=0))
    events += t.on_notification(P.NOTE_ITEM_COMPLETED, turn(item={"type": "reasoning", "id": "rs1", "summary": ["think"], "content": []}))
    events += t.on_notification(P.NOTE_ITEM_STARTED, turn(item={"type": "agentMessage", "id": "m1", "text": ""}))
    events += t.on_notification(P.NOTE_AGENT_MESSAGE_DELTA, turn(itemId="m1", delta="Hel"))
    events += t.on_notification(P.NOTE_AGENT_MESSAGE_DELTA, turn(itemId="m1", delta="lo"))
    events += t.on_notification(P.NOTE_ITEM_COMPLETED, turn(item={"type": "agentMessage", "id": "m1", "text": "Hello"}))
    events += t.on_notification(P.NOTE_TURN_COMPLETED, {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed", "error": None}})
    types = [e.event.type for e in events]
    assert types == ["run.started", "message.started", "reasoning.delta", "message.delta", "message.delta",
                     "message.completed", "run.completed"]
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), tuple(events))
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert len(messages) == 1 and messages[0].text == "Hello" and messages[0].reasoning_text == "think"
    assert state.run_state == "completed"
    assert len({e.event_id for e in events}) == len(events)


def test_command_item_is_one_tool_card_with_incremental_output() -> None:
    t = make()
    events = list(t.begin_run())
    base = {"type": "commandExecution", "id": "cmd1", "command": "ls", "cwd": "/tmp"}
    events += t.on_notification(P.NOTE_ITEM_STARTED, turn(item={**base, "status": "inProgress"}))
    events += t.on_notification(P.NOTE_COMMAND_OUTPUT_DELTA, turn(itemId="cmd1", delta="a\n"))
    events += t.on_notification(P.NOTE_COMMAND_OUTPUT_DELTA, turn(itemId="cmd1", delta="b\n"))
    events += t.on_notification(P.NOTE_ITEM_COMPLETED, turn(item={**base, "status": "completed", "exitCode": 0, "aggregatedOutput": "a\nb\n"}))
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), tuple(events))
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(tools) == 1 and tools[0].name == "ls" and tools[0].status == "completed"
    updates = [e for e in events if e.event.type == "tool.updated"]
    assert [u.event.cumulative for u in updates] == [False, False]
    # a non-zero exit is an error even when the status says completed
    tr = make()
    tr.begin_run()
    done = tr.on_notification(P.NOTE_ITEM_COMPLETED, turn(item={**base, "status": "completed", "exitCode": 2, "aggregatedOutput": "x"}))
    assert [e.event.type for e in done] == ["tool.started", "tool.completed"] and done[-1].event.is_error is True


def test_file_change_emits_file_changed_rows_and_approval_round_trip() -> None:
    t = make()
    events = list(t.begin_run())
    events += t.on_approval_request(7, P.REQ_FILE_CHANGE_APPROVAL, turn(itemId="patch1", startedAtMs=0, reason="write", grantRoot="/tmp"))
    request = events[-1].event.request
    assert [o.option_id for o in request.options] == ["accept", "acceptForSession", "decline", "cancel"]
    pending = t.pending_approval(request.request_id)
    assert pending is not None and pending.rpc_id == 7 and pending.item_id == "patch1"
    t.take_approval(request.request_id)
    events += t.on_permission_resolved(request.request_id, "accept")
    changes = [{"path": "/tmp/a", "kind": {"type": "add"}, "diff": "+a"}, {"path": "/tmp/b", "kind": {"type": "update", "movePath": None}, "diff": "-x"}]
    events += t.on_notification(P.NOTE_ITEM_STARTED, turn(item={"type": "fileChange", "id": "patch1", "changes": changes, "status": "inProgress"}))
    events += t.on_notification(P.NOTE_ITEM_COMPLETED, turn(item={"type": "fileChange", "id": "patch1", "changes": changes, "status": "completed"}))
    changed = [e.event for e in events if e.event.type == "file.changed"]
    assert [(c.path, c.operation) for c in changed] == [("/tmp/a", "add"), ("/tmp/b", "update")]
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), tuple(events))
    interactions = [i for i in state.items if isinstance(i, InteractionItem)]
    assert interactions and interactions[0].status == "resolved"


def test_available_decisions_filter_the_card_options() -> None:
    t = make()
    t.begin_run()
    events = t.on_approval_request(1, P.REQ_COMMAND_APPROVAL, turn(itemId="c", startedAtMs=0, command="rm -rf x",
                                                                  availableDecisions=["accept", "decline"]))
    assert [o.option_id for o in events[-1].event.request.options] == ["accept", "decline"]
    assert events[-1].event.request.title == "rm -rf x"


def test_user_input_questions_answer_one_by_one_and_build_the_reply() -> None:
    t = make()
    t.begin_run()
    events = t.on_user_input_request(9, turn(itemId="ask", isBlocking=True, questions=[
        {"id": "q1", "header": "H", "question": "One?", "isOther": False, "options": [{"label": "yes", "description": ""}]},
        {"id": "q2", "header": "H", "question": "Two?", "isOther": True, "options": None}]))
    requests = [e.event.request for e in events if e.event.type == "question.requested"]
    assert len(requests) == 2 and requests[0].allow_free_text is False and requests[1].allow_free_text is True
    resolved, batch, cancelled = t.answer_question(requests[0].request_id, option_id="o1", text=None, cancelled=False)
    assert batch is None and not cancelled and resolved[0].event.type == "question.resolved"
    resolved, batch, cancelled = t.answer_question(requests[1].request_id, option_id=None, text="free", cancelled=False)
    assert batch is not None and batch.rpc_id == 9
    assert batch.response() == {"answers": {"q1": {"answers": ["yes"]}, "q2": {"answers": ["free"]}}}


def test_foreign_thread_and_stale_turn_notifications_are_dropped() -> None:
    t = make()
    t.begin_run()
    assert t.on_notification(P.NOTE_AGENT_MESSAGE_DELTA, {"threadId": "other", "turnId": "turn-1", "itemId": "m", "delta": "x"}) == ()
    assert t.ignored_foreign == 1
    t.on_notification(P.NOTE_TURN_COMPLETED, {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}})
    assert t.on_notification(P.NOTE_AGENT_MESSAGE_DELTA, turn(itemId="m", delta="late")) == ()
    assert t.ignored_after_terminal >= 1


def test_failed_turn_and_rejected_turn_start_both_converge_to_run_failed() -> None:
    t = make()
    t.begin_run()
    events = t.on_notification(P.NOTE_TURN_COMPLETED, {"threadId": THREAD, "turn": {"id": "turn-1", "status": "failed",
                                                      "error": {"message": "boom", "codexErrorInfo": "responseStreamDisconnected"}}})
    assert events[-1].event.type == "run.failed" and events[-1].event.error.code == "responseStreamDisconnected"
    t2 = make()
    t2.begin_run()
    failed = t2.on_error(-32603, "rejected")
    assert failed[-1].event.type == "run.failed" and failed[-1].event.error.code == "-32603"
    assert t2.on_error(-32603, "again") == ()


def test_usage_plan_and_unknown_notifications() -> None:
    t = make()
    t.begin_run()
    usage = t.on_notification(P.NOTE_TOKEN_USAGE, turn(tokenUsage={"total": {"totalTokens": 10, "inputTokens": 6, "cachedInputTokens": 1,
                                                                   "outputTokens": 4, "reasoningOutputTokens": 2}, "last": {}, "modelContextWindow": 100}))
    snapshot = usage[0].event.usage
    assert (snapshot.total_tokens, snapshot.input_tokens, snapshot.reasoning_tokens, snapshot.context_window) == (10, 6, 2, 100)
    plan = t.on_notification(P.NOTE_TURN_PLAN, turn(explanation=None, plan=[{"step": "a", "status": "inProgress"}]))
    assert plan[-1].event.type == "plan.updated" and plan[-1].event.entries[0].status == "in_progress"
    unknown = t.on_notification("thread/name/updated", {"threadId": THREAD, "threadName": "x"})
    assert unknown[0].event.type == "extension.event" and unknown[0].event.namespace == "codex-app-server"


def test_approval_settings_round_trip() -> None:
    assert P.approval_settings("ask") == ("on-request", "user", "workspace-write")
    assert P.approval_settings("bypass") == ("never", "user", "danger-full-access")
    assert P.approval_settings("deny") == ("never", "user", "workspace-write")
    assert P.approval_settings("plan") is None and P.approval_settings("host:ask") is None
    assert P.policy_from_settings("on-request", {"type": "workspaceWrite"}, "user") == "ask"
    assert P.policy_from_settings("never", "danger-full-access") == "bypass"
    tables = P.mcp_servers_config([{"name": "fs", "command": "uvx", "args": ["x"], "env": [{"name": "A", "value": "1"}]},
                                   {"name": "web", "type": "http", "url": "https://h", "headers": [{"name": "Authorization", "value": "t"}]}])
    assert tables == {"fs": {"command": "uvx", "args": ["x"], "env": {"A": "1"}}, "web": {"url": "https://h", "http_headers": {"Authorization": "t"}}}
