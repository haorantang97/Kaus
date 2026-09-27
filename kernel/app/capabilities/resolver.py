"""Capability Resolver：树继承 / 本地覆盖 / 阻断的纯函数实现（R-01 树计算层）。

职责
----
把「根 → … → 目标 Project」这条祖先链上的能力赋值，合并成目标 Project 的
有效能力集合。**Backend 无关**：通用能力（skills / mcp / instructions /
plugins …）与 backend-scoped 能力（``<backend>:runtime-config``、
``<backend>:default-model`` …）走完全相同的合并算法，唯一区别是可选的
``backend_key`` 过滤视图（供 Projector 落盘用）。

本模块是**纯函数**：不读文件、不查数据库、不依赖时间。输入 = 祖先链 + 赋值表，
输出 = :class:`~app.capabilities.models.EffectiveCapabilities`。

合并规则（v1.0 §5.2）
--------------------
按祖先链**从根到叶**顺序处理每个节点：

1. 该节点的 ``block`` 赋值：把对应 (type, id) 从当前累积集合中移除，并记入
   ``blocked``。因为处理是自根向叶的，祖先的 block 会影响其所有后代；后代若
   再显式赋值同一条目，则重新生效（child-wins）。
2. 该节点的 ``local`` / ``inherited-override`` 赋值：按该 capability type 的
   **Merge Strategy** 与已累积的祖先值合并。

v1.0 §5.2 明确要求「不同类型不得使用完全相同的合并算法」，因此这里把合并策略
做成可注入的 :class:`MergeStrategy`，按 capability type 的 *name* 部分选择
（name 是 ``<backend>:<name>`` 里冒号后的那段，因此策略表**不含任何 backend 名字**）：

- :func:`replace_child_wins`（默认）：子级整条替换祖先条目。
  对应 v1.0 §5.2「本地同 ID 版本覆盖祖先版本」（skills）与「同 ID Connection 由
  子级配置覆盖」（mcp），以及 R-01 的 ``<backend>:default-model``
  ——最近的祖先赋值生效，本地赋值覆盖之（D-10 软继承语义）。
- :func:`merge_config_child_wins`：按**键**合并 config，子级键覆盖祖先键。
  用于 ``runtime-config`` 这类「整份配置」型 backend-scoped 能力，
  以保住 R-01 的核心价值「根上改一次、全树生效」，同时允许子级只覆盖个别键。
- :func:`monotonic_child_wins`：同上，但**安全字段只能收紧**（R-12 / AD-150）。
  用于 ``delegation`` / ``delegation-extras`` / ``policies``。

对应规范
--------
- R-01：继承拆两层；backend-scoped 能力与通用能力走同一个 Resolver；
  Projector 按 backend 过滤。
- v1.0 §5.2 / §5.3：继承计算与「Project-owned, Agent-materialized」四层拆分。
- v1.0 §5.2：每条规则保留来源 Project（这里是 ``source_project_id`` 与
  ``contributing_project_ids``）。
- v1.0 §16.2 Capability Resolver 测试项：多层继承 / Child Override / Block /
  Dict Merge / 版本冲突。
- R-12 / AD-150：安全类规则的**单调合并**——:func:`monotonic_child_wins`。
  安全字段（审批档、委派深度/并发、自动放行开关）子项目只能收紧不能放宽；
  越界的写法保留祖先值并进 ``EffectiveCapabilities.warnings``。字段表在
  :mod:`app.capabilities.monotonic`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from app.base import DomainModel
from app.capabilities import monotonic
from app.capabilities.models import (
    BlockedCapability,
    EffectiveCapabilities,
    EffectiveCapability,
    ProjectCapability,
    parse_capability_type,
)
from app.ids import ProjectId

# --------------------------------------------------------------------------- #
# Merge Strategy
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MergeOutcome:
    """一次合并的结果 + 这次合并有话要说（批次二十四 / AD-150）。

    策略返回**裸的** :class:`EffectiveCapability` 仍然合法（旧策略一行不用改）；
    需要向上报告「你写的那个值没生效」的策略返回本类型。
    """

    effective: EffectiveCapability
    warnings: tuple[str, ...] = ()


MergeStrategy = Callable[
    [EffectiveCapability, ProjectCapability], "EffectiveCapability | MergeOutcome"
]
"""(已累积的祖先有效值, 当前节点的赋值) -> 新的有效值（或带 warnings 的结果）。"""


def _as_outcome(result: EffectiveCapability | MergeOutcome) -> MergeOutcome:
    return result if isinstance(result, MergeOutcome) else MergeOutcome(result)


def replace_child_wins(
    inherited: EffectiveCapability, assignment: ProjectCapability
) -> EffectiveCapability:
    """子级整条替换祖先条目（默认策略）。"""
    return EffectiveCapability(
        capability_type=assignment.capability_type,
        capability_id=assignment.capability_id,
        config=dict(assignment.config),
        version=assignment.version,
        source_project_id=assignment.project_id,
        inherited=False,
        contributing_project_ids=(
            *inherited.contributing_project_ids,
            assignment.project_id,
        ),
    )


def merge_config_child_wins(
    inherited: EffectiveCapability, assignment: ProjectCapability
) -> EffectiveCapability:
    """按键合并 config，子级键覆盖祖先键；祖先独有的键保留。

    这是 R-01「根上改一次、全树生效」的载体：祖先在根上写一份 runtime-config，
    子级只需要覆盖个别键，其余键继续从根继承。
    """
    merged: dict[str, Any] = dict(inherited.config)
    merged.update(assignment.config)
    return EffectiveCapability(
        capability_type=assignment.capability_type,
        capability_id=assignment.capability_id,
        config=merged,
        version=assignment.version or inherited.version,
        source_project_id=assignment.project_id,
        inherited=False,
        contributing_project_ids=(
            *inherited.contributing_project_ids,
            assignment.project_id,
        ),
    )


def monotonic_child_wins(
    inherited: EffectiveCapability, assignment: ProjectCapability
) -> MergeOutcome:
    """R-12 / AD-07 / AD-150：安全字段只能收紧，其余字段照常 child-wins。

    「其余字段照常」这一半很重要：单调不是「祖先说了算」，是「祖先的**安全约束**
    说了算」。子项目改 ``default_model`` / ``timeout_seconds`` 这类非安全字段，
    与默认策略完全一样地生效。哪些字段算安全，见
    :data:`app.capabilities.monotonic.SAFETY_FIELDS`——一张表，不是散在这里的
    if/else。

    合并的基线是**祖先的有效值**（不是本节点的赋值），所以约束沿树累积：根压到 1、
    中间层想放到 9 会被驳回并保留 1，叶子再想放到 5 一样被驳回。

    AD-161：合并发生在**字段级**，且先把 ``{"value": {...}}`` 这层壳拆开。
    在外层 ``update`` 的旧写法会让子级那份 ``value`` 整段顶掉祖先的 ``value``，
    于是「只改了个默认模型」顺手抹掉了祖先所有的安全约束（评审 R3 的第一条路）。
    """
    corrected, warnings = monotonic.enforce(
        inherited.config,
        assignment.config,
        context=f"{assignment.project_id} 的 {assignment.capability_type}/{assignment.capability_id}",
    )
    merged = monotonic.merge_fields(inherited.config, corrected)
    return MergeOutcome(
        EffectiveCapability(
            capability_type=assignment.capability_type,
            capability_id=assignment.capability_id,
            config=merged,
            version=assignment.version or inherited.version,
            source_project_id=assignment.project_id,
            inherited=False,
            contributing_project_ids=(
                *inherited.contributing_project_ids,
                assignment.project_id,
            ),
        ),
        warnings,
    )


#: 按 capability type 的 *name* 段选择策略。键里不含任何 backend 名字，
#: 因此 ``<backend-a>:runtime-config`` 与 ``<backend-b>:runtime-config`` 自动同策略。
DEFAULT_MERGE_STRATEGIES: Final[Mapping[str, MergeStrategy]] = {
    "runtime-config": merge_config_child_wins,
    # AD-150：委派与策略类能力走单调合并（安全字段只能收紧）。
    **{name: monotonic_child_wins for name in monotonic.MONOTONIC_CAPABILITY_NAMES},
}

DEFAULT_MERGE_STRATEGY: Final[MergeStrategy] = replace_child_wins


def select_merge_strategy(
    capability_type: str,
    strategies: Mapping[str, MergeStrategy] | None = None,
) -> MergeStrategy:
    """按能力类型选择合并策略；未登记的类型用默认的整条替换。"""
    table = DEFAULT_MERGE_STRATEGIES if strategies is None else strategies
    name = parse_capability_type(capability_type).name
    return table.get(name, DEFAULT_MERGE_STRATEGY)


# --------------------------------------------------------------------------- #
# Resolver 接口
# --------------------------------------------------------------------------- #


class CapabilityResolutionRequest(DomainModel):
    """一次解析请求。

    ``ancestry`` 是**从根到目标**的 Project id 序列，最后一个元素即目标 Project。
    调用方（Project 服务 / 迁移器）负责把树展开成这条链——Resolver 本身不认识树，
    也就不需要任何存储依赖。
    """

    ancestry: tuple[ProjectId, ...]
    #: 非空时输出只保留通用能力 + 该 backend 的 scoped 能力（R-01 Projector 过滤）。
    backend_key: str | None = None

    @property
    def target_project_id(self) -> ProjectId:
        if not self.ancestry:
            raise ValueError("ancestry 不能为空：至少要有目标 Project 自己")
        return self.ancestry[-1]


@runtime_checkable
class CapabilityResolver(Protocol):
    """R-01 树计算层的统一接口。通用能力与 backend-scoped 能力共用它。"""

    def resolve(
        self,
        request: CapabilityResolutionRequest,
        assignments: Mapping[str, Sequence[ProjectCapability]],
    ) -> EffectiveCapabilities:
        """按祖先链合并赋值，返回目标 Project 的有效能力。

        参数
        ----
        request:
            祖先链 + 可选 backend 过滤。
        assignments:
            ``project_id -> 该 Project 上的能力赋值列表``。缺失的 project id
            视为没有任何赋值。
        """
        ...


# --------------------------------------------------------------------------- #
# 纯函数实现
# --------------------------------------------------------------------------- #


def resolve_effective_capabilities(
    ancestry: Sequence[str],
    assignments: Mapping[str, Sequence[ProjectCapability]],
    *,
    backend_key: str | None = None,
    strategies: Mapping[str, MergeStrategy] | None = None,
) -> EffectiveCapabilities:
    """R-01 树计算层的纯函数实现（祖先继承 + 本地覆盖 + 阻断）。"""
    if not ancestry:
        raise ValueError("ancestry 不能为空：至少要有目标 Project 自己")

    target_project_id = ancestry[-1]
    accumulated: dict[tuple[str, str], EffectiveCapability] = {}
    #: 记录曾经贡献过、但当前被 block 掉的条目的 provenance，便于后代重新赋值时接续。
    provenance: dict[tuple[str, str], tuple[str, ...]] = {}
    blocked: dict[tuple[str, str], BlockedCapability] = {}
    #: AD-150：合并过程中被驳回的写法（越界的安全字段保留了祖先值）。
    merge_warnings: list[str] = []

    for project_id_in_chain in ancestry:
        node_assignments = list(assignments.get(project_id_in_chain, ()))
        _assert_assignments_belong_to(node_assignments, project_id_in_chain)

        # 1) 先处理 block：祖先的 block 对后代生效，直到后代重新赋值。
        for assignment in node_assignments:
            if assignment.assignment_mode != "block":
                continue
            key = assignment.key
            accumulated.pop(key, None)
            blocked[key] = BlockedCapability(
                capability_type=assignment.capability_type,
                capability_id=assignment.capability_id,
                blocked_by_project_id=project_id_in_chain,
            )

        # 2) 再处理赋值 / 覆盖：child-wins，按类型策略合并。
        for assignment in node_assignments:
            if assignment.assignment_mode == "block":
                continue
            key = assignment.key
            blocked.pop(key, None)
            inherited = accumulated.get(key) or _empty_effective(assignment, provenance.get(key, ()))
            strategy = select_merge_strategy(assignment.capability_type, strategies)
            outcome = _as_outcome(strategy(inherited, assignment))
            merged = outcome.effective
            merge_warnings.extend(outcome.warnings)
            accumulated[key] = merged
            provenance[key] = merged.contributing_project_ids

    entries = tuple(
        entry.evolve(inherited=entry.source_project_id != target_project_id)
        for entry in _stable_sorted(accumulated.values())
    )
    blocked_entries = tuple(_stable_sorted_blocked(blocked.values()))

    if backend_key is not None:
        entries = tuple(_filter_for_backend(entries, backend_key))
        blocked_entries = tuple(
            b
            for b in blocked_entries
            if _scope_of(b.capability_type) in (None, backend_key)
        )

    return EffectiveCapabilities(
        project_id=target_project_id,
        backend_key=backend_key,
        entries=entries,
        blocked=blocked_entries,
        warnings=tuple(merge_warnings),
    )


class TreeCapabilityResolver:
    """:class:`CapabilityResolver` 的默认实现，包装纯函数并允许注入合并策略表。"""

    def __init__(self, strategies: Mapping[str, MergeStrategy] | None = None) -> None:
        self._strategies = strategies

    def resolve(
        self,
        request: CapabilityResolutionRequest,
        assignments: Mapping[str, Sequence[ProjectCapability]],
    ) -> EffectiveCapabilities:
        return resolve_effective_capabilities(
            request.ancestry,
            assignments,
            backend_key=request.backend_key,
            strategies=self._strategies,
        )


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _empty_effective(
    assignment: ProjectCapability, contributing: tuple[str, ...]
) -> EffectiveCapability:
    """尚无祖先值时的空起点；``source_project_id`` 会被策略立刻覆盖。"""
    return EffectiveCapability(
        capability_type=assignment.capability_type,
        capability_id=assignment.capability_id,
        config={},
        version=None,
        source_project_id=assignment.project_id,
        inherited=False,
        contributing_project_ids=contributing,
    )


def _assert_assignments_belong_to(
    assignments: Sequence[ProjectCapability], project_id_in_chain: str
) -> None:
    wrong = [a.id for a in assignments if a.project_id != project_id_in_chain]
    if wrong:
        raise ValueError(
            f"assignments[{project_id_in_chain!r}] 里混入了别的 Project 的赋值：{wrong}"
        )


def _scope_of(capability_type: str) -> str | None:
    return parse_capability_type(capability_type).backend_key


def _filter_for_backend(
    entries: Iterable[EffectiveCapability], backend_key: str
) -> Iterable[EffectiveCapability]:
    for entry in entries:
        scope = entry.type_ref.backend_key
        if scope is None or scope == backend_key:
            yield entry


def _stable_sorted(
    entries: Iterable[EffectiveCapability],
) -> list[EffectiveCapability]:
    return sorted(entries, key=lambda e: (e.capability_type, e.capability_id))


def _stable_sorted_blocked(
    entries: Iterable[BlockedCapability],
) -> list[BlockedCapability]:
    return sorted(entries, key=lambda e: (e.capability_type, e.capability_id))


__all__ = [
    "CapabilityResolutionRequest",
    "CapabilityResolver",
    "DEFAULT_MERGE_STRATEGIES",
    "DEFAULT_MERGE_STRATEGY",
    "MergeOutcome",
    "MergeStrategy",
    "TreeCapabilityResolver",
    "merge_config_child_wins",
    "monotonic_child_wins",
    "replace_child_wins",
    "resolve_effective_capabilities",
    "select_merge_strategy",
]
