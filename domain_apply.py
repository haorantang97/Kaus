#!/usr/bin/env python3
"""把只读迁移计划写进领域 SQLite（Phase 1「换地基」的唯一写入路径）。

位置说明
--------
本模块**不在** `kernel/app/` 里：它认识 Hermes 的旧概念（profile 名、twin），
属于接入层而不是公共层。公共层（`kernel/app/`）受
`kernel/tests/test_public_type_purity.py` 的私有名词扫描约束，任何一家 Agent
的私有名词都不得出现在那里；这类知识只能待在接入层，正是 N §3 的要求。

输入是 `scripts/migrate_profiles_readonly.py` 产出的计划 dict（schema
`hermes-migration-plan.v1`），输出是 `kernel/app/persistence` 的
`RepositorySet` 里的行。计划里的 `native_profile_id` 在这里翻译成公共层的
`AgentBinding.native_scope_ref`（AD-02：公共层字段名不得泄露某一 Agent 的私有
概念；计划 schema 保持原样，以免破坏 `--check` 的逐字节幂等）。

映射
----
=========================  ==================================================
计划字段                    领域对象
=========================  ==================================================
`projects[].id/slug`       `Project.id` / `Project.slug`（R-05 不变量）
`projects[].display_name`  `Project.display_name`（labels.json）
`projects[].parent_...`    `Project.parent_project_id`（hierarchy.json）
`projects[].ui_state`      `Project.metadata["ui_state"]`（§11.1 metadata_json）
`projects[].ui_state.draft` 额外落 `Project.status = "draft"`（§4.1 枚举）
`bindings[].id`            `AgentBinding.id`（AD-01 四段式，twin 出第四段）
`bindings[].native_...`    `AgentBinding.native_scope_ref`（AD-02）
`bindings[].twin_mode`     `AgentBinding.runtime_config["twin_mode"]`（AD-03）
=========================  ==================================================

不做的事
--------
- **不导入任何会话**（R-03）：计划里本就没有 conversation 对象，这里也不去读
  任何原生会话存储；
- **不删除**领域库里计划之外的 Project / Binding 行：删除是有损操作，Phase 1 只报告
  （`ApplyResult.stale_*`），由对账端点 / 报告驱动人工决定（AD-40）。

`project_capabilities`（AD-42）
------------------------------
16 个继承键 + 软继承的 `model` 由 `capability_import.py` 分类成能力行，本模块的
:func:`apply_capabilities` 负责落库。与 Project / Binding 的一个**有意差别**：
本导入器拥有的能力类型（`capability_import.OWNED_CAPABILITY_TYPES`）在计划内
Project 上的计划外行会被**删除**，而不是只报告。理由：能力行是纯派生数据，
用户把某个本地覆盖删掉后若残留一条 local 行，它会永远盖住根上的值，
「根上改一次、全树生效」当场失效——这正是 AD-42 要保住的验收点。
不属于本导入器的类型、以及计划外 Project 上的行，仍然只报告（`stale_capabilities`）。

幂等
----
每一行都先读回现状比对**内容**（不含时间戳）：完全相同 → 一次写都不发；
有差异 → 保留原 `created_at`，只更新 `updated_at`。因此重跑不会产生重复行，
也不会把未变更的行刷出新时间戳。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _root in (KERNEL_ROOT, REPO_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import capability_import  # noqa: E402
from app.capabilities.models import ProjectCapability  # noqa: E402
from app.persistence.base import RepositorySet  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402

#: 第一个 Backend（AD-18 / AD-32：走 HTTP+SSE API server，driver_kind = native）。
BACKEND_KEY = "hermes"
BACKEND_DISPLAY_NAME = "Hermes"
BACKEND_DRIVER_KIND = "native"

#: 计划 schema，写入前校验，避免把别的 JSON 当计划灌进去。
SUPPORTED_PLAN_SCHEMA = "hermes-migration-plan.v1"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class ApplyResult:
    """一次 apply 的结果（可打印、可断言、可作为报告）。"""

    backend_written: bool = False
    projects_created: list[str] = field(default_factory=list)
    projects_updated: list[str] = field(default_factory=list)
    projects_unchanged: list[str] = field(default_factory=list)
    bindings_created: list[str] = field(default_factory=list)
    bindings_updated: list[str] = field(default_factory=list)
    bindings_unchanged: list[str] = field(default_factory=list)
    stale_projects: list[str] = field(default_factory=list)
    stale_bindings: list[str] = field(default_factory=list)
    #: 批次八第 2 件：领域库自有的 Binding（`origin == "domain"`，由写端点建的）。
    #: 它们**不是**计划外遗留——旧状态里本来就没有它们。单独一个桶，
    #: 既不进 `stale_bindings`，也永远不会被这个导入器碰。
    domain_bindings: list[str] = field(default_factory=list)
    capabilities_created: list[str] = field(default_factory=list)
    capabilities_updated: list[str] = field(default_factory=list)
    capabilities_unchanged: list[str] = field(default_factory=list)
    capabilities_removed: list[str] = field(default_factory=list)
    stale_capabilities: list[str] = field(default_factory=list)
    redacted_credentials: int = 0

    @property
    def wrote_anything(self) -> bool:
        return bool(
            self.backend_written
            or self.projects_created
            or self.projects_updated
            or self.bindings_created
            or self.bindings_updated
            or self.capabilities_created
            or self.capabilities_updated
            or self.capabilities_removed
        )

    def summary(self) -> str:
        return (
            f"backend {'写入' if self.backend_written else '未变'}；"
            f"Project 新建 {len(self.projects_created)} / 更新 {len(self.projects_updated)}"
            f" / 未变 {len(self.projects_unchanged)}；"
            f"Binding 新建 {len(self.bindings_created)} / 更新 {len(self.bindings_updated)}"
            f" / 未变 {len(self.bindings_unchanged)}；"
            f"能力 新建 {len(self.capabilities_created)} / 更新 {len(self.capabilities_updated)}"
            f" / 未变 {len(self.capabilities_unchanged)} / 收敛删除 {len(self.capabilities_removed)}"
            f"（脱敏 {self.redacted_credentials} 个疑似凭据值）；"
            f"计划外遗留 Project {len(self.stale_projects)} / Binding {len(self.stale_bindings)}"
            f" / 能力 {len(self.stale_capabilities)}；"
            f"领域库自有 Binding {len(self.domain_bindings)}（不收敛）"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "backend_written": self.backend_written,
            "projects": {
                "created": sorted(self.projects_created),
                "updated": sorted(self.projects_updated),
                "unchanged": sorted(self.projects_unchanged),
                "stale": sorted(self.stale_projects),
            },
            "bindings": {
                "created": sorted(self.bindings_created),
                "updated": sorted(self.bindings_updated),
                "unchanged": sorted(self.bindings_unchanged),
                "stale": sorted(self.stale_bindings),
                "domainOwned": sorted(self.domain_bindings),
            },
            "capabilities": {
                "created": sorted(self.capabilities_created),
                "updated": sorted(self.capabilities_updated),
                "unchanged": sorted(self.capabilities_unchanged),
                "removed": sorted(self.capabilities_removed),
                "stale": sorted(self.stale_capabilities),
                "redactedCredentials": self.redacted_credentials,
            },
        }


# --------------------------------------------------------------------------- #
# 计划 → 领域对象（纯函数，便于对账端点复用同一份翻译）
# --------------------------------------------------------------------------- #


def project_metadata(ui_state: Mapping[str, Any] | None) -> dict[str, Any]:
    """§11.1：`killed` / `pinned` / `drafts` 落 `metadata_json`。

    刻意保持成一个**闭合的三键子对象**：读取端只认 `metadata["ui_state"]`，
    将来往 metadata 里加别的东西不会和它打架。
    """
    state = dict(ui_state or {})
    return {
        "ui_state": {
            "pinned": bool(state.get("pinned")),
            "killed": bool(state.get("killed")),
            "draft": bool(state.get("draft")),
        }
    }


def project_status(ui_state: Mapping[str, Any] | None) -> str:
    """草稿节点落 §4.1 的 `status = "draft"`；其余 `active`。

    `killed` **不**映射成 `disabled`：旧模型里 killed 是可级联的 UI 闸
    （`effective_killed` 在读取端派生），把它压进 status 会丢掉级联语义，
    也会让「取消 kill」变成改 status。killed 只留在 `metadata.ui_state`。
    """
    return "draft" if (ui_state or {}).get("draft") else "active"


def binding_runtime_config(plan_binding: Mapping[str, Any]) -> dict[str, Any]:
    """AD-03：twin 的 `twin_mode` 落 `runtime_config_json.twin_mode`。

    非 twin 的默认 Binding 得到空 dict——公共层不往里塞任何东西，这个字段是
    Driver 的私有出口（N §5.2 路径 B）。
    """
    if plan_binding.get("is_default"):
        return {}
    # twin Binding：即使模式标提取为空也写键，让「这是 twin」本身可查。
    return {"twin_mode": plan_binding.get("twin_mode")}


def desired_project(plan_project: Mapping[str, Any], *, at: datetime) -> Project:
    return Project(
        id=plan_project["id"],
        slug=plan_project["slug"],
        display_name=plan_project["display_name"],
        parent_project_id=plan_project.get("parent_project_id"),
        workspace_root=None,
        status=project_status(plan_project.get("ui_state")),
        metadata=project_metadata(plan_project.get("ui_state")),
        created_at=at,
        updated_at=at,
    )


def desired_binding(plan_binding: Mapping[str, Any], *, at: datetime) -> AgentBinding:
    native_scope_ref = plan_binding["native_profile_id"]
    return AgentBinding(
        id=plan_binding["id"],
        project_id=plan_binding["project_id"],
        backend_id=f"backend:{plan_binding['backend_id']}",
        # 显示名 = 原生作用域标识：默认 Binding 上等于 slug，twin 上是 twin 自己的名字，
        # 都是稳定的、可从计划确定性推导的值（AD-01 对幂等的要求）。
        display_name=native_scope_ref,
        native_scope_ref=native_scope_ref,
        enabled=True,
        is_default=bool(plan_binding.get("is_default")),
        default_model_id=None,
        default_provider_id=None,
        runtime_config=binding_runtime_config(plan_binding),
        compatibility_state="unknown",
        created_at=at,
        updated_at=at,
    )


def desired_backend(*, at: datetime | None = None) -> Backend:
    """Backend Registry 里的第一行。

    `installed` / `version` / `last_probe_at` 留空：探测是 Registry 的职责
    （AD-28 的 `probe_state` 同理），迁移器不假装自己探测过。
    """
    return Backend.create(
        key=BACKEND_KEY,
        display_name=BACKEND_DISPLAY_NAME,
        driver_kind=BACKEND_DRIVER_KIND,
        installed=False,
        last_probe_at=at,
    )


def expected_snapshot(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """计划 → 对账端点的「期望侧」快照（字段名与领域模型的蛇形字段一致）。

    与 :func:`desired_project` / :func:`desired_binding` 共用同一批映射函数，
    所以对账比的是「同一套规则算出来的期望」，不会因为两处翻译走样而假报差异。
    """
    projects = [
        {
            "id": p["id"],
            "slug": p["slug"],
            "display_name": p["display_name"],
            "parent_project_id": p.get("parent_project_id"),
            "status": project_status(p.get("ui_state")),
            "metadata": project_metadata(p.get("ui_state")),
        }
        for p in plan.get("projects", ())
    ]
    bindings = [
        {
            "id": b["id"],
            "project_id": b["project_id"],
            "backend_id": f"backend:{b['backend_id']}",
            "display_name": b["native_profile_id"],
            "native_scope_ref": b["native_profile_id"],
            "enabled": True,
            "is_default": bool(b.get("is_default")),
            "runtime_config": binding_runtime_config(b),
        }
        for b in plan.get("bindings", ())
    ]
    return {"projects": projects, "bindings": bindings}


# --------------------------------------------------------------------------- #
# 能力计划 → 领域对象（AD-42）
# --------------------------------------------------------------------------- #


def desired_capability(row: Mapping[str, Any], *, at: datetime) -> ProjectCapability:
    """能力计划的一行 → :class:`ProjectCapability`。

    `id` 由 `capability_import.assignment_uuid()` 的确定性 uuid5 得来，因此重跑
    拿到同一个 id —— 这是幂等（不产生重复行、不刷时间戳）的前提。
    """
    return ProjectCapability(
        id=f"capability:{row['assignment_uuid']}",
        project_id=row["project_id"],
        capability_type=row["capability_type"],
        capability_id=row["capability_id"],
        assignment_mode=row["assignment_mode"],
        config=dict(row.get("config") or {}),
        version=None,
        # `source_project_id` 留空：本地赋值的来源就是它自己，
        # `inherited-override` 的来源标注属于 Phase 5 有写端点之后的事。
        source_project_id=None,
        created_at=at,
        updated_at=at,
    )


async def apply_capabilities(
    repositories: RepositorySet,
    capability_plan: Mapping[str, Any],
    result: ApplyResult,
    *,
    at: datetime,
) -> None:
    """把能力计划写进 `project_capabilities`。幂等；收敛范围见模块 docstring。"""
    rows: Sequence[Mapping[str, Any]] = list(capability_plan.get("rows", ()))
    planned_projects = {str(p) for p in capability_plan.get("meta", {}).get("projects", ())}
    if not planned_projects:
        planned_projects = {str(row["project_id"]) for row in rows}
    result.redacted_credentials = sum(
        len(entry.get("paths", ())) for entry in capability_plan.get("redactions", ())
    )

    desired_by_id: dict[str, ProjectCapability] = {}
    for row in rows:
        assignment = desired_capability(row, at=at)
        desired_by_id[assignment.id] = assignment

    for assignment_id in sorted(desired_by_id):
        desired = desired_by_id[assignment_id]
        existing = await repositories.capabilities.get(desired.id)
        if existing is None:
            await repositories.capabilities.save(desired)
            result.capabilities_created.append(desired.id)
        elif _content(existing) != _content(desired):
            await repositories.capabilities.save(
                desired.evolve(created_at=existing.created_at, updated_at=at)
            )
            result.capabilities_updated.append(desired.id)
        else:
            result.capabilities_unchanged.append(desired.id)

    # 收敛：本导入器拥有的类型 + 计划内 Project，计划里没有的行删掉；其余只报告。
    owned = capability_import.OWNED_CAPABILITY_TYPES
    for project in await repositories.projects.list_all():
        for existing in await repositories.capabilities.list_for_project(project.id):
            if existing.id in desired_by_id:
                continue
            if existing.capability_type in owned and project.id in planned_projects:
                await repositories.capabilities.delete(existing.id)
                result.capabilities_removed.append(existing.id)
            else:
                result.stale_capabilities.append(existing.id)


async def ancestry_by_project(repositories: RepositorySet) -> dict[str, list[str]]:
    """``project_id -> 从根到自身的 id 链``。对账端点算祖先 Block 影响面时用。"""
    out: dict[str, list[str]] = {}
    for project in await repositories.projects.list_all():
        out[project.id] = [p.id for p in await repositories.projects.ancestry(project.id)]
    return out


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def _content(model: Project | AgentBinding | Backend | ProjectCapability) -> dict[str, Any]:
    """比对用的内容视图：去掉时间戳，其余全比。"""
    data = model.model_dump(mode="json", by_alias=False)
    for key in ("created_at", "updated_at", "last_probe_at"):
        data.pop(key, None)
    return data


async def apply_plan(
    repositories: RepositorySet,
    plan: Mapping[str, Any],
    *,
    at: datetime | None = None,
    capability_plan: Mapping[str, Any] | None = None,
) -> ApplyResult:
    """把计划写进领域库。幂等：内容相同则一次写都不发。

    `capability_plan` 是 `capability_import.build_capability_plan(...).to_json()`
    的产物；给了就一并写 `project_capabilities`（AD-42），不给就完全跳过能力维度
    （老调用方的行为一字不变）。
    """
    schema = plan.get("schema_version")
    if schema != SUPPORTED_PLAN_SCHEMA:
        raise ValueError(
            f"不认识的迁移计划 schema：{schema!r}（期望 {SUPPORTED_PLAN_SCHEMA!r}）"
        )
    if any(key in plan for key in ("conversations", "sessions")):
        # R-03 的硬闸：计划里出现会话对象说明输入不是本阶段该用的东西。
        raise ValueError("R-03：Phase 1 不导入任何存量会话，计划中不得含会话对象")

    timestamp = at or _now()
    result = ApplyResult()

    backend = desired_backend()
    existing_backend = await repositories.backends.get(backend.id)
    if existing_backend is None or _content(existing_backend) != _content(backend):
        await repositories.backends.save(backend)
        result.backend_written = True

    plan_projects: Sequence[Mapping[str, Any]] = list(plan.get("projects", ()))
    plan_bindings: Sequence[Mapping[str, Any]] = list(plan.get("bindings", ()))

    # 先按父级深度排序写入，避免中间态出现指向尚不存在的父 Project 的行。
    for plan_project in _in_parent_order(plan_projects):
        desired = desired_project(plan_project, at=timestamp)
        existing = await repositories.projects.get(desired.id)
        if existing is None:
            await repositories.projects.save(desired)
            result.projects_created.append(desired.id)
        elif _content(existing) != _content(desired):
            await repositories.projects.save(
                desired.evolve(created_at=existing.created_at, updated_at=timestamp)
            )
            result.projects_updated.append(desired.id)
        else:
            result.projects_unchanged.append(desired.id)

    # 默认 Binding 先写：`is_default` 在同一 Project 内唯一，先写非默认再写默认
    # 不会冲突，但反过来若某条旧行占着默认位就会撞闸——按 id 排序即可让
    # `binding:<slug>:<backend>`（三段）排在 `...:<discriminator>`（四段）之前。
    for plan_binding in sorted(plan_bindings, key=lambda b: b["id"]):
        desired = desired_binding(plan_binding, at=timestamp)
        existing = await repositories.bindings.get(desired.id)
        if existing is None:
            await repositories.bindings.save(desired)
            result.bindings_created.append(desired.id)
        elif _content(existing) != _content(desired):
            await repositories.bindings.save(
                desired.evolve(created_at=existing.created_at, updated_at=timestamp)
            )
            result.bindings_updated.append(desired.id)
        else:
            result.bindings_unchanged.append(desired.id)

    # 能力行在 Project 之后写：block/local 行都挂在已经存在的 Project 上。
    if capability_plan is not None:
        schema = capability_plan.get("schema_version")
        if schema != capability_import.CAPABILITY_PLAN_SCHEMA:
            raise ValueError(
                f"不认识的能力计划 schema：{schema!r}"
                f"（期望 {capability_import.CAPABILITY_PLAN_SCHEMA!r}）"
            )
        await apply_capabilities(repositories, capability_plan, result, at=timestamp)

    planned_project_ids = {p["id"] for p in plan_projects}
    planned_binding_ids = {b["id"] for b in plan_bindings}
    for project in await repositories.projects.list_all():
        if project.id not in planned_project_ids:
            result.stale_projects.append(project.id)
        for binding in await repositories.bindings.list_for_project(project.id):
            if binding.id in planned_binding_ids:
                continue
            if binding.is_domain_owned:
                # 写端点建的行：计划里没有它是**正常**的，不是漂移。
                result.domain_bindings.append(binding.id)
            else:
                result.stale_bindings.append(binding.id)
    return result


def _in_parent_order(
    plan_projects: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """按「父先于子」排序；同层按 id 保证确定性。环不可能出现（计划器已断环）。"""
    by_id = {p["id"]: p for p in plan_projects}
    depth: dict[str, int] = {}

    def depth_of(identifier: str, seen: frozenset[str] = frozenset()) -> int:
        if identifier in depth:
            return depth[identifier]
        row = by_id.get(identifier)
        parent = row.get("parent_project_id") if row else None
        if row is None or parent is None or parent not in by_id or identifier in seen:
            value = 0
        else:
            value = depth_of(parent, seen | {identifier}) + 1
        depth[identifier] = value
        return value

    return sorted(plan_projects, key=lambda p: (depth_of(p["id"]), p["id"]))


__all__ = [
    "BACKEND_DRIVER_KIND",
    "BACKEND_KEY",
    "SUPPORTED_PLAN_SCHEMA",
    "ApplyResult",
    "ancestry_by_project",
    "apply_capabilities",
    "apply_plan",
    "binding_runtime_config",
    "desired_backend",
    "desired_binding",
    "desired_capability",
    "desired_project",
    "expected_snapshot",
    "project_metadata",
    "project_status",
]
