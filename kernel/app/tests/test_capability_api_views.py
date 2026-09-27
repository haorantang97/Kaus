"""公共层的能力序列化与能力对账（`app/api/views.py`）。

这些是纯函数，和 HTTP、仓库、任何一家 Backend 都无关，因此在 kernel 里单测；
端到端的端点形状由接入层的 `tests/test_phase1_capabilities.py` 覆盖。
"""

from __future__ import annotations

from app.api.views import (
    build_capability_diff,
    capability_to_wire,
    capability_value_digest,
    effective_capabilities_to_wire,
)
from app.capabilities.models import (
    BlockedCapability,
    EffectiveCapabilities,
    EffectiveCapability,
    ProjectCapability,
)


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #


def test_digest_is_stable_across_key_order():
    """键序不影响摘要——两侧对账各自构造 dict，顺序不该造成假差异。"""
    assert capability_value_digest({"a": 1, "b": 2}) == capability_value_digest({"b": 2, "a": 1})


def test_digest_changes_with_the_value():
    assert capability_value_digest({"a": 1}) != capability_value_digest({"a": 2})


def test_digest_does_not_contain_the_value():
    """§5.4：Drift Diff 必须脱敏。摘要里不得出现原值的任何片段。"""
    secret = "supersecret-value-000"
    digest = capability_value_digest({"api_key": secret})
    assert secret not in digest
    assert digest.startswith("sha256:")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def test_effective_capabilities_wire_shape_answers_why():
    effective = EffectiveCapabilities(
        project_id="project:leaf",
        backend_key="b1",
        entries=(
            EffectiveCapability(
                capability_type="b1:runtime-config",
                capability_id="compression",
                config={"value": {"enabled": True}},
                source_project_id="project:mid",
                inherited=True,
                contributing_project_ids=("project:root", "project:mid"),
            ),
        ),
        blocked=(
            BlockedCapability(
                capability_type="mcp",
                capability_id="mcp-servers",
                blocked_by_project_id="project:mid",
            ),
        ),
    )
    wire = effective_capabilities_to_wire(effective, ancestry=("project:root", "project:leaf"))

    assert wire["projectId"] == "project:leaf"
    assert wire["backendKey"] == "b1"
    assert wire["ancestry"] == ["project:root", "project:leaf"]
    entry = wire["entries"][0]
    assert entry["sourceProjectId"] == "project:mid"
    assert entry["inherited"] is True
    # 链上有两个贡献者 → 这条能力被更近的节点覆盖过。
    assert entry["overridden"] is True
    assert entry["blocked"] is False
    assert wire["blocked"][0]["blockedByProjectId"] == "project:mid"
    assert wire["counts"] == {"entries": 1, "blocked": 1}


def test_single_assignment_is_not_reported_as_overridden():
    effective = EffectiveCapabilities(
        project_id="project:root",
        entries=(
            EffectiveCapability(
                capability_type="memory",
                capability_id="memory",
                source_project_id="project:root",
                inherited=False,
                contributing_project_ids=("project:root",),
            ),
        ),
    )
    assert effective_capabilities_to_wire(effective)["entries"][0]["overridden"] is False


def test_capability_to_wire_keeps_the_assignment_mode():
    assignment = ProjectCapability.create(
        project_id="project:mid",
        capability_type="mcp",
        capability_id="mcp-servers",
        assignment_mode="block",
    )
    wire = capability_to_wire(assignment)
    assert wire["assignmentMode"] == "block"
    assert wire["capabilityType"] == "mcp"


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #


def _row(identifier: str, digest: str, **extra) -> dict:
    return {"id": identifier, "digest": digest, **extra}


def test_identical_sides_are_in_sync():
    rows = [_row("p|t|c", "sha256:aaa")]
    diff = build_capability_diff(expected_rows=rows, actual_rows=list(rows))
    assert diff.in_sync is True
    assert diff.to_wire()["knownDivergences"] == []


def test_missing_and_unexpected_rows_are_separated():
    diff = build_capability_diff(
        expected_rows=[_row("p|t|only-expected", "sha256:a")],
        actual_rows=[_row("p|t|only-actual", "sha256:b")],
    )
    assert diff.missing_in_db == ("p|t|only-expected",)
    assert diff.unexpected_in_db == ("p|t|only-actual",)
    assert diff.in_sync is False


def test_changed_rows_report_digests_not_values():
    diff = build_capability_diff(
        expected_rows=[_row("p|t|c", "sha256:a")],
        actual_rows=[_row("p|t|c", "sha256:b", source_project_id="project:root")],
    )
    assert diff.changed == (
        {
            "id": "p|t|c",
            "expectedDigest": "sha256:a",
            "actualDigest": "sha256:b",
            "actualSourceProjectId": "project:root",
        },
    )


def test_known_divergences_do_not_count_as_differences():
    """接入层可以标注「这条差异是已知的语义差」，它就不再算真差异。"""
    diff = build_capability_diff(
        expected_rows=[
            _row("p|t|blocked", "sha256:a", known_divergence="祖先 Block 的传播语义差"),
            _row("p|t|drifted", "sha256:a", known_divergence="同上"),
        ],
        actual_rows=[_row("p|t|drifted", "sha256:zzz")],
    )
    assert diff.in_sync is True
    assert diff.missing_in_db == ()
    assert diff.changed == ()
    sides = {entry["side"] for entry in diff.known_divergences}
    assert sides == {"missingInDb", "changed"}
    assert all("Block" in entry["reason"] or "同上" in entry["reason"] for entry in diff.known_divergences)


def test_generic_types_cover_the_ones_the_first_import_uses():
    """第一批导入用到的通用类型必须都在通用类型登记表里（UI 分组按它归类）。"""
    from app.capabilities.models import GENERIC_CAPABILITY_TYPES

    assert {"mcp", "hooks", "skills"} <= GENERIC_CAPABILITY_TYPES


# --------------------------------------------------------------------------- #
# Backend 视图（AD-50 三态）
# --------------------------------------------------------------------------- #


def test_backend_wire_capabilities_are_two_layers_plus_an_unknown_count():
    """AD-71：``ui`` 只有取值，``detail`` 才带 note/verification，另报 unknownCount。"""
    from app.api.views import backend_to_wire
    from app.projects.models import Backend
    from runtime.capability_matrix import (
        BackendCapabilities,
        CardCapabilities,
        SessionCapabilities,
        ToolCardCapabilities,
        declare,
        unknown_count,
    )

    note = "方法能调，但只反映当前进程内的会话。"
    capabilities = BackendCapabilities(
        structured_events=True,
        sessions=SessionCapabilities(
            list=declare("own_process", verification="live", note=note), create=True
        ),
        card=CardCapabilities(
            streaming=True, tools=ToolCardCapabilities(calls=True, output=False)
        ),
    )
    backend = Backend.create(key="demo", driver_kind="mock", capabilities=capabilities)
    wire = backend_to_wire(backend)
    assert wire["id"] == "backend:demo"

    # ui 层：只有取值，对话页读它，因此不可能内联到任何备注。
    ui_sessions = wire["capabilities"]["ui"]["sessions"]
    assert ui_sessions["list"] == "own_process"
    assert ui_sessions["create"] == "supported"
    assert ui_sessions["history"] == "unknown", "未声明是 unknown，不是「确认不支持」"
    assert note not in repr(wire["capabilities"]["ui"])
    assert wire["capabilities"]["ui"]["card"]["tools"]["output"] == "unsupported"

    # detail 层：取值 + 状态 + 取证等级 + note。
    detail_sessions = wire["capabilities"]["detail"]["sessions"]
    assert detail_sessions["list"] == {
        "value": "own_process",
        "status": "supported",
        "verification": "live",
        "note": note,
        "supportedBool": True,
    }

    # unknownCount 同时出现在能力块与顶层。
    assert wire["capabilities"]["unknownCount"] == unknown_count(capabilities)
    assert wire["unknownCount"] == wire["capabilities"]["unknownCount"] > 0

    # 整份输出可 JSON 序列化（端点直接返回它）。
    import json

    assert json.loads(json.dumps(wire))["capabilities"]["ui"]["structuredEvents"] == "supported"
