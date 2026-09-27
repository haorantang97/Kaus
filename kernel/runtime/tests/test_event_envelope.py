"""AgentEventEnvelope v1.1 测试（N §7.1 / §7.2 / §7.3）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from runtime.event_envelope import (
    AGENT_EVENT_TYPES,
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    AuthenticationResolved,
    EventSource,
    ExtensionEvent,
    MessageDelta,
    RunFailed,
    SCHEMA_VERSION,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
    make_envelope,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
SOURCE = EventSource(driver_kind="mock", driver_version="0.1.0", backend_version="9.9")


def envelope(event: AgentEvent, sequence: int = 0) -> AgentEventEnvelope:
    return make_envelope(
        event=event,
        project_id="project:pronto",
        conversation_id="conversation:11111111-1111-4111-8111-111111111111",
        agent_binding_id="binding:pronto:acme",
        backend_id="backend:acme",
        sequence=sequence,
        source=SOURCE,
        occurred_at=NOW,
        event_id=f"evt-{sequence}",
    )


#: N §7.2 逐行抄下来的公共事件类型 + AD-08 与 AD-27 补入的两条，用作独立的对照表。
N_7_2_EVENT_TYPES = (
    # Session / runtime lifecycle
    "session.created",
    "session.resumed",
    "session.state",
    "run.started",
    "run.completed",
    "run.interrupted",
    "run.failed",
    # User-visible content
    "message.started",
    "message.delta",
    "message.completed",
    # AD-27：流式思考的增量文本补入公共 union（v1.1 冻结前的最后一项）。
    "reasoning.delta",
    "reasoning.status",
    "plan.updated",
    # Actions, tools and artifacts
    "tool.started",
    "tool.updated",
    "tool.completed",
    "terminal.started",
    "terminal.updated",
    "terminal.completed",
    "file.changed",
    "artifact.created",
    # Human-in-the-loop
    "permission.requested",
    "permission.resolved",
    "question.requested",
    "question.resolved",
    "authentication.requested",
    # AD-08：认证闭环补入公共 union（v1.1 冻结前的最后一项）。
    "authentication.resolved",
    # Usage and diagnostics
    "usage.updated",
    "diagnostic.notice",
    # Graceful extension path
    "extension.event",
)


def test_union_covers_exactly_n_7_2() -> None:
    """N §7.2 的事件分类 + AD-08 的补全，必须一条不多、一条不少地建模。"""
    assert AGENT_EVENT_TYPES == N_7_2_EVENT_TYPES
    # AD-27 之后是 30 条：AD-08 的 authentication.resolved + AD-27 的 reasoning.delta。
    assert len(AGENT_EVENT_TYPES) == 30
    assert "run.spawned" not in AGENT_EVENT_TYPES, (
        "AD-08：委派用信封头 parentRunId 表达，run.spawned 不进公共 union"
    )


def test_ad08_closes_all_three_interaction_kinds() -> None:
    """N §7.3 规则 4 并列的三种交互请求，AD-08 之后各自都有 resolved 事件。"""
    for kind in ("permission", "question", "authentication"):
        assert f"{kind}.requested" in AGENT_EVENT_TYPES
        assert f"{kind}.resolved" in AGENT_EVENT_TYPES, (
            f"{kind} 缺少闭环事件，卡片会永远停在 pending"
        )


def test_authentication_resolved_outcome_is_a_closed_set() -> None:
    """AD-08：outcome 是封闭集合，Driver 必须把原生结果映射进来。"""
    resolved = AuthenticationResolved(request_id="req-1", outcome="authenticated")
    assert resolved.model_dump(by_alias=True) == {
        "type": "authentication.resolved",
        "requestId": "req-1",
        "outcome": "authenticated",
    }
    for outcome in ("authenticated", "declined", "cancelled", "failed"):
        assert AuthenticationResolved(request_id="r", outcome=outcome).outcome == outcome
    with pytest.raises(ValidationError):
        AuthenticationResolved(request_id="r", outcome="whatever-the-backend-said")  # type: ignore[arg-type]


def test_every_declared_type_is_parseable_by_the_union() -> None:
    """union 的 discriminator 必须认得 N §7.2 的每一个 type。"""
    adapter = TypeAdapter(AgentEvent)
    for event_type in AGENT_EVENT_TYPES:
        with pytest.raises(ValidationError) as excinfo:
            adapter.validate_python({"type": event_type, "__probe__": True})
        errors = excinfo.value.errors()
        # 能走到「字段校验」说明 discriminator 已命中该分支；
        # 若 type 不在 union 里，报的会是 union_tag_invalid。
        assert all(e["type"] != "union_tag_invalid" for e in errors), event_type

    with pytest.raises(ValidationError) as excinfo:
        adapter.validate_python({"type": "not.a.public.event"})
    assert any(e["type"] == "union_tag_invalid" for e in excinfo.value.errors())


def test_envelope_wire_shape_is_camel_case() -> None:
    """N §7.1 的 TypeScript 接口是驼峰，序列化必须逐字段对齐。"""
    dumped = envelope(ToolUpdated(call_id="call-1", progress={"pct": 10})).model_dump(
        by_alias=True
    )
    assert dumped["schemaVersion"] == SCHEMA_VERSION == "1.1"
    for key in (
        "eventId",
        "projectId",
        "conversationId",
        "agentBindingId",
        "backendId",
        "nativeSessionId",
        "runId",
        "parentRunId",
        "nativeEventId",
        "sequence",
        "occurredAt",
        "source",
        "event",
    ):
        assert key in dumped, f"Envelope 缺字段 {key}（N §7.1）"
    assert dumped["source"]["driverKind"] == "mock"
    assert dumped["event"] == {
        "type": "tool.updated",
        "callId": "call-1",
        "output": None,
        "progress": {"pct": 10},
        # AD-27：与 terminal.updated 对称的增量/全量开关，默认增量。
        "cumulative": False,
    }


def test_envelope_roundtrip_via_json() -> None:
    original = envelope(MessageDelta(message_id="m1", text="hi"), sequence=7)
    payload = original.model_dump_json(by_alias=True)
    restored = AgentEventEnvelope.model_validate_json(payload)
    assert restored == original
    assert restored.event_type == "message.delta"


def test_discriminated_union_parses_by_type() -> None:
    adapter = TypeAdapter(AgentEvent)
    parsed = adapter.validate_python({"type": "tool.completed", "callId": "c1", "output": 1, "isError": True})
    assert parsed.type == "tool.completed" and parsed.is_error is True


def test_public_event_layer_forbids_private_fields() -> None:
    """N §3 / §7.3 规则 7：私有字段不得挂上公共事件，只能走 extension.event。"""
    with pytest.raises(ValidationError):
        MessageDelta(message_id="m1", text="hi", native_payload={"x": 1})  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentEventEnvelope.model_validate(
            {
                **envelope(MessageDelta(message_id="m1", text="hi")).model_dump(by_alias=True),
                "backendPrivateField": 1,
            }
        )
    # 合法出口：
    extension = ExtensionEvent(namespace="acme", name="native.thing", data={"raw": 1})
    assert envelope(extension).event.namespace == "acme"


def test_usage_snapshot_never_fakes_missing_fields() -> None:
    """N §7.3 规则 6：公共层不得伪造缺失字段——未报告即 None，不是 0。"""
    usage = UsageSnapshot(input_tokens=10)
    assert usage.input_tokens == 10
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.cost_usd is None
    dumped = UsageUpdated(usage=usage).model_dump(by_alias=True)["usage"]
    assert dumped["outputTokens"] is None


def test_sequence_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        envelope(MessageDelta(message_id="m", text="x"), sequence=-1)


def test_run_failed_carries_structured_error() -> None:
    failure = RunFailed(run_id="run-1", error=AgentError(code="boom", message="炸了"))
    assert failure.error.code == "boom"
    with pytest.raises(ValidationError):
        RunFailed(run_id="run-1")  # type: ignore[call-arg]


def test_events_are_immutable() -> None:
    event = MessageDelta(message_id="m1", text="hi")
    with pytest.raises(ValidationError):
        event.text = "changed"  # type: ignore[misc]


def test_parent_run_id_expresses_delegation() -> None:
    """AD-08：信封头可选 parentRunId，表达委派 / 子 run。"""
    child = make_envelope(
        event=MessageDelta(message_id="m1", text="子任务输出"),
        project_id="project:pronto",
        conversation_id="conversation:11111111-1111-4111-8111-111111111111",
        agent_binding_id="binding:pronto:acme",
        backend_id="backend:acme",
        sequence=3,
        source=SOURCE,
        occurred_at=NOW,
        event_id="evt-child",
        run_id="run-child",
        parent_run_id="run-parent",
    )
    assert child.is_child_run is True
    assert child.model_dump(by_alias=True)["parentRunId"] == "run-parent"

    top_level = envelope(MessageDelta(message_id="m1", text="hi"))
    assert top_level.is_child_run is False
    assert top_level.parent_run_id is None
    # 默认不出现在 wire 上的值仍是 None，而不是被伪造成某个 run id。
    assert top_level.model_dump(by_alias=True)["parentRunId"] is None


def test_parent_run_id_requires_a_self_identity_and_rejects_self_parenting() -> None:
    """子 run 必须自报 runId，且不能是自己的父 run（AD-08）。"""
    base = envelope(MessageDelta(message_id="m1", text="hi")).model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        AgentEventEnvelope.model_validate({**base, "parentRunId": "run-parent"})
    with pytest.raises(ValidationError):
        AgentEventEnvelope.model_validate(
            {**base, "runId": "run-1", "parentRunId": "run-1"}
        )
    assert AgentEventEnvelope.model_validate(
        {**base, "runId": "run-child", "parentRunId": "run-parent"}
    ).is_child_run


def test_stable_ids_present_on_incremental_events() -> None:
    """N §7.3 规则 1：可增量更新的对象必须有稳定 ID。"""
    assert "message_id" in MessageDelta.model_fields
    assert "call_id" in ToolUpdated.model_fields
    from runtime.event_envelope import (
        PermissionResolved,
        QuestionResolved,
        TerminalUpdated,
    )

    assert "terminal_id" in TerminalUpdated.model_fields
    assert "request_id" in PermissionResolved.model_fields
    assert "request_id" in QuestionResolved.model_fields
