"""Project / Backend / AgentBinding 领域模型。

职责
----
定义永久树的节点（Project）、Agent/Harness 接入类型（Backend）与某个 Project
对某个 Backend 的具体挂载（AgentBinding）。

对应规范
--------
- v1.0 §4.1 Project：id / slug / displayName / parentProjectId / workspaceRoot /
  status / metadata / createdAt / updatedAt；``workspaceRoot`` 可为空（抽象项目范围）。
- v1.0 §4.2 Backend：Agent/Harness 的接入类型，不是模型。
- v1.0 §4.3 AgentBinding：某 Project 对某 Backend 的挂载。
- v1.0 §11.2 ID 命名；「Native ID 永远单独保存」。
- N §5【替换】：``Backend`` 表示接入类型、``AgentBinding`` 表示 Project 侧的配置实例；
  Driver 分类用 ``driver_kind: acp | native | sdk | mock``（N §5.3），
  取代 v1.0 §4.2 的 ``adapterType``（裁决表 #6 采用 Driver 命名）。
- N §3 核心约束：公共层不得出现某个 Agent 的私有字段。因此 v1.0 §4.3 里那个
  以某一 Agent 的原生配置目录命名的字段，在此改名为 backend 中立的
  :attr:`AgentBinding.native_scope_ref` ——一个由 Driver 解释的不透明字符串。
  语义不变，命名不再泄露任何一家 Agent 的私有概念（改名理由见收尾报告未决问题）。
- R-05：``Project.slug`` 一经创建不可变，改名只动 ``display_name``。
- AD-28：``Backend.probe_state``（``unknown | available | unavailable | degraded``）
  是领域字段，由 Registry 的探测结果写回，``backends.probe_state`` 列不再留空。
- AD-127：探测失败不抹掉上一次成功探测的能力声明。``Backend.capabilities`` 原样
  保留，:class:`CapabilitySnapshot` 记下那次是什么时候采的，``probe_message``
  记下这次为什么没测到——「引擎离线」是一个事实，不是「能力全部未知」。
- R-09：同一 Project 可以挂多个同 Backend 的 Binding（twin 用例）；
  区分手段是 ``native_scope_ref`` + binding id 的第四段 discriminator，
  额外配置（如 twin 模式）进 ``runtime_config``。类型与 Repository 都不得阻止这一点。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.base import DomainModel
from app.errors import DomainInvariantError, SlugImmutableError
from app.ids import (
    BackendId,
    BackendKey,
    BindingId,
    ProjectId,
    ProjectSlug,
    backend_id,
    binding_id,
    normalize_backend_id,
    parse_binding_id,
    parse_project_id,
    project_id,
)
from runtime.capability_matrix import BackendCapabilities, DriverKind

ProjectStatus = Literal["active", "disabled", "draft"]
"""v1.0 §4.1。"""

CompatibilityState = Literal["ready", "partial", "blocked", "unknown"]
"""v1.0 §4.3。"""

BindingOrigin = Literal["imported", "domain"]
"""这条 Binding 是谁写的（批次八第 2 件）。

- ``imported``：一次性迁移器从旧状态推导出来的。它在**旧状态里有对应物**，
  因此 `/api/_domain/diff` 拿它跟期望侧逐字段对账，导入器也按计划收敛它。
- ``domain``：通过 `POST /api/projects/{id}/bindings` 在领域库里直接建的。
  旧状态里**没有**对应物，所以对账时它既不是 `missingInDb` 也不是
  `unexpectedInDb`——它是领域库自有的行，单独列在 `domainOwned` 里，
  导入器只报告、绝不删。

默认 ``imported``：这个字段之前不存在，库里已有的行全都是导入器写的。
"""

BackendProbeState = Literal["unknown", "available", "unavailable", "degraded"]
"""AD-28：Backend 的探测状态，公共领域字段。

v1.0 §11.1 的 ``backends`` 表一直有 ``probe_state`` 列，但公共 :class:`Backend`
没有对应字段，写入时只能置 NULL——「从没探测过」与「探测过、不可用」在库里
长得一模一样，UI 也就没法解释一个 Backend 为什么不能用。AD-28 把它提升为领域
字段：由 Registry 的探测结果写回（见 :func:`drivers.registry.probe_state_of`）。

取值刻意与 Driver 层的 :data:`drivers.base.ProbeState` **不同名**：
Driver 报的是「这次探测的结果」（``ready`` / ``degraded`` / ``unavailable``），
领域里记的是「这个 Backend 当前处于什么状态」，多一个 ``unknown`` 表示尚未探测过。
两者之间的映射是 Registry 的职责，不是让领域层去认 Driver 的词。
"""


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Project(DomainModel):
    """永久树中的项目节点（v1.0 §4.1）。

    R-05 的落地方式：模型整体冻结（赋值即 ``ValidationError``），并把
    ``id`` / ``slug`` 列为 :attr:`IMMUTABLE_FIELDS`，:meth:`evolve` 修改它们时
    抛 :class:`~app.errors.SlugImmutableError`。改名请用 :meth:`rename`。
    """

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"id", "slug"})
    IMMUTABLE_FIELD_ERROR: ClassVar[type[Exception]] = SlugImmutableError

    id: ProjectId
    slug: ProjectSlug
    display_name: str = Field(min_length=1)
    parent_project_id: ProjectId | None = None
    workspace_root: str | None = None
    status: ProjectStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_id_derives_from_slug(self) -> Self:
        if self.id != project_id(self.slug):
            raise ValueError(
                f"Project.id 必须由 slug 派生（v1.0 §11.2）：期望 {project_id(self.slug)!r}，实际 {self.id!r}"
            )
        if self.parent_project_id == self.id:
            raise ValueError("Project 不能是自己的父节点")
        return self

    @classmethod
    def create(
        cls,
        *,
        slug: str,
        display_name: str | None = None,
        parent_project_id: str | None = None,
        workspace_root: str | None = None,
        status: ProjectStatus = "active",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> Project:
        """按 slug 构造 Project，自动派生 ``project:<slug>``。"""
        timestamp = created_at or _now()
        return cls(
            id=project_id(slug),
            slug=slug,
            display_name=display_name or slug,
            parent_project_id=parent_project_id,
            workspace_root=workspace_root,
            status=status,
            metadata=dict(metadata or {}),
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def is_root(self) -> bool:
        return self.parent_project_id is None

    @property
    def has_workspace(self) -> bool:
        """C-5：无 ``workspace_root`` 的 Project 使用降级版 Workspace 页。"""
        return bool(self.workspace_root)

    def rename(self, display_name: str, *, at: datetime | None = None) -> Project:
        """R-05：改名只动 ``display_name``，slug 与 id 保持不变。"""
        return self.evolve(display_name=display_name, updated_at=at or _now())

    def parse_slug(self) -> str:
        return parse_project_id(self.id)


class CapabilitySnapshot(DomainModel):
    """AD-127：上一次**成功**探测拿到的能力声明 + 采集时间。

    为什么要单独存一份，而不是只留 :attr:`Backend.capabilities`
    -----------------------------------------------------------
    ``capabilities`` 回答「这个引擎有哪些能力」，快照回答「这个答案是什么时候
    问出来的」。探测失败时前者原样保留（引擎离线不会让已经验证过的能力失效），
    而 wire 需要后者才能如实说出「这是缓存，采于某时」——没有采集时间的话，
    一份旧结论和一份刚测的结论在界面上长得一模一样。

    从未成功探测过时整个快照是 ``None``：那时 ``capabilities`` 确实全是
    ``unknown``，也确实没有任何东西可缓存（N §13.1：不知道就说不知道）。
    """

    capabilities: BackendCapabilities
    captured_at: datetime


class Backend(DomainModel):
    """一种 Agent/Harness 的接入类型（v1.0 §4.2 + N §5）。

    注意：Backend 描述的是「接入类型」，不是模型、也不是某个 Project 的配置。
    某个 Project 对它的具体挂载是 :class:`AgentBinding`。
    """

    id: BackendId
    key: BackendKey
    display_name: str = Field(min_length=1)
    driver_kind: DriverKind
    installed: bool = False
    version: str | None = None
    driver_version: str | None = None
    capabilities: BackendCapabilities = Field(default_factory=BackendCapabilities)
    #: AD-28：最近一次探测的结论；从未探测过是 ``unknown``（不是空）。
    probe_state: BackendProbeState = "unknown"
    #: AD-127：最近一次探测留下的人话说明（探测失败时就是失败原因）。
    #: 没有说明就是 ``None``，不编一句「一切正常」。
    probe_message: str | None = None
    #: AD-127：上一次成功探测的能力快照。探测失败时 wire 读它标 ``cached``。
    capability_snapshot: CapabilitySnapshot | None = None
    last_probe_at: datetime | None = None

    @model_validator(mode="after")
    def _check_id_derives_from_key(self) -> Self:
        if self.id != backend_id(self.key):
            raise ValueError(
                f"Backend.id 必须是 backend:<key>（v1.0 §11.2）：期望 {backend_id(self.key)!r}，实际 {self.id!r}"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        key: str,
        display_name: str | None = None,
        driver_kind: DriverKind,
        installed: bool = False,
        version: str | None = None,
        driver_version: str | None = None,
        capabilities: BackendCapabilities | None = None,
        probe_state: BackendProbeState = "unknown",
        probe_message: str | None = None,
        capability_snapshot: CapabilitySnapshot | None = None,
        last_probe_at: datetime | None = None,
    ) -> Backend:
        return cls(
            id=backend_id(key),
            key=key,
            display_name=display_name or key,
            driver_kind=driver_kind,
            installed=installed,
            version=version,
            driver_version=driver_version,
            capabilities=capabilities or BackendCapabilities(),
            probe_state=probe_state,
            probe_message=probe_message,
            capability_snapshot=capability_snapshot,
            last_probe_at=last_probe_at,
        )


class AgentBinding(DomainModel):
    """某个 Project 对某个 Backend 的具体挂载（v1.0 §4.3 + N §9.2）。

    R-09：一个 Project 可以挂多条**同 Backend**的 Binding。区分依据：
    - ``native_scope_ref``：Driver 解释的不透明原生作用域标识（各 Binding 不同）；
    - binding id 的可选第四段 discriminator。

    ``runtime_config`` 承载 backend 私有的运行配置（例如 twin 模式标记），
    它是一个 opaque dict，公共层不解释其内容——这正是 N §5.2 路径 B
    「原生扩展必须隔离在 Driver 内」的存放位置。
    """

    id: BindingId
    project_id: ProjectId
    backend_id: BackendId
    display_name: str = Field(min_length=1)
    native_scope_ref: str | None = None
    enabled: bool = True
    is_default: bool = False
    default_model_id: str | None = None
    default_provider_id: str | None = None
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    compatibility_state: CompatibilityState = "unknown"
    #: 批次八第 2 件：谁写的这一行。见 :data:`BindingOrigin`。
    origin: BindingOrigin = "imported"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_id_consistency(self) -> Self:
        parts = parse_binding_id(self.id)
        if project_id(parts.project_slug) != self.project_id:
            raise ValueError(
                "AgentBinding.id 的 project 段必须与 project_id 一致"
                f"（v1.0 §11.2）：{self.id!r} vs {self.project_id!r}"
            )
        if backend_id(parts.backend_key) != self.backend_id:
            raise ValueError(
                "AgentBinding.id 的 backend 段必须与 backend_id 一致"
                f"（v1.0 §11.2）：{self.id!r} vs {self.backend_id!r}"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        project: Project | str,
        backend: Backend | str,
        display_name: str | None = None,
        discriminator: str | None = None,
        native_scope_ref: str | None = None,
        enabled: bool = True,
        is_default: bool = False,
        default_model_id: str | None = None,
        default_provider_id: str | None = None,
        runtime_config: dict[str, Any] | None = None,
        compatibility_state: CompatibilityState = "unknown",
        origin: BindingOrigin = "imported",
        created_at: datetime | None = None,
    ) -> AgentBinding:
        """构造 Binding。``discriminator`` 非空即产出 R-09 的四段式 id。"""
        project_slug = _project_slug_of(project)
        backend_key = _backend_key_of(backend)
        timestamp = created_at or _now()
        return cls(
            id=binding_id(project_slug, backend_key, discriminator),
            project_id=project_id(project_slug),
            backend_id=backend_id(backend_key),
            display_name=display_name or f"{backend_key}:{project_slug}",
            native_scope_ref=native_scope_ref,
            enabled=enabled,
            is_default=is_default,
            default_model_id=default_model_id,
            default_provider_id=default_provider_id,
            runtime_config=dict(runtime_config or {}),
            compatibility_state=compatibility_state,
            origin=origin,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def is_domain_owned(self) -> bool:
        """领域库自有（不是导入器写的）→ 对账与导入器都不该把它当多余的行。"""
        return self.origin == "domain"

    @property
    def discriminator(self) -> str | None:
        return parse_binding_id(self.id).discriminator

    @property
    def backend_key(self) -> str:
        return parse_binding_id(self.id).backend_key


def _project_slug_of(value: Project | str) -> str:
    """接受 Project、``project:<slug>`` 或裸 slug，统一返回 slug。"""
    if isinstance(value, Project):
        return value.slug
    return parse_project_id(value) if value.startswith("project:") else value


def _backend_key_of(value: Backend | str) -> str:
    """接受 Backend、``backend:<key>`` 或裸 key，统一返回 key。"""
    if isinstance(value, Backend):
        return value.key
    return normalize_backend_id(value).split(":", 1)[1]


def assert_binding_set_valid(bindings: "list[AgentBinding] | tuple[AgentBinding, ...]") -> None:
    """校验一组 Binding 的集合级不变量。

    刻意**不**校验「同一 Project 下 backend 唯一」——R-09 明确要求允许同 backend
    多 Binding。只校验：
    1. id 唯一；
    2. 同一 Project 下最多一个 ``is_default``（v1.0 §4.3 ``isDefault`` 语义）。
    """
    seen_ids: set[str] = set()
    defaults_by_project: dict[str, int] = {}
    for binding in bindings:
        if binding.id in seen_ids:
            raise DomainInvariantError(f"AgentBinding.id 重复：{binding.id!r}")
        seen_ids.add(binding.id)
        if binding.is_default:
            defaults_by_project[binding.project_id] = (
                defaults_by_project.get(binding.project_id, 0) + 1
            )
    over = [pid for pid, count in defaults_by_project.items() if count > 1]
    if over:
        raise DomainInvariantError(f"这些 Project 有多个默认 Binding：{sorted(over)}")


__all__ = [
    "AgentBinding",
    "Backend",
    "BackendProbeState",
    "BindingOrigin",
    "CapabilitySnapshot",
    "CompatibilityState",
    "Project",
    "ProjectStatus",
    "assert_binding_set_valid",
]
