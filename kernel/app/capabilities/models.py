"""能力类型、能力赋值与有效能力的领域模型。

职责
----
1. 定义 **capability type** 的两种形态（R-01）：
   - 通用能力：``skills`` / ``mcp`` / ``instructions`` / ``plugins`` …
   - backend-scoped 能力：``<backend-key>:<name>``，例如某 Backend 的
     ``<backend>:runtime-config`` / ``<backend>:default-model``。
   两者共用同一套 Registry 与 Resolver，只在 Projector 落盘时按 backend 过滤。
2. 定义 :class:`ProjectCapability`（一条能力赋值，对应 v1.0 §11.1
   ``project_capabilities`` 表）与 :class:`EffectiveCapabilities`（Resolver 输出）。

对应规范
--------
- R-01：16 个继承键中不属于通用项目能力的键、以及软继承的 ``model`` 默认值，
  建模为 backend-scoped capability type，进入同一 Capability Registry / Resolver
  参与树继承；Projector 按 backend 过滤后写入对应 Binding 的有效配置。
  D-10 语义保持：Binding 的本地 default model = 未被祖先 scoped 配置覆盖时的取值。
- v1.0 §5.2：Effective = Ancestor Assignments + Local Assignments − Local Blocks
  + Type-specific Merge Rules；「不同类型不得使用完全相同的合并算法」。
- v1.0 §5.2：每条规则保留来源 Project，便于解释「为什么生效」。
- v1.0 §11.1：``assignment_mode`` ∈ {local, inherited-override, block}。
- N §3：能力类型名里出现的 backend key 是**数据**，公共层代码不得硬编码任何
  具体 backend 名字。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Final, Literal, Mapping, NamedTuple, Self

from pydantic import Field, model_validator

from app.base import DomainModel
from app.errors import CapabilityTypeError
from app.ids import (
    SEGMENT_PATTERN,
    CapabilityAssignmentId,
    ProjectId,
    capability_assignment_id,
)

AssignmentMode = Literal["local", "inherited-override", "block"]
"""v1.0 §11.1 ``project_capabilities.assignment_mode``。"""

#: R-01 与 v1.0 §5.2 列出的通用（backend 无关）能力类型。
#: 这不是封闭集合——:func:`parse_capability_type` 允许任何合法标识符作为通用类型，
#: 该常量只用于文档、UI 分组与断言。
GENERIC_CAPABILITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        "skills",
        "mcp",
        "instructions",
        # 事件钩子：v1.0 §5.2.1 把它和 mcp 并列为「能映射为通用项目能力」
        # 的那一部分，因此登记在通用类型里；具体钩子内容的形状由 Projector 翻译。
        "hooks",
        # 委派：AD-46 改判把它从 backend-scoped 提为通用类型——配置只是「上限与
        # 默认值」（是否允许 / 最大嵌套 / 最大并发 / 子 agent 默认模型 / 超时），
        # 这五项在任何能派子 agent 的引擎上都成立。策略 schema 见
        # :mod:`app.capabilities.delegation`；某个 Backend 私有的额外委派配置
        # 落 ``<backend-key>:delegation-extras``，不进这里。
        "delegation",
        "plugins",
        "policies",
        "artifacts",
        "workspace",
    }
)

_CAPABILITY_TYPE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:(?P<backend>{SEGMENT_PATTERN}):)?(?P<name>{SEGMENT_PATTERN})$"
)


class CapabilityTypeRef(NamedTuple):
    """解析后的能力类型：``backend_key`` 为 ``None`` 表示通用能力。"""

    backend_key: str | None
    name: str

    @property
    def is_backend_scoped(self) -> bool:
        return self.backend_key is not None

    def __str__(self) -> str:  # pragma: no cover - 直白
        return f"{self.backend_key}:{self.name}" if self.backend_key else self.name


def parse_capability_type(capability_type: str) -> CapabilityTypeRef:
    """把 ``skills`` / ``<backend>:runtime-config`` 解析为 :class:`CapabilityTypeRef`。"""
    match = _CAPABILITY_TYPE_RE.match(capability_type or "")
    if match is None:
        raise CapabilityTypeError(
            "能力类型必须是 <name> 或 <backend-key>:<name>（R-01），"
            f"实际收到 {capability_type!r}"
        )
    return CapabilityTypeRef(match.group("backend"), match.group("name"))


def is_backend_scoped(capability_type: str) -> bool:
    return parse_capability_type(capability_type).is_backend_scoped


def scoped_capability_type(backend_key: str, name: str) -> str:
    """构造 backend-scoped 能力类型字符串（R-01）。"""
    ref = parse_capability_type(f"{backend_key}:{name}")
    return str(ref)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ProjectCapability(DomainModel):
    """一条能力赋值（v1.0 §11.1 ``project_capabilities`` 的一行）。

    - ``capability_type``：见 :func:`parse_capability_type`（通用或 backend-scoped）。
    - ``capability_id``：该类型内部的条目键，例如某个 skill id、某个 MCP connection id；
      对 ``<backend>:runtime-config`` 这类「整份配置」型能力，惯例用固定键
      （如 ``default``），因为它没有天然的条目维度。
    - ``assignment_mode``：``block`` 表示在这个节点显式阻断该条目（v1.0 §5.2）。
    - ``source_project_id``：``inherited-override`` 时标注被覆盖的来源节点，
      用于解释「为什么生效」。
    """

    id: CapabilityAssignmentId
    project_id: ProjectId
    capability_type: str
    capability_id: str = Field(min_length=1)
    assignment_mode: AssignmentMode = "local"
    config: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None
    source_project_id: ProjectId | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_capability_type(self) -> Self:
        parse_capability_type(self.capability_type)
        if self.assignment_mode == "block" and self.config:
            raise ValueError("block 赋值不得携带 config（v1.0 §5.2：block 只是阻断）")
        return self

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        capability_type: str,
        capability_id: str,
        assignment_mode: AssignmentMode = "local",
        config: Mapping[str, Any] | None = None,
        version: str | None = None,
        source_project_id: str | None = None,
        assignment_uuid: str | None = None,
        created_at: datetime | None = None,
    ) -> ProjectCapability:
        timestamp = created_at or _now()
        return cls(
            id=capability_assignment_id(assignment_uuid),
            project_id=project_id,
            capability_type=capability_type,
            capability_id=capability_id,
            assignment_mode=assignment_mode,
            config=dict(config or {}),
            version=version,
            source_project_id=source_project_id,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def type_ref(self) -> CapabilityTypeRef:
        return parse_capability_type(self.capability_type)

    @property
    def key(self) -> tuple[str, str]:
        """Resolver 用的合并键：(capability_type, capability_id)。"""
        return (self.capability_type, self.capability_id)


class EffectiveCapability(DomainModel):
    """Resolver 输出的单条有效能力。"""

    capability_type: str
    capability_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None
    #: 最终生效值来自哪个 Project（v1.0 §5.2：保留来源，解释「为什么生效」）。
    source_project_id: ProjectId
    #: ``True`` 表示来源不是被解析的目标 Project 本身。
    inherited: bool
    #: 从根到目标、对这条能力有贡献（赋值或覆盖）的 Project，按树顺序。
    contributing_project_ids: tuple[ProjectId, ...] = ()

    @property
    def type_ref(self) -> CapabilityTypeRef:
        return parse_capability_type(self.capability_type)


class BlockedCapability(DomainModel):
    """被显式阻断、因而不出现在有效集合里的能力（v1.0 §5.2 Block）。"""

    capability_type: str
    capability_id: str
    blocked_by_project_id: ProjectId


class EffectiveCapabilities(DomainModel):
    """某个 Project（可选按 backend 过滤）的有效能力集合。

    R-01 的「树计算层」产出物：backend 无关的计算结果；``backend_key`` 非空时
    只是**过滤视图**（供某个 Binding 的 Projector 落盘用），计算规则完全相同。
    """

    project_id: ProjectId
    #: 非空表示这是给某个 backend 的过滤视图：只保留通用能力 + 该 backend 的 scoped 能力。
    backend_key: str | None = None
    entries: tuple[EffectiveCapability, ...] = ()
    blocked: tuple[BlockedCapability, ...] = ()
    #: 合并过程中被驳回的写法（当前只有 R-12 的单调安全合并会产生，AD-150）。
    #: 空元组 = 这次合并没有任何越界。**不是错误**：越界的字段保留了祖先值，
    #: 结果照常可用，这里只是把「你写的那个值没生效、为什么」说出来。
    warnings: tuple[str, ...] = ()

    def get(self, capability_type: str, capability_id: str) -> EffectiveCapability | None:
        for entry in self.entries:
            if entry.capability_type == capability_type and entry.capability_id == capability_id:
                return entry
        return None

    def by_type(self, capability_type: str) -> tuple[EffectiveCapability, ...]:
        return tuple(e for e in self.entries if e.capability_type == capability_type)

    def capability_types(self) -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.capability_type not in seen:
                seen.append(entry.capability_type)
        return tuple(seen)

    def generic(self) -> tuple[EffectiveCapability, ...]:
        return tuple(e for e in self.entries if not e.type_ref.is_backend_scoped)

    def scoped_for(self, backend_key: str) -> tuple[EffectiveCapability, ...]:
        """R-01：Projector 按 backend 过滤后写入对应 Binding 的有效配置。"""
        return tuple(e for e in self.entries if e.type_ref.backend_key == backend_key)

    def is_blocked(self, capability_type: str, capability_id: str) -> bool:
        return any(
            b.capability_type == capability_type and b.capability_id == capability_id
            for b in self.blocked
        )


__all__ = [
    "AssignmentMode",
    "BlockedCapability",
    "CapabilityTypeRef",
    "EffectiveCapabilities",
    "EffectiveCapability",
    "GENERIC_CAPABILITY_TYPES",
    "ProjectCapability",
    "is_backend_scoped",
    "parse_capability_type",
    "scoped_capability_type",
]
