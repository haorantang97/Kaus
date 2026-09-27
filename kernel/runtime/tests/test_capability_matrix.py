"""Capability Matrix 测试（N §13.1 / §8.2；v1.0 §12.3；批次六枚举轴）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from runtime.capability_matrix import (
    ENUM_AXES,
    FEATURE_PATHS,
    BackendCapabilities,
    CapabilityMatrix,
    CapabilityState,
    CardCapabilities,
    ExternalCliCapabilities,
    InterruptCapability,
    ModelCapabilities,
    PermissionCapability,
    ResumeCapability,
    SessionCapabilities,
    SessionListCapability,
    SupportLevel,
    ToolCardCapabilities,
    capabilities_to_wire,
    capability_state_at,
    capability_state_to_ui,
    capability_state_to_wire,
    declare,
    evaluate_support,
    unknown_count,
    unknown_feature_paths,
    with_verification,
)

LIST_NOTE = "只看得见当前进程内的会话：可用来核对，不可用来发现。"

RICH = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(
        list="all", create=True, resume="warm", history=True, branch=True
    ),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=True),
        permissions="protocol",
        interrupt="immediate",
    ),
    external_cli=ExternalCliCapabilities(supported=True, resume=True),
    models=ModelCapabilities(mode="open", reasoning=True, providers=True),
    capability_projection={
        "skills": SupportLevel.NATIVE,
        "mcp": SupportLevel.ADAPTED,
        "memory": SupportLevel.PARTIAL,
        "plugins": SupportLevel.UNSUPPORTED,
    },
)

LEAN = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(create=True, list="none", resume="none"),
    card=CardCapabilities(streaming=True, permissions="none", interrupt="none"),
    external_cli=ExternalCliCapabilities(supported=False),
    models=ModelCapabilities(mode="fixed"),
)


def test_defaults_are_unknown_not_a_silent_no() -> None:
    """未声明就是 ``unknown``——既不假定支持，也不冒充「确认不支持」。"""
    empty = BackendCapabilities()
    assert empty.structured_events.is_unknown
    assert empty.sessions.resume.is_unknown
    assert empty.card.permissions.is_unknown
    assert empty.card.tools.output.is_unknown
    assert empty.external_cli.supported.is_unknown
    # 保守缺省：unknown 是假值，既有的布尔守卫不会被误导成「有」。
    assert bool(empty.card.permissions) is False
    assert empty.models.mode == "fixed"
    assert empty.support_for("skills") is SupportLevel.UNKNOWN
    assert unknown_count(empty) == len(FEATURE_PATHS)


def test_unknown_is_strictly_distinct_from_unsupported() -> None:
    """第 2 条纪律：「没问过」与「问过、没有」不是同一件事。"""
    unknown = CapabilityState()
    unsupported = CapabilityState(value="unsupported")
    assert unknown.status == "unknown" and unsupported.status == "unsupported"
    assert unknown != unsupported
    assert bool(unknown) is False and bool(unsupported) is False
    assert unknown.is_unsupported is False, "unknown 不得被算成「确认不支持」"


def test_evaluate_support_distinguishes_unknown_from_unsupported() -> None:
    verdicts = {
        v.capability_type: v
        for v in evaluate_support(
            ["skills", "mcp", "memory", "plugins", "never-declared"], RICH
        )
    }
    assert verdicts["skills"].projectable and verdicts["skills"].reason is None
    assert verdicts["mcp"].projectable
    assert verdicts["memory"].projectable and "部分支持" in (verdicts["memory"].reason or "")
    assert not verdicts["plugins"].projectable
    assert "明确不支持" in (verdicts["plugins"].reason or "")
    assert not verdicts["never-declared"].projectable
    assert "未声明" in (verdicts["never-declared"].reason or "")
    # 两者都不可投射，但原因不同——UI 才能区分「还没探测」与「确认不支持」。
    assert verdicts["plugins"].level is not verdicts["never-declared"].level


def test_matrix_supports_by_dotted_path() -> None:
    matrix = CapabilityMatrix(backends={"backend:a": RICH, "backend:b": LEAN})
    assert matrix.backend_keys() == ("backend:a", "backend:b")
    assert matrix.supports("backend:a", "card.permissions") is True
    assert matrix.supports("backend:b", "card.permissions") is False
    assert matrix.supports("backend:b", "sessions.create") is True
    assert matrix.supports("backend:unknown", "card.streaming") is False
    assert matrix.supports("backend:a", "card.nonexistent") is False
    assert matrix.supports("backend:a", "models.mode") is False, "非能力字段不参与能力协商"
    # 子项各自成题（AD-68/AD-71）。
    assert matrix.supports("backend:a", "card.tools.output") is True
    assert matrix.supports("backend:b", "card.tools.output") is False


def test_matrix_describe_across_backends() -> None:
    matrix = CapabilityMatrix(backends={"backend:a": RICH, "backend:b": LEAN})
    assert matrix.describe("card.streaming") == {"backend:a": True, "backend:b": True}
    assert matrix.describe("card.interrupt") == {"backend:a": True, "backend:b": False}


def test_matrix_is_immutable_with_backend_returns_new() -> None:
    original = CapabilityMatrix()
    updated = original.with_backend("backend:a", RICH)
    assert original.backends == {}
    assert updated.capabilities_of("backend:a") == RICH


# --------------------------------------------------------------------------- #
# 第 1 条：按题给选项（枚举轴）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "axis"),
    [
        (SessionListCapability, "sessions.list"),
        (ResumeCapability, "sessions.resume"),
        (PermissionCapability, "card.permissions"),
        (InterruptCapability, "card.interrupt"),
    ],
)
def test_enum_axis_values_are_published_for_the_ui(model, axis) -> None:
    """UI 按值渲染，所以取值集合必须是可枚举的公共数据。"""
    assert ENUM_AXES[axis] == model.ALLOWED_VALUES
    assert "unknown" in model.ALLOWED_VALUES


def test_enum_axis_rejects_a_value_from_another_axis() -> None:
    """``warm`` 是续接轴上的档位，写到中断轴上必须当场报错。"""
    with pytest.raises(ValidationError):
        CardCapabilities(interrupt="warm")
    with pytest.raises(ValidationError):
        SessionCapabilities(resume="own_process")
    # 笼统的 "supported" 在枚举轴上不是取值，只是旧写法 → 规范化成 unknown。
    assert CardCapabilities(permissions="supported").permissions.is_unknown


def test_enum_values_map_to_a_coarse_status() -> None:
    resume = ResumeCapability(value="cold")
    assert resume.status == "supported" and bool(resume) is True
    assert ResumeCapability(value="none").status == "unsupported"
    assert ResumeCapability().status == "unknown"
    # mirror/unverified 都算「有这项能力」，形态不同而已。
    assert PermissionCapability(value="mirror").is_supported
    assert PermissionCapability(value="unverified").is_supported
    assert InterruptCapability(value="tool_boundary").is_supported


def test_tool_card_is_two_questions_not_one_and_a_half() -> None:
    """AD-68/AD-71：工具卡拆成 calls / output，UI 不必读备注就知道要不要画输出栏。"""
    tools = ToolCardCapabilities(calls=True, output=False)
    assert bool(tools.calls) is True and bool(tools.output) is False
    assert "card.tools.calls" in FEATURE_PATHS and "card.tools.output" in FEATURE_PATHS
    assert "card.tools" not in FEATURE_PATHS


# --------------------------------------------------------------------------- #
# 第 3 条：声明 vs 实测（verification）
# --------------------------------------------------------------------------- #


def test_verification_defaults_to_declared_and_can_be_written_back() -> None:
    capabilities = BackendCapabilities(
        sessions=SessionCapabilities(create=True),
        card=CardCapabilities(permissions=declare("protocol", verification="bench")),
    )
    assert capabilities.sessions.create.verification == "declared"
    assert capabilities.card.permissions.verification == "bench"

    benched = with_verification(capabilities, {"sessions.create": "bench"})
    assert benched.sessions.create.verification == "bench"
    assert benched.sessions.create.value == "supported", "写回取证等级不得改动取值"
    # 原对象不变（模型不可变）。
    assert capabilities.sessions.create.verification == "declared"


def test_with_verification_ignores_paths_that_are_not_capability_axes() -> None:
    capabilities = BackendCapabilities(sessions=SessionCapabilities(create=True))
    assert with_verification(capabilities, {"models.mode": "live"}) == capabilities
    assert with_verification(capabilities, {"card.nope": "live"}) == capabilities


def test_ad60_is_expressed_as_protocol_plus_bench_not_as_a_half_value() -> None:
    """AD-60「契约测试通过、现场未验」= 取值 protocol + 取证等级 bench。"""
    state = PermissionCapability(value="protocol", verification="bench")
    assert state.is_supported, "链路是有的——不该在取值上打折"
    assert state.verification == "bench", "没跑过真机这件事，由取证等级说"


# --------------------------------------------------------------------------- #
# 兼容：旧布尔 / 旧三态 JSON
# --------------------------------------------------------------------------- #


def test_booleans_are_still_accepted_and_normalized() -> None:
    sessions = SessionCapabilities(create=True, history=False)
    assert sessions.create.value == "supported" and sessions.create.note is None
    assert sessions.history.value == "unsupported"
    revived = BackendCapabilities.model_validate_json(
        '{"structuredEvents": true, "sessions": {"create": true, "history": false}}'
    )
    assert revived.structured_events.is_supported
    assert revived.sessions.create.is_supported
    assert revived.sessions.history.is_unsupported


def test_legacy_boolean_on_an_enum_axis_falls_back_to_unknown(caplog) -> None:
    """旧布尔说不出是 warm 还是 cold，只能落 unknown——并且要留下日志。"""
    with caplog.at_level("WARNING"):
        sessions = SessionCapabilities(resume=True, list=False)
    assert sessions.resume.is_unknown
    assert sessions.list.value == "none", "旧的 False 在枚举轴上落否定值"
    assert any("unknown" in record.message for record in caplog.records)


def test_legacy_partial_normalizes_to_unknown_with_a_log(caplog) -> None:
    """AD-71：``partial`` 整体退场；旧数据规范化为 unknown 并打日志。"""
    with caplog.at_level("WARNING"):
        revived = BackendCapabilities.model_validate_json(
            '{"sessions": {"list": {"status": "partial", "note": "n", "supportedBool": true}},'
            ' "card": {"reasoning": "partial"}}'
        )
    assert revived.sessions.list.is_unknown
    assert revived.sessions.list.note == "n", "note 留在声明上，只是不再参与 UI 判断"
    assert revived.card.reasoning.is_unknown
    assert any("partial" in record.message for record in caplog.records)


def test_legacy_flat_tool_card_migrates_to_two_children(caplog) -> None:
    with caplog.at_level("WARNING"):
        card = CardCapabilities.model_validate({"tools": True})
    assert card.tools.calls.is_supported
    assert card.tools.output.is_unknown, "旧声明说不出有没有输出，装作有就是骗人"
    assert any("calls" in record.message for record in caplog.records)


def test_round_trip_through_json_preserves_value_note_and_verification() -> None:
    revived = BackendCapabilities.model_validate_json(RICH.model_dump_json(by_alias=True))
    assert revived == RICH
    with_notes = BackendCapabilities(
        sessions=SessionCapabilities(
            list=declare("own_process", verification="live", note=LIST_NOTE)
        )
    )
    revived = BackendCapabilities.model_validate_json(
        with_notes.model_dump_json(by_alias=True)
    )
    assert revived.sessions.list.note == LIST_NOTE
    assert revived.sessions.list.verification == "live"


def test_wire_shape_can_be_read_back() -> None:
    """API 输出的 detail 层带派生量，读回来时必须被丢掉而不是报错。"""
    detail = capabilities_to_wire(RICH)["detail"]
    assert BackendCapabilities.model_validate(detail) == RICH


# --------------------------------------------------------------------------- #
# 路径寻址与 unknown 计数
# --------------------------------------------------------------------------- #


def test_capability_state_at_is_conservative_off_the_happy_path() -> None:
    assert capability_state_at(RICH, "sessions.list").value == "all"
    assert capability_state_at(RICH, "card.nonexistent").is_unknown
    # 非能力字段（枚举）问不出能力 → unknown，而不是「确认不支持」。
    assert capability_state_at(RICH, "models.mode").is_unknown
    assert capability_state_at(RICH, "sessions.list.value").is_unknown


def test_every_feature_path_resolves_to_a_capability_state() -> None:
    for path in FEATURE_PATHS:
        assert isinstance(capability_state_at(RICH, path), CapabilityState), path
    assert capability_state_at(RICH, "structured_events").is_supported


def test_unknown_paths_are_listed_and_counted() -> None:
    capabilities = BackendCapabilities(
        structured_events=True,
        sessions=SessionCapabilities(create=True),
    )
    unknown = unknown_feature_paths(capabilities)
    assert "structured_events" not in unknown and "sessions.create" not in unknown
    assert "card.tools.output" in unknown
    assert unknown_count(capabilities) == len(unknown) == len(FEATURE_PATHS) - 2


def test_matrix_state_of_and_describe_status() -> None:
    matrix = CapabilityMatrix(backends={"backend:a": RICH, "backend:b": LEAN})
    assert matrix.state_of("backend:a", "sessions.list").value == "all"
    assert matrix.state_of("backend:b", "sessions.list").value == "none"
    assert matrix.state_of("backend:unknown", "sessions.list").is_unknown
    assert matrix.status_of("backend:a", "sessions.resume").value == "warm"
    statuses = matrix.describe_status("sessions.list")
    assert {k: v.value for k, v in statuses.items()} == {
        "backend:a": "all",
        "backend:b": "none",
    }


# --------------------------------------------------------------------------- #
# AD-71：降级只有两条分支，wire 分两层
# --------------------------------------------------------------------------- #


def test_degradation_hides_the_control_and_carries_no_note() -> None:
    """AD-71：不支持/未知一律静默隐藏；没有第三条「显示 + 备注」的分支。"""
    matrix = CapabilityMatrix(backends={"backend:a": RICH, "backend:b": LEAN})

    available = matrix.degradation_for("backend:a", "card.interrupt")
    assert available.available and not available.hide_control
    assert available.value == "immediate", "UI 按值挑形态"
    assert not hasattr(available, "message"), "AD-71：降级指令里不带任何备注"

    missing = matrix.degradation_for("backend:b", "card.interrupt")
    assert missing.available is False and missing.hide_control is True
    assert missing.value == "none" and missing.status == "unsupported"
    assert missing.open_in_cli is False, "该 Backend 未声明站外 CLI，就不该提示 Open in CLI"

    unknown = matrix.degradation_for("backend:b", "card.artifacts")
    assert unknown.hide_control is True and unknown.status == "unknown"

    cli_capable = CapabilityMatrix(
        backends={
            "backend:d": BackendCapabilities(
                external_cli=ExternalCliCapabilities(supported=True)
            )
        }
    )
    assert cli_capable.degradation_for("backend:d", "card.tools.calls").open_in_cli


def test_wire_is_two_layers_and_ui_never_sees_a_note() -> None:
    capabilities = BackendCapabilities(
        sessions=SessionCapabilities(
            list=declare("own_process", verification="live", note=LIST_NOTE),
            create=True,
        ),
        card=CardCapabilities(tools=ToolCardCapabilities(calls=True, output=False)),
        models=ModelCapabilities(mode="fixed"),
    )
    wire = capabilities_to_wire(capabilities)

    # ui 层：只有取值，一个 note 都没有。
    assert wire["ui"]["sessions"]["list"] == "own_process"
    assert wire["ui"]["sessions"]["create"] == "supported"
    assert wire["ui"]["card"]["tools"] == {"calls": "supported", "output": "unsupported"}
    assert LIST_NOTE not in repr(wire["ui"])

    # detail 层：取值 + 状态 + 取证等级 + note。
    detail = wire["detail"]["sessions"]["list"]
    assert detail == {
        "value": "own_process",
        "status": "supported",
        "verification": "live",
        "note": LIST_NOTE,
        "supportedBool": True,
    }
    assert wire["detail"]["sessions"]["history"] == {
        "value": "unknown",
        "status": "unknown",
        "verification": "declared",
        "supportedBool": False,
    }
    assert wire["unknownCount"] == unknown_count(capabilities)
    # 非能力字段原样保留；能力投射是另一根轴，不被改写。
    assert wire["ui"]["models"]["mode"] == "fixed"
    assert wire["detail"]["capabilityProjection"] == {}


def test_capability_state_to_ui_and_wire() -> None:
    state = ResumeCapability(value="cold", verification="live", note="n")
    assert capability_state_to_ui(state) == "cold"
    assert capability_state_to_wire(state)["note"] == "n"


def test_wire_capability_projection_is_serialized_as_plain_strings() -> None:
    wire = capabilities_to_wire(RICH)
    assert wire["detail"]["capabilityProjection"]["skills"] == "native"
    assert wire["detail"]["capabilityProjection"]["plugins"] == "unsupported"
