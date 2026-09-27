"""序列化与对账：纯函数，不依赖任何 web 框架。

为什么单独一个模块
------------------
1. 端点处理器里剩下的只有「取参数、调 Repository、返回 dict」，路由层因此极薄；
2. 对账逻辑（:func:`build_domain_diff`）可以脱离 HTTP 单独测试；
3. 本模块不 import web 框架，公共层的模型扫描（``kernel/tests/
   test_public_type_purity.py`` 会 import ``app`` 下每个子模块）不需要装框架。

对账端点的输入形状
------------------
对账要比较两侧：

- **期望侧**：由调用方（接入层）从旧 JSON 状态推导出来的一份快照。它的元素是
  普通 dict，字段名与领域模型的蛇形字段一一对应，见
  :data:`COMPARED_PROJECT_FIELDS` / :data:`COMPARED_BINDING_FIELDS`；
- **实际侧**：领域库里现有的 :class:`~app.projects.models.Project` /
  :class:`~app.projects.models.AgentBinding`。

本模块**不知道**期望侧是怎么算出来的——旧状态文件的读取、旧树语义的复刻属于
接入层，公共层只做集合比对。这样公共层不必认识任何一家 Backend 的旧布局。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities, ProjectCapability
from app.projects.models import AgentBinding, Backend, Project
from runtime.capability_matrix import capabilities_to_wire

#: 对账时逐字段比对的 Project 字段（蛇形名）。
#: 刻意不含 ``created_at`` / ``updated_at``：它们是写入时刻的记录，不是内容。
COMPARED_PROJECT_FIELDS: tuple[str, ...] = (
    "slug",
    "display_name",
    "parent_project_id",
    "status",
    "metadata",
)

#: 对账时逐字段比对的 AgentBinding 字段（蛇形名）。
COMPARED_BINDING_FIELDS: tuple[str, ...] = (
    "project_id",
    "backend_id",
    "display_name",
    "native_scope_ref",
    "enabled",
    "is_default",
    "runtime_config",
)


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def project_to_wire(project: Project) -> dict[str, Any]:
    """Project → wire dict（驼峰键，与 v1.0 §4.1 的接口一致）。"""
    return project.model_dump(mode="json", by_alias=True)


def backend_to_wire(backend: Backend) -> dict[str, Any]:
    """Backend → wire dict（驼峰键，与 v1.0 §4.2 / §12.3 一致）。

    AD-127：探测失败（``probeState == "unavailable"``）且**有过**一次成功探测时，
    这里回的是那一次的能力快照，每项 ``verification`` 标 ``cached``、带
    ``capturedAt``，顶层多一个 ``cachedAt``。引擎离线是 ``probeState`` +
    ``probeMessage`` 这一个事实，不该顺手把已经验证过的能力抹回 ``unknown``
    ——那会让整张能力清单与工具卡在界面上集体消失。从未成功探测过的 Backend
    没有快照，那时才是名副其实的全 ``unknown``。

    AD-71：``capabilities`` 分两层——``ui`` 只有取值（对话页读这一层，因此不可能
    把能力备注内联到对话界面上），``detail`` 才带 ``note`` 与 ``verification``
    （给想知道「为什么没有这个按钮」的人看）。``unknownCount`` 同时提到顶层，
    读者一眼看得出这份声明还有多少题没落实（未声明/未实测）。
    """
    payload = backend.model_dump(mode="json", by_alias=True)
    snapshot = backend.capability_snapshot
    cached = backend.probe_state == "unavailable" and snapshot is not None
    capabilities = capabilities_to_wire(
        snapshot.capabilities if cached else backend.capabilities,
        captured_at=(
            snapshot.captured_at.isoformat().replace("+00:00", "Z") if cached else None
        ),
    )
    payload["capabilities"] = capabilities
    payload["unknownCount"] = capabilities["unknownCount"]
    # 快照的原始形态不上 wire：它与 capabilities 是同一份内容的两种说法，
    # 上了就是给前端两个真源。读者要的那一样东西（采集时间）在 cachedAt 里。
    payload.pop("capabilitySnapshot", None)
    return payload


def binding_to_wire(binding: AgentBinding) -> dict[str, Any]:
    """AgentBinding → wire dict。``discriminator`` 是从 id 派生的只读补充字段。

    ``origin`` 一并透出（批次八第 2 件）：UI 要能区分「迁移进来的」与
    「自己建的」——后者可以删，前者删了下次导入还会回来。
    """
    payload = binding.model_dump(mode="json", by_alias=True)
    payload["discriminator"] = binding.discriminator
    return payload


def capability_value_digest(value: Any) -> str:
    """能力值的稳定摘要。

    对账**只输出摘要、不输出值**：这些能力里有 `providers` 与
    `credential_pool_strategies`（R-06），而 v1.0 §5.4 要求 Drift Diff 与日志
    继续执行脱敏。摘要用规范化 JSON（键排序、无空格）算 sha256 取前 16 位十六进制
    ——足以判「一样/不一样」，不足以反推值。

    两侧对账必须用**同一个**函数，所以它住在公共层，接入层直接调用它。
    """
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def capability_to_wire(assignment: ProjectCapability) -> dict[str, Any]:
    """本地能力赋值 → wire dict（v1.0 §11.1 `project_capabilities` 的一行）。"""
    return assignment.model_dump(mode="json", by_alias=True)


def effective_capabilities_to_wire(
    effective: EffectiveCapabilities,
    *,
    ancestry: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolver 输出 → wire dict。

    每一条都回答 v1.0 §5.2 的「为什么生效」：来源 Project、是否继承而来、
    是否在链上被更近的节点覆盖过（`overridden`）、链上有哪些节点贡献过。
    被阻断的条目单独列在 `blocked` 里，附阻断它的 Project（AD-06 位置式传播）。
    """
    return {
        "projectId": effective.project_id,
        "backendKey": effective.backend_key,
        "ancestry": list(ancestry),
        "entries": [_effective_entry_to_wire(entry) for entry in effective.entries],
        "blocked": [_blocked_entry_to_wire(blocked) for blocked in effective.blocked],
        "counts": {
            "entries": len(effective.entries),
            "blocked": len(effective.blocked),
        },
    }


def _effective_entry_to_wire(entry: Any) -> dict[str, Any]:
    return {
        "capabilityType": entry.capability_type,
        "capabilityId": entry.capability_id,
        "config": entry.config,
        "version": entry.version,
        "sourceProjectId": entry.source_project_id,
        "inherited": entry.inherited,
        "overridden": len(entry.contributing_project_ids) > 1,
        "contributingProjectIds": list(entry.contributing_project_ids),
        "blocked": False,
    }


def _blocked_entry_to_wire(blocked: Any) -> dict[str, Any]:
    return {
        "capabilityType": blocked.capability_type,
        "capabilityId": blocked.capability_id,
        "blockedByProjectId": blocked.blocked_by_project_id,
        "blocked": True,
    }


def effective_capability_entry_to_wire(
    effective: EffectiveCapabilities, *, capability_type: str, capability_id: str
) -> dict[str, Any] | None:
    """`/effective-capabilities` 里**某一条**的形状（AD-144）。

    三种可能，形状与整份列表里的那一条**逐字相同**（写端点回的东西必须能和读端点
    对上，否则前端要为「写完之后」单独写一套解析）：生效 → entries 里的那条；
    被阻断 → blocked 里的那条；两边都没有 → ``None``（这条能力在这个节点上既没有
    赋值也没有继承来，是一个如实的「没有」，不是空对象）。
    """
    entry = effective.get(capability_type, capability_id)
    if entry is not None:
        return _effective_entry_to_wire(entry)
    for blocked in effective.blocked:
        if (
            blocked.capability_type == capability_type
            and blocked.capability_id == capability_id
        ):
            return _blocked_entry_to_wire(blocked)
    return None


def normalize_project_id(value: str) -> str:
    """允许端点同时接受 ``project:<slug>`` 与裸 slug。"""
    return value if value.startswith("project:") else f"project:{value}"


def normalize_backend_key_id(value: str) -> str:
    """允许端点同时接受 ``backend:<key>`` 与裸 key。"""
    return value if value.startswith("backend:") else f"backend:{value}"


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiffSection:
    """一类对象的差异。三个差异桶互不重叠，外加一个「不算差异」的桶。

    ``domain_owned``（批次八第 2 件）装的是**领域库自有**的对象：它们由写端点
    直接创建，旧状态里本来就没有对应物，因此「期望侧没有它」不是差异，而是
    正常。把它们从 ``unexpected_in_db`` 里摘出来单独列，读者一眼看得出
    「这几条不是漂移，是新建的」；也因此 ``in_sync`` 不受它们影响。
    """

    missing_in_db: tuple[str, ...] = ()
    unexpected_in_db: tuple[str, ...] = ()
    changed: tuple[dict[str, Any], ...] = ()
    domain_owned: tuple[str, ...] = ()

    @property
    def in_sync(self) -> bool:
        return not (self.missing_in_db or self.unexpected_in_db or self.changed)

    def to_wire(self) -> dict[str, Any]:
        return {
            "inSync": self.in_sync,
            "missingInDb": list(self.missing_in_db),
            "unexpectedInDb": list(self.unexpected_in_db),
            "changed": [dict(entry) for entry in self.changed],
            "domainOwned": list(self.domain_owned),
        }


@dataclass(frozen=True)
class CapabilityDiffSection:
    """能力维度的差异：领域库算出的**有效能力** ↔ 现行引擎算出的有效配置。

    三个真差异桶（`missing_in_db` / `unexpected_in_db` / `changed`）之外，多一个
    `known_divergences` 桶：期望侧的行可以自带一条 `known_divergence` 说明，
    命中时该行不算差异，而是被单独列出来并附上原因。公共层不知道这些原因是什么
    ——原因字符串由接入层给（它才认识旧引擎的语义），这里只负责分桶与呈现。

    **值不出现在输出里**，只有摘要（`digest`）：§5.4 要求 Drift Diff 脱敏，
    而这些键里恰好有 `providers` / `credential_pool_strategies`（R-06）。
    """

    missing_in_db: tuple[str, ...] = ()
    unexpected_in_db: tuple[str, ...] = ()
    changed: tuple[dict[str, Any], ...] = ()
    known_divergences: tuple[dict[str, Any], ...] = ()

    @property
    def in_sync(self) -> bool:
        return not (self.missing_in_db or self.unexpected_in_db or self.changed)

    def to_wire(self) -> dict[str, Any]:
        return {
            "inSync": self.in_sync,
            "missingInDb": list(self.missing_in_db),
            "unexpectedInDb": list(self.unexpected_in_db),
            "changed": [dict(entry) for entry in self.changed],
            "knownDivergences": [dict(entry) for entry in self.known_divergences],
        }


def build_capability_diff(
    *,
    expected_rows: Sequence[Mapping[str, Any]],
    actual_rows: Sequence[Mapping[str, Any]],
) -> CapabilityDiffSection:
    """比对两侧的有效能力行。

    行的形状（两侧一致）::

        {"id": "<project>|<type>|<capability_id>",
         "project_id": ..., "capability_type": ..., "capability_id": ...,
         "digest": "sha256:…",                 # 值的摘要，不是值
         "known_divergence": "…"}              # 仅期望侧、仅可选

    `id` 是自然键，因此两侧不需要共享任何 id 生成规则。
    """
    expected_by_id = {str(row["id"]): row for row in expected_rows}
    actual_by_id = {str(row["id"]): row for row in actual_rows}

    missing: list[str] = []
    known: list[dict[str, Any]] = []
    for identifier in sorted(set(expected_by_id) - set(actual_by_id)):
        row = expected_by_id[identifier]
        reason = row.get("known_divergence")
        if reason:
            known.append({"id": identifier, "side": "missingInDb", "reason": reason})
        else:
            missing.append(identifier)

    unexpected = tuple(sorted(set(actual_by_id) - set(expected_by_id)))

    changed: list[dict[str, Any]] = []
    for identifier in sorted(set(expected_by_id) & set(actual_by_id)):
        want = expected_by_id[identifier]
        got = actual_by_id[identifier]
        if want.get("digest") == got.get("digest"):
            continue
        entry = {
            "id": identifier,
            "expectedDigest": want.get("digest"),
            "actualDigest": got.get("digest"),
            "actualSourceProjectId": got.get("source_project_id"),
        }
        reason = want.get("known_divergence")
        if reason:
            known.append({**entry, "side": "changed", "reason": reason})
        else:
            changed.append(entry)

    return CapabilityDiffSection(
        missing_in_db=tuple(missing),
        unexpected_in_db=unexpected,
        changed=tuple(changed),
        known_divergences=tuple(known),
    )


@dataclass(frozen=True)
class DomainDiff:
    """旧 JSON 状态（期望）与领域库（实际）的完整差异。"""

    projects: DiffSection = field(default_factory=DiffSection)
    bindings: DiffSection = field(default_factory=DiffSection)
    capabilities: CapabilityDiffSection | None = None
    expected_counts: Mapping[str, int] = field(default_factory=dict)
    actual_counts: Mapping[str, int] = field(default_factory=dict)
    #: 接入层给的附加说明（例如「某些节点的原生配置文件还没收敛到有效值」）。
    capability_notes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        if not (self.projects.in_sync and self.bindings.in_sync):
            return False
        return self.capabilities is None or self.capabilities.in_sync

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "inSync": self.in_sync,
            "counts": {
                "expected": dict(self.expected_counts),
                "actual": dict(self.actual_counts),
            },
            "projects": self.projects.to_wire(),
            "bindings": self.bindings.to_wire(),
        }
        if self.capabilities is not None:
            payload["capabilities"] = self.capabilities.to_wire()
            if self.capability_notes:
                payload["capabilities"]["notes"] = dict(self.capability_notes)
        return payload


def _changed_fields(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    fields: Sequence[str],
) -> dict[str, dict[str, Any]]:
    delta: dict[str, dict[str, Any]] = {}
    for name in fields:
        if name not in expected:
            # 期望侧没给这个字段 = 不比对它（接入层可以只对账它负责的子集）。
            continue
        want = expected[name]
        got = actual.get(name)
        if want != got:
            delta[name] = {"expected": want, "actual": got}
    return delta


def _diff_section(
    expected_rows: Iterable[Mapping[str, Any]],
    actual_rows: Iterable[Mapping[str, Any]],
    fields: Sequence[str],
) -> DiffSection:
    expected_by_id = {str(row["id"]): row for row in expected_rows}
    actual_by_id = {str(row["id"]): row for row in actual_rows}

    missing = tuple(sorted(set(expected_by_id) - set(actual_by_id)))
    # 实际侧多出来的行分两种：真漂移，和领域库自有的（`origin == "domain"`）。
    # 后者旧状态里本来就没有，不该被报成 unexpected。
    surplus = sorted(set(actual_by_id) - set(expected_by_id))
    domain_owned = tuple(
        identifier
        for identifier in surplus
        if actual_by_id[identifier].get("origin") == "domain"
    )
    unexpected = tuple(
        identifier for identifier in surplus if identifier not in set(domain_owned)
    )
    changed: list[dict[str, Any]] = []
    for identifier in sorted(set(expected_by_id) & set(actual_by_id)):
        delta = _changed_fields(expected_by_id[identifier], actual_by_id[identifier], fields)
        if delta:
            changed.append({"id": identifier, "fields": delta})
    return DiffSection(
        missing_in_db=missing,
        unexpected_in_db=unexpected,
        changed=tuple(changed),
        domain_owned=domain_owned,
    )


def _project_actual_row(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "slug": project.slug,
        "display_name": project.display_name,
        "parent_project_id": project.parent_project_id,
        "status": project.status,
        "metadata": project.metadata,
    }


def _binding_actual_row(binding: AgentBinding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "backend_id": binding.backend_id,
        "display_name": binding.display_name,
        "native_scope_ref": binding.native_scope_ref,
        "enabled": binding.enabled,
        "is_default": binding.is_default,
        "runtime_config": binding.runtime_config,
        # 不在 COMPARED_BINDING_FIELDS 里：origin 不参与逐字段比对，
        # 它只决定「实际侧多出来的这一行算不算差异」。
        "origin": binding.origin,
    }


def build_domain_diff(
    *,
    expected_projects: Sequence[Mapping[str, Any]],
    expected_bindings: Sequence[Mapping[str, Any]],
    actual_projects: Sequence[Project],
    actual_bindings: Sequence[AgentBinding],
    expected_capabilities: Sequence[Mapping[str, Any]] | None = None,
    actual_capabilities: Sequence[Mapping[str, Any]] | None = None,
    capability_notes: Mapping[str, Any] | None = None,
) -> DomainDiff:
    """比对期望快照与领域库现状，返回可审计的差异清单。

    只做集合与逐字段比对，不做任何修复动作——Phase 1 的验收项是「旧 JSON 与新
    DB 的双读差异**可审计**」，修复由重新运行迁移器完成。
    """
    capabilities = None
    if expected_capabilities is not None and actual_capabilities is not None:
        capabilities = build_capability_diff(
            expected_rows=expected_capabilities, actual_rows=actual_capabilities
        )
    expected_counts = {
        "projects": len(expected_projects),
        "bindings": len(expected_bindings),
    }
    actual_counts = {
        "projects": len(actual_projects),
        "bindings": len(actual_bindings),
    }
    if capabilities is not None:
        expected_counts["effectiveCapabilities"] = len(expected_capabilities or ())
        actual_counts["effectiveCapabilities"] = len(actual_capabilities or ())
    return DomainDiff(
        projects=_diff_section(
            expected_projects,
            [_project_actual_row(p) for p in actual_projects],
            COMPARED_PROJECT_FIELDS,
        ),
        bindings=_diff_section(
            expected_bindings,
            [_binding_actual_row(b) for b in actual_bindings],
            COMPARED_BINDING_FIELDS,
        ),
        capabilities=capabilities,
        expected_counts=expected_counts,
        actual_counts=actual_counts,
        capability_notes=dict(capability_notes or {}),
    )


__all__ = [
    "COMPARED_BINDING_FIELDS",
    "COMPARED_PROJECT_FIELDS",
    "CapabilityDiffSection",
    "DiffSection",
    "DomainDiff",
    "backend_to_wire",
    "binding_to_wire",
    "build_capability_diff",
    "build_domain_diff",
    "capability_to_wire",
    "effective_capability_entry_to_wire",
    "capability_value_digest",
    "effective_capabilities_to_wire",
    "normalize_backend_key_id",
    "normalize_project_id",
    "project_to_wire",
]
