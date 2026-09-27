"""翻译器（规格 §3 / AD-37 / AD-38，验收点 2）。

fixture 就是 run3 的原始报文，因此这些断言不是「我们希望它这样」，而是
「实测的那一条流翻出来应该是这样」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.hermes.http_client import parse_sse_text
from drivers.hermes.translator import HermesRunTranslator, TranslationTarget
from runtime.event_reducer import (
    MessageItem,
    TimelineState,
    ToolItem,
    reduce_events,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "hermes_run_sse.txt"
RUN_ID = "run_36414bb8ee694016b045c8e2121c41f6"

TARGET = TranslationTarget(
    project_id="project:demo",
    conversation_id="conversation:demo",
    agent_binding_id="binding:demo:hermes",
    backend_id="backend:hermes",
    native_session_id="api_1788339747_a7afdfa5",
    driver_version="0.1.0",
    backend_version="0.21.0",
)

RUN_OBJECT = {
    "object": "hermes.run",
    "run_id": RUN_ID,
    "status": "completed",
    "session_id": "api_1788339747_a7afdfa5",
    "model": "hermes-agent",
    "last_event": "run.completed",
    "output": "HERMES-PROBE-TOOL-MARKER",
    "usage": {"input_tokens": 29414, "output_tokens": 95, "total_tokens": 29509},
}


def _translate(run_object=RUN_OBJECT):
    translator = HermesRunTranslator(target=TARGET, run_id=RUN_ID)
    events = parse_sse_text(FIXTURE.read_text(encoding="utf-8"))
    envelopes = list(translator.run_started())
    for event in events:
        envelopes.extend(translator.translate_sse(event))
    envelopes.extend(translator.finalize(run_object))
    return translator, tuple(envelopes)


# --------------------------------------------------------------------------- #
# 逐事件映射
# --------------------------------------------------------------------------- #


def test_probe_stream_maps_to_expected_public_events() -> None:
    _, envelopes = _translate()
    types = [e.event.type for e in envelopes]
    assert types == [
        "run.started",
        "tool.started",
        "tool.completed",
        *(["message.delta"] * 12),
        # reasoning.available 的 text 等于答案 → 判为「答案重复」，一条都不产出。
        "usage.updated",
        "message.completed",
        "run.completed",
    ]


def test_tool_started_input_is_the_preview_not_the_real_arguments() -> None:
    _, envelopes = _translate()
    started = next(e for e in envelopes if e.event.type == "tool.started")
    assert started.event.name == "terminal"
    assert started.event.input == {"preview": "echo HERMES-PROBE-TOOL-MARKER"}


def test_ad38_tool_completed_carries_no_output_and_keeps_accumulated() -> None:
    """AD-38：载荷不带工具结果 → ``output=None``，reducer 保留已累积内容。"""
    _, envelopes = _translate()
    completed = next(e for e in envelopes if e.event.type == "tool.completed")
    assert completed.event.output is None, "SSE 不带工具结果，不许编一个"
    assert completed.event.is_error is False

    # 把一段已累积的输出接在同一个 callId 上，再收终态：内容不得被清空。
    from runtime.event_envelope import ToolUpdated

    call_id = completed.event.call_id
    started = next(e for e in envelopes if e.event.type == "tool.started")
    accumulated = started.model_copy(
        update={
            "event": ToolUpdated(call_id=call_id, output="HERMES-PROBE-TOOL-MARKER"),
            "event_id": "evt-accumulated",
            "sequence": started.sequence + 1,
        }
    )
    ordered = (started, accumulated, completed.model_copy(update={"sequence": 99}))
    state = reduce_events(TimelineState.initial(TARGET.conversation_id), ordered)
    tools = [item for item in state.items if isinstance(item, ToolItem)]
    assert len(tools) == 1
    assert tools[0].output == "HERMES-PROBE-TOOL-MARKER"
    assert tools[0].status == "completed"


def test_deltas_merge_into_one_message_and_output_wins() -> None:
    _, envelopes = _translate()
    state = reduce_events(TimelineState.initial(TARGET.conversation_id), envelopes)
    messages = [item for item in state.items if isinstance(item, MessageItem)]
    assert len(messages) == 1
    assert messages[0].is_terminal
    assert messages[0].text == "HERMES-PROBE-TOOL-MARKER"
    assert state.run_state == "completed"


def test_usage_uses_measured_field_names() -> None:
    """实测字段名是 ``output_tokens``，不是文档写的 ``completion_tokens``。"""
    _, envelopes = _translate()
    usage = next(e for e in envelopes if e.event.type == "usage.updated").event.usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
        29414,
        95,
        29509,
    )
    # HTTP 路径没有 context_used 的来源 → 必须是 None，不得伪造（N §7.3 规则 6）。
    assert usage.context_used is None
    assert usage.cost_usd is None


def test_occurred_at_comes_from_payload_timestamp_not_local_clock() -> None:
    _, envelopes = _translate()
    first_delta = next(e for e in envelopes if e.event.type == "message.delta")
    assert first_delta.occurred_at.timestamp() == pytest.approx(1788339750.877326)


# --------------------------------------------------------------------------- #
# AD-37：ID 合成的确定性
# --------------------------------------------------------------------------- #


def test_ad37_id_shapes() -> None:
    _, envelopes = _translate()
    message_ids = {e.event.message_id for e in envelopes if e.event.type == "message.delta"}
    call_ids = {e.event.call_id for e in envelopes if e.event.type.startswith("tool.")}
    assert message_ids == {f"{RUN_ID}:m1"}
    assert call_ids == {f"{RUN_ID}:t1"}
    assert [e.native_event_id for e in envelopes][:3] == [
        f"{RUN_ID}#000000",
        f"{RUN_ID}#000001",
        f"{RUN_ID}#000002",
    ]


def test_ad37_translation_is_deterministic_across_replays() -> None:
    """同一条流翻两次：eventId / messageId / callId **完全相同**。

    这是重连去重的前提：reducer 靠 ``eventId`` 判「已见过就整条丢弃」，
    合成规则一旦掺进时钟或随机数，重连后 delta 就会落到新卡上。
    """
    _, first = _translate()
    _, second = _translate()
    assert [e.event_id for e in first] == [e.event_id for e in second]
    assert [e.native_event_id for e in first] == [e.native_event_id for e in second]
    assert [e.event.model_dump() for e in first] == [e.event.model_dump() for e in second]


def test_event_ids_differ_across_bindings() -> None:
    """eventId 必须**全局**唯一：换一条 Binding 就得换命名空间。"""
    _, first = _translate()
    other = HermesRunTranslator(
        target=TranslationTarget(
            project_id=TARGET.project_id,
            conversation_id="conversation:other",
            agent_binding_id="binding:other:hermes",
            backend_id=TARGET.backend_id,
        ),
        run_id=RUN_ID,
    )
    envelopes = other.run_started()
    assert envelopes[0].event_id != first[0].event_id


def test_replaying_the_same_envelopes_is_idempotent_in_the_reducer() -> None:
    _, envelopes = _translate()
    once = reduce_events(TimelineState.initial(TARGET.conversation_id), envelopes)
    twice = reduce_events(once, envelopes)
    assert twice.items == once.items
    assert twice.dropped_duplicates == len(envelopes)


# --------------------------------------------------------------------------- #
# 幂等回放与断线重连（规格 §2.7 / §3.6）
# --------------------------------------------------------------------------- #


def test_replayed_run_does_not_create_a_second_card() -> None:
    """``replayed=true`` → 不重复建卡（规格 §2.7）。"""
    translator = HermesRunTranslator(target=TARGET, run_id=RUN_ID)
    assert translator.run_started(replayed=True) == ()
    assert translator.run_started(replayed=False)[0].event.type == "run.started"


def test_reconnect_replay_produces_identical_ids_for_the_same_frames() -> None:
    """重连后服务端从头重放（规格 §3.6 分支 a）：靠确定性 eventId 幂等去重。"""
    events = parse_sse_text(FIXTURE.read_text(encoding="utf-8"))
    first = HermesRunTranslator(target=TARGET, run_id=RUN_ID)
    first_out = [env for e in events[:5] for env in first.translate_sse(e)]
    # 断线 → 重连 → 服务端从头重放同样的帧：新翻译器从同一起点开始。
    second = HermesRunTranslator(target=TARGET, run_id=RUN_ID)
    second_out = [env for e in events for env in second.translate_sse(e)]
    assert [e.event_id for e in second_out[: len(first_out)]] == [
        e.event_id for e in first_out
    ]
    state = reduce_events(TimelineState.initial(TARGET.conversation_id), (*first_out, *second_out))
    messages = [item for item in state.items if isinstance(item, MessageItem)]
    assert len(messages) == 1, "重放不得把同一段正文写成两条消息"
    assert state.dropped_duplicates == len(first_out)


def test_reconnect_budget_stays_under_the_five_minute_buffer() -> None:
    """规格 §3.6：退避 0.5→1→2→4，总预算 240s < 300s 的事件缓冲期。"""
    from drivers.hermes.driver import MAX_CONCURRENT_RUNS  # noqa: F401 - 同模块常量校验
    from drivers.hermes import reconnect

    assert reconnect.RECONNECT_BUDGET_SECONDS == 240
    assert reconnect.RECONNECT_BUDGET_SECONDS < reconnect.EVENT_BUFFER_SECONDS
    assert reconnect.BACKOFF_SCHEDULE == (0.5, 1.0, 2.0, 4.0)
    total = 0.0
    delays = []
    while total < reconnect.RECONNECT_BUDGET_SECONDS:
        delay = reconnect.backoff_for(len(delays))
        delays.append(delay)
        total += delay
    assert reconnect.exhausted(total) is True


# --------------------------------------------------------------------------- #
# reasoning.available 的三分支（规格 §3.2 / §8-③）
# --------------------------------------------------------------------------- #


def _fresh() -> HermesRunTranslator:
    return HermesRunTranslator(target=TARGET, run_id=RUN_ID)


def test_reasoning_branch_answer_duplicate_is_dropped() -> None:
    """分支 1：``text`` 与已收到的 delta 拼接结果重合 → 丢弃。

    run3 的实测样本正是这一支：``text`` 恰好等于最终答案，无法区分「思考摘要」
    与「答案重复」，所以宁可不显示，也不要让思考区和答案区显示同一段话。
    """
    translator = _fresh()
    for chunk in ("\n\nHER", "MES", "-OK"):
        translator.translate_payload(
            {"event": "message.delta", "run_id": RUN_ID, "delta": chunk}
        )
    out = translator.translate_payload(
        {"event": "reasoning.available", "run_id": RUN_ID, "text": "HERMES-OK"}
    )
    assert out == ()


def test_reasoning_branch_incremental_becomes_reasoning_delta() -> None:
    """分支 2：后一条以前一条为前缀 → 增量流 → ``reasoning.delta``（AD-27）。"""
    translator = _fresh()
    first = translator.translate_payload(
        {"event": "reasoning.available", "run_id": RUN_ID, "text": "先看退出码"}
    )
    assert first[0].event.type == "reasoning.status"
    second = translator.translate_payload(
        {"event": "reasoning.available", "run_id": RUN_ID, "text": "先看退出码，再回答"}
    )
    assert second[0].event.type == "reasoning.delta"
    assert second[0].event.text == "，再回答"
    assert second[0].event.message_id == f"{RUN_ID}:m1"


def test_reasoning_branch_standalone_summary_becomes_status() -> None:
    """分支 3：与答案无关、也不是增量 → ``reasoning.status``。"""
    translator = _fresh()
    translator.translate_payload(
        {"event": "message.delta", "run_id": RUN_ID, "delta": "42"}
    )
    out = translator.translate_payload(
        {"event": "reasoning.available", "run_id": RUN_ID, "text": "先在心里推理三步"}
    )
    assert out[0].event.type == "reasoning.status"
    assert out[0].event.status == "available"
    assert out[0].event.summary == "先在心里推理三步"


# --------------------------------------------------------------------------- #
# 降级与防御
# --------------------------------------------------------------------------- #


def test_unknown_event_goes_to_extension_not_a_new_public_type() -> None:
    translator = _fresh()
    out = translator.translate_payload(
        {"event": "subagent.start", "run_id": RUN_ID, "child_session_id": "api_x"}
    )
    assert out[0].event.type == "extension.event"
    assert out[0].event.namespace == "hermes"
    assert out[0].event.name == "subagent.start"
    assert out[0].event.data["child_session_id"] == "api_x"


def test_malformed_frames_yield_at_most_one_notice_per_run() -> None:
    from drivers.hermes.http_client import parse_sse_text as parse

    translator = _fresh()
    frames = parse("data: [DONE]\n\ndata: not json either\n\n")
    out = [env for frame in frames for env in translator.translate_sse(frame)]
    assert len(out) == 1
    assert out[0].event.type == "diagnostic.notice"
    assert out[0].event.level == "warn"


def test_foreign_run_id_is_discarded_with_one_notice() -> None:
    translator = _fresh()
    first = translator.translate_payload(
        {"event": "message.delta", "run_id": "run_other", "delta": "x"}
    )
    assert first[0].event.type == "diagnostic.notice"
    second = translator.translate_payload(
        {"event": "message.delta", "run_id": "run_other", "delta": "y"}
    )
    assert second == ()


def test_concurrent_same_tool_calls_warn_about_pairing() -> None:
    """规格 §3.3 的已知局限：同名工具并发时 FIFO 配对可能配错，必须如实提示。"""
    translator = _fresh()
    translator.translate_payload({"event": "tool.started", "run_id": RUN_ID, "tool": "terminal"})
    translator.translate_payload({"event": "tool.started", "run_id": RUN_ID, "tool": "terminal"})
    out = translator.translate_payload(
        {"event": "tool.completed", "run_id": RUN_ID, "tool": "terminal", "error": False}
    )
    assert out[0].event.type == "diagnostic.notice"
    assert out[1].event.call_id == f"{RUN_ID}:t1", "FIFO：配最早一个未完成的调用"


def test_tool_completed_without_started_still_gets_a_deterministic_call_id() -> None:
    translator = _fresh()
    out = translator.translate_payload(
        {"event": "tool.completed", "run_id": RUN_ID, "tool": "terminal", "error": True}
    )
    assert out[0].event.call_id == f"{RUN_ID}:t1"
    assert out[0].event.is_error is True


def test_output_mismatch_emits_a_notice_and_prefers_backend_text() -> None:
    translator = _fresh()
    translator.translate_payload({"event": "message.delta", "run_id": RUN_ID, "delta": "part"})
    out = translator.finalize({"status": "completed", "output": "the whole answer"})
    types = [e.event.type for e in out]
    assert "diagnostic.notice" in types
    completed = next(e for e in out if e.event.type == "message.completed")
    assert completed.event.text == "the whole answer"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", "run.completed"),
        ("failed", "run.failed"),
        ("cancelled", "run.interrupted"),
    ],
)
def test_run_terminal_status_mapping(status: str, expected: str) -> None:
    translator = _fresh()
    out = translator.finalize({"status": status})
    assert out[-1].event.type == expected


@pytest.mark.parametrize("status", ["queued", "started", "running", "stopping"])
def test_active_status_does_not_finalize_text_or_run(status: str) -> None:
    translator = _fresh()
    translator.translate_payload({"event": "message.delta", "run_id": RUN_ID, "delta": "part"})
    assert translator.finalize({"status": status, "output": "part"}) == ()
    translator.translate_payload({"event": "message.delta", "run_id": RUN_ID, "delta": " two"})
    final = translator.finalize({"status": "completed", "output": "part two"})
    assert final[-1].event.type == "run.completed"
    assert next(e for e in final if e.event.type == "message.completed").event.text == "part two"


def test_stop_timeout_only_warns_and_late_result_can_still_finalize() -> None:
    translator = _fresh()
    pending = translator.stop_unconfirmed(None, last_status="running")
    assert [e.event.type for e in pending] == ["diagnostic.notice", "extension.event"]
    translator.translate_payload({"event": "message.delta", "run_id": RUN_ID, "delta": "late"})
    final = translator.finalize({"status": "cancelled", "output": "late"})
    assert final[-1].event.type == "run.interrupted"
    assert final[-1].event.reason == "stopped"
    assert next(e for e in final if e.event.type == "message.completed").event.text == "late"


def test_unknown_status_does_not_invent_success() -> None:
    translator = _fresh()
    assert [e.event.type for e in translator.finalize({"status": "new-state"})] == ["diagnostic.notice"]
    assert translator.finalize({"status": "failed", "error": "later"})[-1].event.type == "run.failed"


def test_backend_restart_is_reported_as_retriable() -> None:
    """规格 §1.5：在途 run 不可恢复 → ``backend.restarted``，可重试。"""
    translator = _fresh()
    out = translator.backend_restarted()
    assert out[0].event.type == "run.failed"
    assert out[0].event.error.code == "backend.restarted"
    assert out[0].event.error.retriable is True
