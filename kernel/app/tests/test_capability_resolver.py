"""Capability Resolver 测试（R-01；v1.0 §5.2 / §16.2）。

重点验证 R-01 的核心主张：**backend-scoped 能力类型与通用能力走同一个
Resolver**，只在输出侧按 backend 过滤；以及祖先继承 / 本地覆盖 / 阻断三件事。
"""

from __future__ import annotations

import pytest

from app.capabilities.models import (
    GENERIC_CAPABILITY_TYPES,
    ProjectCapability,
    is_backend_scoped,
    parse_capability_type,
    scoped_capability_type,
)
from app.capabilities.resolver import (
    CapabilityResolutionRequest,
    CapabilityResolver,
    TreeCapabilityResolver,
    merge_config_child_wins,
    replace_child_wins,
    resolve_effective_capabilities,
    select_merge_strategy,
)
from app.errors import CapabilityTypeError

# 树：project:x（根） → project:coding → project:pronto
ROOT = "project:x"
MID = "project:coding"
LEAF = "project:pronto"
CHAIN = (ROOT, MID, LEAF)

# R-01 的两个 backend-scoped 例子（用具体 backend key 表达；公共层代码不认识它们）。
RUNTIME_CONFIG = scoped_capability_type("acme", "runtime-config")
DEFAULT_MODEL = scoped_capability_type("acme", "default-model")
OTHER_RUNTIME_CONFIG = scoped_capability_type("other", "runtime-config")


def assign(
    project_id: str,
    capability_type: str,
    capability_id: str,
    *,
    config: dict | None = None,
    version: str | None = None,
    mode: str = "local",
) -> ProjectCapability:
    return ProjectCapability.create(
        project_id=project_id,
        capability_type=capability_type,
        capability_id=capability_id,
        config=config,
        version=version,
        assignment_mode=mode,  # type: ignore[arg-type]
    )


def group(*assignments: ProjectCapability) -> dict[str, list[ProjectCapability]]:
    table: dict[str, list[ProjectCapability]] = {}
    for item in assignments:
        table.setdefault(item.project_id, []).append(item)
    return table


# --------------------------------------------------------------------------- #
# 能力类型解析（R-01 的两种形态）
# --------------------------------------------------------------------------- #


def test_generic_and_backend_scoped_types() -> None:
    assert parse_capability_type("skills") == (None, "skills")
    assert parse_capability_type(RUNTIME_CONFIG) == ("acme", "runtime-config")
    assert is_backend_scoped(RUNTIME_CONFIG) is True
    assert is_backend_scoped("skills") is False
    assert {"skills", "mcp", "instructions", "plugins"} <= GENERIC_CAPABILITY_TYPES


@pytest.mark.parametrize("bad", ["", "Skills", "a:b:c", "with space", "-lead"])
def test_invalid_capability_types(bad: str) -> None:
    with pytest.raises(CapabilityTypeError):
        parse_capability_type(bad)


def test_merge_strategy_is_selected_by_name_not_backend() -> None:
    """策略表按类型 name 选择，因此不含任何 backend 名字（N §3）。"""
    assert select_merge_strategy(RUNTIME_CONFIG) is merge_config_child_wins
    assert select_merge_strategy(OTHER_RUNTIME_CONFIG) is merge_config_child_wins
    assert select_merge_strategy("skills") is replace_child_wins
    assert select_merge_strategy(DEFAULT_MODEL) is replace_child_wins


# --------------------------------------------------------------------------- #
# 祖先继承
# --------------------------------------------------------------------------- #


def test_ancestor_inheritance_reaches_leaf() -> None:
    assignments = group(assign(ROOT, "skills", "code-review", version="1.0"))
    effective = resolve_effective_capabilities(CHAIN, assignments)

    entry = effective.get("skills", "code-review")
    assert entry is not None
    assert entry.source_project_id == ROOT
    assert entry.inherited is True
    assert entry.version == "1.0"
    assert entry.contributing_project_ids == (ROOT,)
    assert effective.project_id == LEAF


def test_nearest_ancestor_wins_over_farther_ancestor() -> None:
    assignments = group(
        assign(ROOT, "skills", "code-review", version="1.0"),
        assign(MID, "skills", "code-review", version="2.0"),
    )
    entry = resolve_effective_capabilities(CHAIN, assignments).get("skills", "code-review")
    assert entry is not None
    assert entry.version == "2.0"
    assert entry.source_project_id == MID
    assert entry.contributing_project_ids == (ROOT, MID)


def test_local_override_wins_and_is_not_marked_inherited() -> None:
    """v1.0 §5.2：本地同 ID 版本覆盖祖先版本（Child Wins）。"""
    assignments = group(
        assign(ROOT, "skills", "code-review", version="1.0"),
        assign(LEAF, "skills", "code-review", version="3.0", mode="inherited-override"),
    )
    entry = resolve_effective_capabilities(CHAIN, assignments).get("skills", "code-review")
    assert entry is not None
    assert entry.version == "3.0"
    assert entry.source_project_id == LEAF
    assert entry.inherited is False


def test_union_across_types_and_ids() -> None:
    assignments = group(
        assign(ROOT, "skills", "a"),
        assign(MID, "skills", "b"),
        assign(LEAF, "mcp", "github"),
    )
    effective = resolve_effective_capabilities(CHAIN, assignments)
    assert {e.capability_id for e in effective.by_type("skills")} == {"a", "b"}
    assert effective.get("mcp", "github") is not None
    assert effective.capability_types() == ("mcp", "skills")


# --------------------------------------------------------------------------- #
# 阻断
# --------------------------------------------------------------------------- #


def test_local_block_removes_inherited_entry() -> None:
    assignments = group(
        assign(ROOT, "skills", "code-review"),
        assign(LEAF, "skills", "code-review", mode="block"),
    )
    effective = resolve_effective_capabilities(CHAIN, assignments)
    assert effective.get("skills", "code-review") is None
    assert effective.is_blocked("skills", "code-review")
    assert effective.blocked[0].blocked_by_project_id == LEAF


def test_ancestor_block_propagates_to_descendants() -> None:
    assignments = group(
        assign(ROOT, "skills", "code-review"),
        assign(MID, "skills", "code-review", mode="block"),
    )
    effective = resolve_effective_capabilities(CHAIN, assignments)
    assert effective.get("skills", "code-review") is None
    assert effective.is_blocked("skills", "code-review")


def test_descendant_reassignment_overrides_ancestor_block() -> None:
    """Child-Wins：后代显式重新赋值可以复活被祖先阻断的条目。"""
    assignments = group(
        assign(ROOT, "skills", "code-review", version="1.0"),
        assign(MID, "skills", "code-review", mode="block"),
        assign(LEAF, "skills", "code-review", version="9.0"),
    )
    effective = resolve_effective_capabilities(CHAIN, assignments)
    entry = effective.get("skills", "code-review")
    assert entry is not None and entry.version == "9.0"
    assert not effective.is_blocked("skills", "code-review")


def test_block_assignment_cannot_carry_config() -> None:
    with pytest.raises(Exception):
        ProjectCapability.create(
            project_id=ROOT,
            capability_type="skills",
            capability_id="x",
            assignment_mode="block",
            config={"a": 1},
        )


# --------------------------------------------------------------------------- #
# R-01：backend-scoped 能力走同一个 Resolver
# --------------------------------------------------------------------------- #


def test_r01_scoped_and_generic_share_one_resolver() -> None:
    assignments = group(
        assign(ROOT, "skills", "code-review"),
        assign(ROOT, RUNTIME_CONFIG, "default", config={"providers": ["p1"], "compression": True}),
        assign(ROOT, DEFAULT_MODEL, "default", config={"modelId": "root-model"}),
    )
    effective = resolve_effective_capabilities(CHAIN, assignments)

    # 一次解析同时产出通用能力与 backend-scoped 能力。
    assert effective.get("skills", "code-review") is not None
    assert effective.get(RUNTIME_CONFIG, "default") is not None
    assert effective.get(DEFAULT_MODEL, "default") is not None
    assert len(effective.generic()) == 1
    assert len(effective.scoped_for("acme")) == 2


def test_r01_runtime_config_merges_per_key_root_change_propagates() -> None:
    """R-01 核心价值：根上改一次、全树生效；子级只覆盖个别键。"""
    assignments = group(
        assign(
            ROOT,
            RUNTIME_CONFIG,
            "default",
            config={"providers": ["p1"], "compression": True, "context": 8000},
        ),
        assign(LEAF, RUNTIME_CONFIG, "default", config={"context": 32000}),
    )
    entry = resolve_effective_capabilities(CHAIN, assignments).get(RUNTIME_CONFIG, "default")
    assert entry is not None
    assert entry.config == {"providers": ["p1"], "compression": True, "context": 32000}
    assert entry.source_project_id == LEAF
    assert entry.contributing_project_ids == (ROOT, LEAF)


def test_r01_default_model_is_soft_inheritance_d10() -> None:
    """D-10：Binding 的本地 default model = 未被祖先 scoped 配置覆盖时的取值。"""
    only_root = group(assign(ROOT, DEFAULT_MODEL, "default", config={"modelId": "root-model"}))
    entry = resolve_effective_capabilities(CHAIN, only_root).get(DEFAULT_MODEL, "default")
    assert entry is not None and entry.config == {"modelId": "root-model"}
    assert entry.inherited is True

    with_local = group(
        assign(ROOT, DEFAULT_MODEL, "default", config={"modelId": "root-model"}),
        assign(LEAF, DEFAULT_MODEL, "default", config={"modelId": "leaf-model"}),
    )
    entry = resolve_effective_capabilities(CHAIN, with_local).get(DEFAULT_MODEL, "default")
    assert entry is not None and entry.config == {"modelId": "leaf-model"}
    assert entry.inherited is False


def test_r01_projector_filters_by_backend() -> None:
    """R-01 落盘层：Projector 按 backend 过滤后写入对应 Binding。"""
    assignments = group(
        assign(ROOT, "skills", "code-review"),
        assign(ROOT, RUNTIME_CONFIG, "default", config={"a": 1}),
        assign(ROOT, OTHER_RUNTIME_CONFIG, "default", config={"b": 2}),
    )
    for_acme = resolve_effective_capabilities(CHAIN, assignments, backend_key="acme")
    types = set(for_acme.capability_types())
    assert types == {"skills", RUNTIME_CONFIG}
    assert OTHER_RUNTIME_CONFIG not in types

    for_other = resolve_effective_capabilities(CHAIN, assignments, backend_key="other")
    assert set(for_other.capability_types()) == {"skills", OTHER_RUNTIME_CONFIG}

    # 不过滤时两者都在（树计算层是 backend 无关的）。
    unfiltered = resolve_effective_capabilities(CHAIN, assignments)
    assert len(unfiltered.entries) == 3


def test_backend_filter_also_filters_blocked_list() -> None:
    assignments = group(
        assign(ROOT, RUNTIME_CONFIG, "default", config={"a": 1}),
        assign(ROOT, OTHER_RUNTIME_CONFIG, "default", config={"b": 2}),
        assign(LEAF, RUNTIME_CONFIG, "default", mode="block"),
        assign(LEAF, OTHER_RUNTIME_CONFIG, "default", mode="block"),
    )
    for_acme = resolve_effective_capabilities(CHAIN, assignments, backend_key="acme")
    assert [b.capability_type for b in for_acme.blocked] == [RUNTIME_CONFIG]


# --------------------------------------------------------------------------- #
# Resolver 接口
# --------------------------------------------------------------------------- #


def test_tree_resolver_implements_protocol() -> None:
    resolver = TreeCapabilityResolver()
    assert isinstance(resolver, CapabilityResolver)


def test_resolver_interface_matches_pure_function() -> None:
    assignments = group(
        assign(ROOT, "skills", "a"),
        assign(LEAF, RUNTIME_CONFIG, "default", config={"x": 1}),
    )
    request = CapabilityResolutionRequest(ancestry=CHAIN, backend_key="acme")
    assert request.target_project_id == LEAF

    via_interface = TreeCapabilityResolver().resolve(request, assignments)
    via_function = resolve_effective_capabilities(CHAIN, assignments, backend_key="acme")
    assert via_interface == via_function


def test_custom_merge_strategy_can_be_injected() -> None:
    """v1.0 §5.2：不同类型不得使用完全相同的合并算法 → 策略可注入。"""
    assignments = group(
        assign(ROOT, "skills", "a", config={"k": 1}),
        assign(LEAF, "skills", "a", config={"j": 2}),
    )
    merged = TreeCapabilityResolver(strategies={"skills": merge_config_child_wins}).resolve(
        CapabilityResolutionRequest(ancestry=CHAIN), assignments
    )
    entry = merged.get("skills", "a")
    assert entry is not None and entry.config == {"k": 1, "j": 2}

    replaced = TreeCapabilityResolver().resolve(
        CapabilityResolutionRequest(ancestry=CHAIN), assignments
    )
    entry = replaced.get("skills", "a")
    assert entry is not None and entry.config == {"j": 2}


def test_resolver_is_pure_and_deterministic() -> None:
    assignments = group(
        assign(ROOT, "skills", "b"),
        assign(ROOT, "skills", "a"),
        assign(MID, "mcp", "github"),
    )
    first = resolve_effective_capabilities(CHAIN, assignments)
    second = resolve_effective_capabilities(CHAIN, assignments)
    assert first == second
    # 输出顺序稳定（按 type, id 排序），便于 diff 与快照。
    assert [(e.capability_type, e.capability_id) for e in first.entries] == [
        ("mcp", "github"),
        ("skills", "a"),
        ("skills", "b"),
    ]


def test_empty_ancestry_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_effective_capabilities((), {})


def test_assignments_must_belong_to_their_node() -> None:
    bad = {ROOT: [assign(MID, "skills", "a")]}
    with pytest.raises(ValueError):
        resolve_effective_capabilities(CHAIN, bad)


def test_missing_nodes_are_treated_as_empty() -> None:
    effective = resolve_effective_capabilities(CHAIN, {LEAF: [assign(LEAF, "skills", "a")]})
    assert len(effective.entries) == 1
