"""翻译器单测：乱序 / 重复通知、权限往返、cancel、以及「不伪造」的边界。

这里用的报文形状与 :mod:`drivers.acp.testing.fake_acp_agent` 同源（ACP 规范 +
探针实测），但**不起子进程**——翻译器是纯的，所以可以逐条断言。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drivers.acp.translator import (
    ACP_EXTENSION_NAMESPACE,
    AcpTranslationContext,
    AcpTranslator,
    make_event_id,
)
from runtime.event_envelope import SCHEMA_VERSION
from runtime.event_reducer import (
    InteractionItem,
    MessageItem,
    TimelineState,
    ToolItem,
    reduce_events,
)

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

CONTEXT = AcpTranslationContext(
    project_id="project:contract",
    conversation_id="conversation:11111111-1111-4111-8111-111111111111",
    agent_binding_id="binding:contract:acp",
    backend_id="backend:acp",
    session_id="47001d1c-5a8e-47cd-b05a-fd1cbee30758",
    # batch53 / AD-174：唯一性从外面注入，所以单测里它就是个**定值**——
    # 翻译器仍然是「同一串输入产出同一串输出」的纯函数。
    run_token="a1b2c3d4",
    driver_version="0.1.0",
    backend_version="9.9.9",
)


def make_translator() -> AcpTranslator:
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return EPOCH + timedelta(milliseconds=counter["n"])

    return AcpTranslator(CONTEXT, clock=clock)


def update(session_id: str | None = None, **fields: object) -> dict:
    return {
        "sessionId": session_id if session_id is not None else CONTEXT.session_id,
        "update": dict(fields),
    }


def text_chunk(text: str) -> dict:
    return update(
        sessionUpdate="agent_message_chunk", content={"type": "text", "text": text}
    )


def types_of(envelopes) -> list[str]:
    return [e.event.type for e in envelopes]


# --------------------------------------------------------------------------- #
# 信封形状与 ID 规则
# --------------------------------------------------------------------------- #


def test_envelope_header_is_filled_from_context() -> None:
    translator = make_translator()
    (created,) = translator.session_created()
    assert created.schema_version == SCHEMA_VERSION
    assert created.project_id == CONTEXT.project_id
    assert created.conversation_id == CONTEXT.conversation_id
    assert created.agent_binding_id == CONTEXT.agent_binding_id
    assert created.backend_id == CONTEXT.backend_id
    assert created.native_session_id == CONTEXT.session_id
    assert created.source.driver_kind == "acp"
    assert created.source.backend_version == "9.9.9"
    # ACP 没有委派概念：parentRunId 恒为空。
    assert created.parent_run_id is None


def test_event_ids_are_deterministic_uuid5() -> None:
    """AD-37 同族规则：同一串输入两次翻译，eventId 逐条相同。"""
    first = make_translator()
    second = make_translator()
    first_events = [*first.begin_run(), *first.on_session_update(text_chunk("a"))]
    second_events = [*second.begin_run(), *second.on_session_update(text_chunk("a"))]
    assert [e.event_id for e in first_events] == [e.event_id for e in second_events]
    ids_first = [
        make_event_id(CONTEXT.agent_binding_id, CONTEXT.session_id, n, run_token=CONTEXT.run_token)
        for n in range(3)
    ]
    produced = []
    third = make_translator()
    produced += list(third.begin_run())
    produced += list(third.on_session_update(text_chunk("a")))
    assert [e.event_id for e in produced] == ids_first
    assert len({e.event_id for e in produced}) == len(produced)


def test_event_ids_isolate_different_attachments_to_the_same_native_session() -> None:
    from dataclasses import replace

    first = make_translator()
    resumed = AcpTranslator(replace(CONTEXT, run_token="new-attach"), clock=lambda: EPOCH)
    first_events = [*first.session_created(), *first.begin_run(), *first.on_session_update(text_chunk("old")), *first.on_stop_reason("end_turn")]
    resumed_events = [*resumed.session_resumed(), *resumed.begin_run(), *resumed.on_session_update(text_chunk("new")), *resumed.on_stop_reason("end_turn")]
    assert [e.sequence for e in first_events] == [e.sequence for e in resumed_events]
    assert {e.event_id for e in first_events}.isdisjoint(e.event_id for e in resumed_events)


def test_id_rules_match_the_frozen_docstring() -> None:
    translator = make_translator()
    (started,) = translator.begin_run()
    run_id = f"{CONTEXT.session_id}:{CONTEXT.run_token}:r1"
    assert started.event.run_id == run_id
    events = translator.on_session_update(text_chunk("hi"))
    assert types_of(events) == ["message.started", "message.delta"]
    assert events[0].event.message_id == f"{run_id}:m1"


def test_sequence_is_monotonic_and_unique() -> None:
    translator = make_translator()
    envelopes = [
        *translator.session_created(),
        *translator.begin_run(),
        *translator.on_session_update(text_chunk("a")),
        *translator.on_session_update(text_chunk("b")),
        *translator.on_stop_reason("end_turn"),
    ]
    sequences = [e.sequence for e in envelopes]
    assert sequences == sorted(sequences) == list(range(len(sequences)))


# --------------------------------------------------------------------------- #
# 文本 / 思考 / 消息段
# --------------------------------------------------------------------------- #


def test_text_deltas_merge_into_one_message() -> None:
    translator = make_translator()
    envelopes = [
        *translator.begin_run(),
        *translator.on_session_update(text_chunk("Hello")),
        *translator.on_session_update(text_chunk(", ")),
        *translator.on_session_update(text_chunk("world.")),
        *translator.on_stop_reason("end_turn"),
    ]
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert len(messages) == 1
    assert messages[0].text == "Hello, world."
    assert messages[0].is_terminal


def test_thought_chunks_become_reasoning_delta_on_the_same_message() -> None:
    """AD-27：思考归到**这条消息**的思考区，且绝不混进正文。"""
    translator = make_translator()
    thought = update(
        sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "Let me "}
    )
    thought2 = update(
        sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "think."}
    )
    envelopes = [
        *translator.begin_run(),
        *translator.on_session_update(thought),
        *translator.on_session_update(thought2),
        *translator.on_session_update(text_chunk("Answer.")),
        *translator.on_stop_reason("end_turn"),
    ]
    assert types_of(envelopes) == [
        "run.started",
        "message.started",
        "reasoning.delta",
        "reasoning.delta",
        "message.delta",
        "message.completed",
        "run.completed",
    ]
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert len(messages) == 1
    assert messages[0].reasoning_text == "Let me think."
    assert messages[0].text == "Answer."
    # ACP 没有「思考状态」这种事件，因此永远不产出 reasoning.status。
    assert "reasoning.status" not in types_of(envelopes)


def test_tool_call_closes_the_open_message_segment() -> None:
    translator = make_translator()
    envelopes = [
        *translator.begin_run(),
        *translator.on_session_update(text_chunk("before")),
        *translator.on_session_update(
            update(sessionUpdate="tool_call", toolCallId="c1", title="read", status="pending")
        ),
        *translator.on_session_update(text_chunk("after")),
        *translator.on_stop_reason("end_turn"),
    ]
    message_ids = [
        e.event.message_id for e in envelopes if e.event.type == "message.started"
    ]
    run_id = f"{CONTEXT.session_id}:{CONTEXT.run_token}:r1"
    assert message_ids == [f"{run_id}:m1", f"{run_id}:m2"]
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert [m.text for m in messages] == ["before", "after"]


def test_non_text_content_block_does_not_leak_into_the_message() -> None:
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_session_update(
        update(
            sessionUpdate="agent_message_chunk",
            content={"type": "image", "data": "AAAA", "mimeType": "image/png"},
        )
    )
    assert [e.event.type for e in envelopes] == ["artifact.created"]
    assert envelopes[0].event.artifact.uri == "data:image/png;base64,AAAA"


# --------------------------------------------------------------------------- #
# 工具：乱序、重复、终态
# --------------------------------------------------------------------------- #


def test_tool_call_uses_acp_tool_call_id_verbatim() -> None:
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_session_update(
        update(
            sessionUpdate="tool_call",
            toolCallId="call_001",
            title="read_file",
            kind="read",
            status="pending",
            rawInput={"path": "README.md"},
        )
    )
    assert types_of(envelopes) == ["tool.started"]
    started = envelopes[0].event
    assert started.call_id == "call_001"
    assert started.name == "read_file"
    assert started.input == {"path": "README.md"}


def test_duplicate_tool_call_is_dropped() -> None:
    translator = make_translator()
    translator.begin_run()
    call = update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
    assert types_of(translator.on_session_update(call)) == ["tool.started"]
    assert translator.on_session_update(dict(call)) == ()
    assert translator.ignored_duplicate_tool == 1


def test_out_of_order_tool_update_synthesises_tool_started() -> None:
    """更新先于 ``tool_call`` 到达时补一条 ``tool.started``，否则卡建不出来。"""
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_session_update(
        update(
            sessionUpdate="tool_call_update",
            toolCallId="c9",
            status="in_progress",
            content=[{"type": "content", "content": {"type": "text", "text": "x"}}],
        )
    )
    assert types_of(envelopes) == ["tool.started", "tool.updated"]
    assert envelopes[0].event.call_id == "c9"
    # 名字没得抄时退回 toolCallId，不编一个。
    assert envelopes[0].event.name == "c9"


def test_updates_after_terminal_tool_are_dropped() -> None:
    translator = make_translator()
    translator.begin_run()
    translator.on_session_update(
        update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
    )
    done = translator.on_session_update(
        update(sessionUpdate="tool_call_update", toolCallId="c1", status="completed")
    )
    assert types_of(done) == ["tool.completed"]
    late = translator.on_session_update(
        update(sessionUpdate="tool_call_update", toolCallId="c1", status="in_progress")
    )
    assert late == ()
    assert translator.ignored_after_terminal == 1


def test_tool_output_is_cumulative_snapshot() -> None:
    """ACP 未规定 content 是追加还是替换 → 一律按全量快照（cumulative=True）。"""
    translator = make_translator()
    translator.begin_run()
    translator.on_session_update(
        update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
    )
    (updated,) = translator.on_session_update(
        update(
            sessionUpdate="tool_call_update",
            toolCallId="c1",
            status="in_progress",
            content=[{"type": "content", "content": {"type": "text", "text": "one"}}],
        )
    )
    assert updated.event.cumulative is True
    assert updated.event.output == "one"
    assert updated.event.progress == "in_progress"


def test_tool_completed_without_output_keeps_accumulated_content() -> None:
    """AD-38：终态不带 output 时保留已累积内容（合并由 Reducer 落实）。"""
    translator = make_translator()
    envelopes = [
        *translator.begin_run(),
        *translator.on_session_update(
            update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
        ),
        *translator.on_session_update(
            update(
                sessionUpdate="tool_call_update",
                toolCallId="c1",
                status="in_progress",
                content=[{"type": "content", "content": {"type": "text", "text": "partial"}}],
            )
        ),
        *translator.on_session_update(
            update(sessionUpdate="tool_call_update", toolCallId="c1", status="completed")
        ),
        *translator.on_stop_reason("end_turn"),
    ]
    completed = [e for e in envelopes if e.event.type == "tool.completed"]
    assert completed and completed[0].event.output is None
    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    (tool,) = [i for i in state.items if isinstance(i, ToolItem)]
    assert tool.output == "partial"
    assert tool.status == "completed"


def test_failed_tool_status_marks_is_error() -> None:
    translator = make_translator()
    translator.begin_run()
    translator.on_session_update(
        update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
    )
    (completed,) = translator.on_session_update(
        update(sessionUpdate="tool_call_update", toolCallId="c1", status="failed")
    )
    assert completed.event.is_error is True


# --------------------------------------------------------------------------- #
# 乱序 / 越界的会话
# --------------------------------------------------------------------------- #


def test_updates_for_a_foreign_session_are_dropped_not_guessed() -> None:
    """N §6.1/§6.2：会话身份不许猜——别的会话的报文整条丢弃。"""
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_session_update(
        {
            "sessionId": "some-other-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "not mine"},
            },
        }
    )
    assert envelopes == ()
    assert translator.ignored_foreign == 1


def test_unknown_session_update_kind_goes_to_extension() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_session_update(
        update(sessionUpdate="brand_new_thing", payload={"a": 1})
    )
    assert envelope.event.type == "extension.event"
    assert envelope.event.namespace == ACP_EXTENSION_NAMESPACE
    assert envelope.event.name == "brand_new_thing"
    assert translator.unknown_updates == ["brand_new_thing"]


def test_type_spelling_of_the_discriminator_is_accepted() -> None:
    """判别字段在规范文本里是 ``sessionUpdate``，生成的 schema 文档里写作
    ``type``；两种都认，免得因为一个字段名把整条流丢掉。"""
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_session_update(
        update(type="agent_message_chunk", content={"type": "text", "text": "hi"})
    )
    assert types_of(envelopes) == ["message.started", "message.delta"]


def test_user_message_chunk_is_not_an_assistant_message() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_session_update(
        update(sessionUpdate="user_message_chunk", content={"type": "text", "text": "hi"})
    )
    assert envelope.event.type == "extension.event"
    assert envelope.event.name == "user_message_chunk"


# --------------------------------------------------------------------------- #
# 权限往返
# --------------------------------------------------------------------------- #


PERMISSION_PARAMS = {
    "sessionId": CONTEXT.session_id,
    "toolCall": {"toolCallId": "call_001", "title": "run_shell"},
    "options": [
        {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
    ],
}


def test_permission_request_carries_options_verbatim() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_permission_request(7, PERMISSION_PARAMS)
    request = envelope.event.request
    assert envelope.event.type == "permission.requested"
    assert request.request_id == f"{CONTEXT.session_id}:{CONTEXT.run_token}:r1:p1"
    assert request.tool_call_id == "call_001"
    assert request.title == "run_shell"
    assert [(o.option_id, o.label, o.kind) for o in request.options] == [
        ("allow", "Allow once", "allow_once"),
        ("deny", "Reject", "reject_once"),
    ]
    pending = translator.pending_permission(request.request_id)
    assert pending is not None and pending.rpc_id == 7


def test_permission_round_trip_closes_the_card() -> None:
    translator = make_translator()
    envelopes = list(translator.begin_run())
    envelopes += list(translator.on_session_update(text_chunk("about to run")))
    envelopes += list(translator.on_permission_request(7, PERMISSION_PARAMS))
    request_id = f"{CONTEXT.session_id}:{CONTEXT.run_token}:r1:p1"
    pending = translator.take_permission(request_id)
    assert pending is not None and pending.rpc_id == 7
    assert translator.take_permission(request_id) is None, "取过一次就注销"
    envelopes += list(translator.on_permission_resolved(request_id, "allow"))
    envelopes += list(translator.on_stop_reason("end_turn"))

    state = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    (interaction,) = [i for i in state.items if isinstance(i, InteractionItem)]
    assert interaction.interaction_kind == "permission"
    assert interaction.status == "resolved"
    assert interaction.decision == "allow"
    # 权限请求会关掉开着的消息段（N §7.3 规则 4：它不是消息的一部分）。
    assert "message.completed" in types_of(envelopes)


def test_permission_request_for_a_foreign_session_is_dropped() -> None:
    translator = make_translator()
    translator.begin_run()
    assert translator.on_permission_request(1, {**PERMISSION_PARAMS, "sessionId": "x"}) == ()
    assert translator.ignored_foreign == 1


# --------------------------------------------------------------------------- #
# run 终态：cancel / stopReason / 错误
# --------------------------------------------------------------------------- #


def test_cancelled_stop_reason_becomes_run_interrupted() -> None:
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_stop_reason("cancelled")
    assert types_of(envelopes) == ["run.interrupted"]
    assert envelopes[0].event.reason == "cancelled"


def test_driver_side_interrupt_flag_wins_over_stop_reason() -> None:
    """``session/cancel`` 是通知、无回执：终态只能从 stopReason 回来，但如果
    Driver 已经判定这一轮是被用户打断的，就必须落 ``run.interrupted``。"""
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_stop_reason("end_turn", interrupted=True)
    assert types_of(envelopes) == ["run.interrupted"]


def test_terminal_state_is_emitted_only_once() -> None:
    translator = make_translator()
    translator.begin_run()
    assert types_of(translator.on_stop_reason("end_turn")) == ["run.completed"]
    assert translator.on_stop_reason("cancelled", interrupted=True) == ()
    assert translator.on_error(-32000, "late failure") == ()
    assert translator.ignored_after_terminal == 2


def test_unusual_stop_reason_is_explained_via_extension() -> None:
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_stop_reason("max_tokens")
    assert types_of(envelopes) == ["extension.event", "run.completed"]
    assert envelopes[0].event.data == {"stopReason": "max_tokens"}


def test_prompt_error_becomes_run_failed() -> None:
    translator = make_translator()
    translator.begin_run()
    envelopes = translator.on_stop_reason  # 占位，确保下面用的是 on_error
    del envelopes
    failed = translator.on_error(-32000, "boom", {"scenario": "failure"})
    assert types_of(failed) == ["run.failed"]
    error = failed[0].event.error
    assert error.code == "-32000" and error.message == "boom"
    assert error.detail == {"data": {"scenario": "failure"}}


def test_run_terminal_closes_the_open_message() -> None:
    translator = make_translator()
    envelopes = [
        *translator.begin_run(),
        *translator.on_session_update(text_chunk("partial")),
        *translator.on_stop_reason("cancelled"),
    ]
    assert types_of(envelopes)[-2:] == ["message.completed", "run.interrupted"]


def test_second_run_gets_a_new_run_id_and_message_namespace() -> None:
    translator = make_translator()
    translator.begin_run()
    translator.on_session_update(text_chunk("a"))
    translator.on_stop_reason("end_turn")
    (started,) = translator.begin_run()
    assert started.event.run_id == f"{CONTEXT.session_id}:{CONTEXT.run_token}:r2"
    envelopes = translator.on_session_update(text_chunk("b"))
    assert envelopes[0].event.message_id == f"{CONTEXT.session_id}:{CONTEXT.run_token}:r2:m1"


# --------------------------------------------------------------------------- #
# batch53 / AD-174：run id 的唯一性来自注入的 run_token
# --------------------------------------------------------------------------- #


def _with_token(token: str) -> AcpTranslator:
    """同一条会话（同一个 ``session_id``），换一枚 ``run_token``。"""
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return EPOCH + timedelta(milliseconds=counter["n"])

    from dataclasses import replace

    return AcpTranslator(replace(CONTEXT, run_token=token), clock=clock)


def _run_ids(translator: AcpTranslator, rounds: int) -> list[str]:
    ids: list[str] = []
    for _ in range(rounds):
        (started,) = translator.begin_run()
        ids.append(started.event.run_id)
        translator.on_session_update(text_chunk("x"))
        translator.on_stop_reason("end_turn")
    return ids


def test_run_ids_of_two_attachments_to_the_same_session_never_collide() -> None:
    """MV-01 的那条尺子：**同一条会话**、两次接上，run id 一个都不许重。

    真机上的形状是这样的：后端重启，用同一个 ``native_session_id`` 续上，
    翻译器实例里的轮次计数器却从 0 重新数——于是重启后的第一轮又叫 ``…:r1``，
    和重启前那一轮同名。撞名之后 ``_run_started_at`` 取到的是**重启前**那条
    ``run.started`` 的落库时间，「这一轮是不是我在等的那一轮」也随之判错。

    这里两个翻译器共用一个 ``session_id``、各拿一枚 ``run_token``，各跑五轮：
    两个 run id 集合必须**不相交**。
    """
    first = _with_token("aaaa1111")
    second = _with_token("bbbb2222")

    before_restart = _run_ids(first, 5)
    after_restart = _run_ids(second, 5)

    assert len(set(before_restart)) == 5, "同一次接上之内也不许重"
    assert len(set(after_restart)) == 5
    assert set(before_restart).isdisjoint(set(after_restart)), (
        f"跨进程撞名了：{sorted(set(before_restart) & set(after_restart))}"
    )
    # ``:rN`` 保留着——人读日志时要的就是「这次接上之后的第 N 轮」。
    assert before_restart[0].endswith(":r1") and after_restart[0].endswith(":r1")
    assert before_restart[0] == f"{CONTEXT.session_id}:aaaa1111:r1"


def test_the_translator_still_produces_the_same_string_for_the_same_input() -> None:
    """纯度没破：唯一性是**注入**的，所以同一串输入仍然产出同一串输出。

    这条和 ``test_purity.py`` 是一对——那边守「不许认识某一家 agent」，这边守
    「不许自己去摇随机数 / 看钟」。翻译器里但凡有一个 ``uuid4()``，这条就红。
    """
    assert _run_ids(_with_token("cafe0001"), 3) == _run_ids(_with_token("cafe0001"), 3)


# --------------------------------------------------------------------------- #
# usage：只有上下文占用，没有 token
# --------------------------------------------------------------------------- #


def test_usage_update_fills_only_context_fields() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_session_update(
        update(sessionUpdate="usage_update", size=1_000_000, used=7605)
    )
    usage = envelope.event.usage
    assert usage.context_window == 1_000_000
    assert usage.context_used == 7605
    # N §7.3 规则 6：没报的字段留 None，不填 0、不估算。
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.cost_usd is None


def test_usage_update_without_numbers_falls_back_to_extension() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_session_update(update(sessionUpdate="usage_update"))
    assert envelope.event.type == "extension.event"


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def test_plan_update_maps_entries() -> None:
    translator = make_translator()
    translator.begin_run()
    (envelope,) = translator.on_session_update(
        update(
            sessionUpdate="plan",
            entries=[
                {"content": "read", "status": "completed", "priority": "high"},
                {"content": "write", "status": "in_progress"},
                {"content": "", "status": "pending"},
            ],
        )
    )
    assert envelope.event.type == "plan.updated"
    entries = envelope.event.entries
    assert [e.content for e in entries] == ["read", "write"]
    assert entries[0].status == "completed"
    assert entries[0].priority == "high"


# --------------------------------------------------------------------------- #
# 重放幂等
# --------------------------------------------------------------------------- #


def test_replay_of_the_same_stream_is_idempotent() -> None:
    translator = make_translator()
    envelopes = [
        *translator.session_created(),
        *translator.begin_run(),
        *translator.on_session_update(text_chunk("a")),
        *translator.on_session_update(
            update(sessionUpdate="tool_call", toolCallId="c1", title="t", status="pending")
        ),
        *translator.on_session_update(
            update(sessionUpdate="tool_call_update", toolCallId="c1", status="completed")
        ),
        *translator.on_stop_reason("end_turn"),
    ]
    once = reduce_events(TimelineState.initial(CONTEXT.conversation_id), envelopes)
    twice = reduce_events(once, envelopes)
    assert twice.items == once.items
    assert twice.run_state == once.run_state == "completed"
    assert twice.dropped_duplicates == len(envelopes)


@pytest.mark.parametrize("missing", ["update", "sessionUpdate"])
def test_malformed_updates_do_not_raise(missing: str) -> None:
    translator = make_translator()
    translator.begin_run()
    params = (
        {"sessionId": CONTEXT.session_id}
        if missing == "update"
        else {"sessionId": CONTEXT.session_id, "update": {"content": {}}}
    )
    assert translator.on_session_update(params) == ()
    assert translator.unknown_updates
