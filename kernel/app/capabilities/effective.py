"""「这个 Project / 这条 Binding 的有效能力是什么」——**唯一**的一处算法入口。

为什么单独抽出来
----------------
在批次四十三之前，这段「展开祖先链 → 取赋值表 → 跑 Resolver」的三行分别写在
``binding_router._effective_of``（带 ``backend_key`` 过滤）与
``session_router._effective_entry``（不带过滤）里，各自 import 一次 Resolver。
本批要加第三个调用方（Session Host 建会话时要拿这条 Binding 的有效 MCP），
再抄一遍就是第三份可以各自走样的账——v1.0 §5.2 的「每条规则保留来源 Project」
之所以成立，前提是**全站只有一个算法**。

两个函数、一条算法
------------------
- :func:`resolve_effective_for_project`：不带 backend 过滤的全量视图。写端点回
  「这条能力最终是什么」用它（能力本身是 backend 无关的，R-01 树计算层）。
- :func:`resolve_effective_for_binding`：带 ``backend_key`` 过滤（R-01）——物化 /
  投射的目标是**这一家**引擎，别家的 scoped 能力不该出现在它的变更集里。

本模块只做编排（读仓储 + 调 Resolver），合并规则一个字都不在这里，
全在 :mod:`app.capabilities.resolver`。
"""

from __future__ import annotations

from typing import Any

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability, parse_capability_type
from app.capabilities.resolver import (
    CapabilityResolutionRequest,
    TreeCapabilityResolver,
)


async def resolve_effective_for_project(
    repositories: Any, project_id: str, *, backend_key: str | None = None
) -> EffectiveCapabilities:
    """展开祖先链 → 取赋值表 → 跑 Resolver。

    ``repositories`` 只用到 ``projects.ancestry`` 与
    ``capabilities.list_for_projects`` 两个方法，所以这里不绑死
    :class:`~app.persistence.base.RepositorySet` 的具体类型。
    """
    ancestry = [p.id for p in await repositories.projects.ancestry(project_id)]
    assignments = await repositories.capabilities.list_for_projects(ancestry)
    settings = getattr(repositories, "settings", None)
    scopes = await settings.get("skill_tree.backend_scopes.v1") if settings and backend_key else None
    skill_scope = (scopes or {}).get(backend_key, backend_key)
    aliases = skill_scope != backend_key
    result = TreeCapabilityResolver().resolve(
        CapabilityResolutionRequest(
            ancestry=tuple(ancestry), backend_key=None if aliases else backend_key
        ),
        assignments,
    )
    if not aliases:
        return result
    # A transport can share skill ownership without sharing its private runtime
    # configuration format. The driver catalog declares this one narrow alias.
    def accepted(entry):
        scope, _, name = entry.capability_type.rpartition(":")
        return not scope or scope == backend_key or (name == "skills" and scope == skill_scope)
    return result.model_copy(update={"backend_key": backend_key,
        "entries": tuple(entry for entry in result.entries if accepted(entry)),
        "blocked": tuple(entry for entry in result.blocked if accepted(entry))})


def is_native_mcp_record(entry: Any) -> bool:
    """A backend-scoped ``<agent>:mcp`` row records an Agent's own native MCP server."""
    ref = parse_capability_type(entry.capability_type)
    return ref.is_backend_scoped and ref.name == "mcp"


def without_native_mcp(effective: EffectiveCapabilities) -> EffectiveCapabilities:
    """Native MCP records never reach an adapter.

    The source Agent already loads them from its own configuration, and their
    catalogue copy carries no credential values. Projecting them again would
    duplicate (or break) the server. Generic ``mcp`` rows are unaffected.
    """
    if not any(is_native_mcp_record(e) for e in (*effective.entries, *effective.blocked)):
        return effective
    return effective.model_copy(update={
        "entries": tuple(e for e in effective.entries if not is_native_mcp_record(e)),
        "blocked": tuple(e for e in effective.blocked if not is_native_mcp_record(e))})


async def resolve_effective_for_binding(
    repositories: Any, binding: Any
) -> EffectiveCapabilities:
    """这条 Binding 的有效能力（带 ``backend_key`` 过滤，R-01）。"""
    return without_native_mcp(await _resolve_effective_for_binding(repositories, binding))


async def _resolve_effective_for_binding(
    repositories: Any, binding: Any
) -> EffectiveCapabilities:
    frozen = binding.runtime_config.get("group_capabilities")
    if frozen is not None and binding.runtime_config.get("group_execution"):
        return EffectiveCapabilities.model_validate(frozen)
    effective = await resolve_effective_for_project(
        repositories, binding.project_id, backend_key=binding.backend_key
    )
    if binding.runtime_config.get("skill_mode") == "native":
        return effective
    # Resolve opaque catalogue references only at the adapter boundary. Native
    # paths never become persistent capability config or general instructions.
    settings = getattr(repositories, "settings", None)
    catalog = await settings.get("skill_tree.catalog.v1") if settings else None
    managed = await settings.get("skill_tree.managed_projects.v1") if settings else None
    scopes = await settings.get("skill_tree.backend_scopes.v1") if settings else None
    skill_scope = (scopes or {}).get(binding.backend_key, binding.backend_key)
    forced = binding.runtime_config.get("skill_mode") == "managed"
    if managed:
        ancestry = await repositories.projects.ancestry(binding.project_id)
        forced = forced or any(
            any(key in managed.get(project.id, ()) for key in (binding.backend_key, skill_scope, "*"))
            for project in ancestry
        )
    if catalog is None and not forced:
        return effective
    by_key = {(item["capabilityType"], item["capabilityId"]): item for item in (catalog or {}).values()}
    assets = []
    for entry in effective.entries:
        if entry.type_ref.name != "skills" or entry.capability_id == "ancestor-skill-dirs":
            continue
        item = by_key.get((entry.capability_type, entry.capability_id), {})
        config = entry.config.get("value", entry.config)
        config = config if isinstance(config, dict) else entry.config
        assets.append({"id": item.get("id", entry.capability_id),
                       "name": item.get("name", entry.capability_id),
                       "capabilityType": entry.capability_type, "capabilityId": entry.capability_id,
                       "sourcePath": item.get("sourcePath") or config.get("sourcePath") or config.get("source_path") or config.get("path"),
                       "available": item.get("available", True)})
    blocked = any(entry.capability_type.split(":")[-1] == "skills" and entry.capability_id != "ancestor-skill-dirs"
                  for entry in effective.blocked)
    if not assets and not blocked and not forced:
        return effective
    selection = EffectiveCapability(capability_type="skill-selection", capability_id="default",
                                    config={"mode": "managed", "assets": assets,
                                            **({"scopeKey": skill_scope} if skill_scope != binding.backend_key else {})}, source_project_id=binding.project_id,
                                    inherited=False)
    return effective.model_copy(update={"entries": (*effective.entries, selection)})


__all__ = [
    "is_native_mcp_record",
    "resolve_effective_for_binding",
    "resolve_effective_for_project",
    "without_native_mcp",
]
