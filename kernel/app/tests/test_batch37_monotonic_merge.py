"""批次三十七 · R3（AD-161）：单调安全合并的三个洞。

外部评审（`docs/quality/external-review-2026-09-07.md` R3）在**真的 Resolver**
上复现出三条路，每一条都能让「子级只能收紧」这个承诺落空：

a. ``{"value": {...}}`` 这层壳让合并发生在外层——子项目只在 ``value`` 里改了
   ``default_model``，祖先的 ``enabled=false`` / ``max_depth=1`` /
   ``max_concurrent=1`` 三条约束整段消失，而且没有 warning；
b. 类型不同直接放行——字符串 ``"true"`` / ``"9"`` 绕过单调检查，随后策略模型
   （pydantic 宽松模式）又把它们转成 ``True`` / ``9`` 投影下去；
c. ``null`` 被当成「解除这条上限」，而不是「这一层不说话」。

用例按这三条组织，最后一组是「非安全键的旧口径没被改坏」的守卫。
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from app.capabilities import monotonic
from app.capabilities.delegation import DelegationPolicy
from app.capabilities.models import ProjectCapability
from app.capabilities.resolver import resolve_effective_capabilities

ROOT = "project:root"
CHILD = "project:child"
GRANDCHILD = "project:grandchild"

DELEGATION = "delegation"

#: 评审复现里那份父项目配置：三条约束全在，且是 ``{"value": {...}}`` 形状。
PARENT_LOCKED: Mapping[str, Any] = {
    "value": {"enabled": False, "max_depth": 1, "max_concurrent": 1}
}


def _assign(project_id: str, config: Mapping[str, Any]) -> ProjectCapability:
    return ProjectCapability.create(
        project_id=project_id,
        capability_type=DELEGATION,
        capability_id=DELEGATION,
        config=dict(config),
    )


def _resolve(*nodes: tuple[str, Mapping[str, Any]]):
    ancestry = [project_id for project_id, _ in nodes]
    assignments = {
        project_id: [_assign(project_id, config)] for project_id, config in nodes
    }
    return resolve_effective_capabilities(ancestry, assignments)


def _effective_config(result) -> dict[str, Any]:
    assert len(result.entries) == 1
    inner, _ = monotonic.unwrap_value(result.entries[0].config)
    return dict(inner)


# --------------------------------------------------------------------------- #
# a · 嵌套结构不得被整段替换
# --------------------------------------------------------------------------- #


def test_child_changing_only_default_model_keeps_every_ancestor_constraint() -> None:
    """评审的原话：用户以为只改了模型，实际把委派重新打开了。"""
    result = _resolve(
        (ROOT, PARENT_LOCKED),
        (CHILD, {"value": {"default_model": "sonnet"}}),
    )
    config = _effective_config(result)
    assert config["enabled"] is False
    assert config["max_depth"] == 1
    assert config["max_concurrent"] == 1
    assert config["default_model"] == "sonnet"
    # 这一条不是越界，不该有 warning——合并对了就该安静。
    assert result.warnings == ()


def test_the_wrapped_shape_survives_the_merge() -> None:
    """形状要保住：下游（投影器）按 ``{"value": ...}`` 取内层。"""
    result = _resolve(
        (ROOT, PARENT_LOCKED), (CHILD, {"value": {"default_model": "sonnet"}})
    )
    _, wrapped = monotonic.unwrap_value(result.entries[0].config)
    assert wrapped is True


def test_bare_dict_shape_merges_field_by_field_too() -> None:
    """裸字典是同一条路：AD-42 两种形状都要落库得下去。"""
    result = _resolve(
        (ROOT, {"enabled": False, "max_depth": 1}),
        (CHILD, {"default_model": "haiku"}),
    )
    config = _effective_config(result)
    assert config == {"enabled": False, "max_depth": 1, "default_model": "haiku"}


def test_the_projected_policy_still_says_delegation_is_off() -> None:
    """走到策略模型这一层再看一次——评审就是在这里看到 ``enabled`` 被补成 true 的。"""
    result = _resolve(
        (ROOT, PARENT_LOCKED), (CHILD, {"value": {"default_model": "sonnet"}})
    )
    policy = DelegationPolicy.from_mapping(_effective_config(result))
    assert policy.enabled is False
    assert policy.max_depth == 1
    assert policy.max_concurrent == 1
    assert policy.default_model == "sonnet"


# --------------------------------------------------------------------------- #
# b · 类型不同不等于放行
# --------------------------------------------------------------------------- #


def test_string_true_cannot_reopen_delegation() -> None:
    result = _resolve((ROOT, PARENT_LOCKED), (CHILD, {"value": {"enabled": "true"}}))
    config = _effective_config(result)
    assert config["enabled"] is False
    assert len(result.warnings) == 1
    assert "enabled" in result.warnings[0]


def test_string_nine_cannot_raise_max_depth() -> None:
    result = _resolve((ROOT, PARENT_LOCKED), (CHILD, {"value": {"max_depth": "9"}}))
    config = _effective_config(result)
    assert config["max_depth"] == 1
    assert len(result.warnings) == 1
    assert "max_depth" in result.warnings[0]


def test_a_legal_numeric_string_is_accepted_and_normalised() -> None:
    """收紧的写法不该被这次改动误伤，而且要以规范化后的形状落下去——
    下游各转各的，正是这个缺陷的来源。"""
    result = _resolve(
        (ROOT, {"value": {"max_depth": 3}}), (CHILD, {"value": {"max_depth": "2"}})
    )
    config = _effective_config(result)
    assert config["max_depth"] == 2
    assert isinstance(config["max_depth"], int) and not isinstance(
        config["max_depth"], bool
    )
    assert result.warnings == ()


def test_an_unrecognisable_value_on_a_safety_knob_is_rejected_with_a_warning() -> None:
    """AD-150 的「看不懂就放行」在安全字段上收窄：看不懂 = 判断不了 = 不放行。"""
    result = _resolve(
        (ROOT, {"value": {"approval_mode": "ask"}}),
        (CHILD, {"value": {"approval_mode": "whatever"}}),
    )
    config = _effective_config(result)
    assert config["approval_mode"] == "ask"
    assert len(result.warnings) == 1
    assert "approval_mode" in result.warnings[0]


def test_a_stricter_approval_mode_still_wins() -> None:
    """单调不是「祖先说了算」：更严的一档照常生效（AD-106 的强度顺序）。"""
    result = _resolve(
        (ROOT, {"value": {"approval_mode": "deny"}}),
        (CHILD, {"value": {"approval_mode": "ask"}}),
    )
    assert _effective_config(result)["approval_mode"] == "ask"
    assert result.warnings == ()


# --------------------------------------------------------------------------- #
# c · null = 继承
# --------------------------------------------------------------------------- #


def test_null_on_a_numeric_limit_inherits_instead_of_unsetting_it() -> None:
    result = _resolve((ROOT, PARENT_LOCKED), (CHILD, {"value": {"max_depth": None}}))
    config = _effective_config(result)
    assert config["max_depth"] == 1
    assert result.warnings == ()


def test_null_on_a_boolean_switch_inherits_too() -> None:
    result = _resolve(
        (ROOT, PARENT_LOCKED),
        (CHILD, {"value": {"enabled": None, "default_model": "opus"}}),
    )
    config = _effective_config(result)
    assert config["enabled"] is False
    assert config["default_model"] == "opus"


def test_null_does_not_become_a_no_limit_in_the_projected_policy() -> None:
    """``None`` 在策略模型里的意思是「本层不设限」——它绝不该由子级凭空造出来。"""
    result = _resolve(
        (ROOT, PARENT_LOCKED), (CHILD, {"value": {"max_concurrent": None}})
    )
    assert DelegationPolicy.from_mapping(_effective_config(result)).max_concurrent == 1


# --------------------------------------------------------------------------- #
# 约束沿树累积 / 非安全键的旧口径
# --------------------------------------------------------------------------- #


def test_constraints_accumulate_down_the_whole_chain() -> None:
    """根压到 1、中间层放 9 被驳回、孙节点再放 5 一样被驳回（AD-150 原文）。"""
    result = _resolve(
        (ROOT, {"value": {"max_depth": 1}}),
        (CHILD, {"value": {"max_depth": 9}}),
        (GRANDCHILD, {"value": {"max_depth": 5}}),
    )
    assert _effective_config(result)["max_depth"] == 1
    assert len(result.warnings) == 2


def test_unknown_non_safety_keys_are_still_child_wins() -> None:
    """AD-150 的「不认得就别拦」对**非安全**键原样保留。"""
    result = _resolve(
        (ROOT, {"value": {"max_depth": 1, "timeout_seconds": 30}}),
        (CHILD, {"value": {"timeout_seconds": 600, "something_new": "x"}}),
    )
    config = _effective_config(result)
    assert config["timeout_seconds"] == 600
    assert config["something_new"] == "x"
    assert config["max_depth"] == 1
    assert result.warnings == ()


def test_a_non_monotonic_capability_type_is_untouched() -> None:
    """只有 :data:`MONOTONIC_CAPABILITY_NAMES` 里的类型走这条策略。"""
    assert "skills" not in monotonic.MONOTONIC_CAPABILITY_NAMES
    assignments = {
        ROOT: [
            ProjectCapability.create(
                project_id=ROOT,
                capability_type="skills",
                capability_id="s1",
                config={"value": {"enabled": False}},
            )
        ],
        CHILD: [
            ProjectCapability.create(
                project_id=CHILD,
                capability_type="skills",
                capability_id="s1",
                config={"value": {"enabled": True}},
            )
        ],
    }
    result = resolve_effective_capabilities([ROOT, CHILD], assignments)
    assert result.entries[0].config == {"value": {"enabled": True}}
    assert result.warnings == ()


# --------------------------------------------------------------------------- #
# 转换与策略模型同源
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "raw"),
    [("enabled", "true"), ("enabled", "false"), ("max_depth", "9")],
)
def test_the_coercion_agrees_with_the_policy_model(field: str, raw: str) -> None:
    """检查时看到的值必须与投影时生效的值是同一个（AD-161 的理由）。"""
    entry = monotonic.safety_field(field)
    assert entry is not None
    assert entry.coerce(raw) == getattr(DelegationPolicy(**{field: raw}), field)


def test_a_truly_uncomparable_value_raises_rather_than_silently_passing() -> None:
    entry = monotonic.safety_field("max_depth")
    assert entry is not None
    with pytest.raises(monotonic.UncomparableValue):
        entry.coerce("not-a-number")
    with pytest.raises(monotonic.UncomparableValue):
        # 布尔不是数字：``True <= 1`` 在 Python 里成立，但它不是这个字段的语义。
        entry.coerce(True)
