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


def is_native_plugin_record(entry: Any) -> bool:
    """A backend-scoped ``<agent>:plugins`` row records a plugin the Agent installed itself."""
    ref = parse_capability_type(entry.capability_type)
    return ref.is_backend_scoped and ref.name == "plugins"


def without_native_plugins(effective: EffectiveCapabilities) -> EffectiveCapabilities:
    """Native plugin records never reach an adapter as library plugins: the Agent loads them
    itself. They move to ``native_plugins`` / ``native_plugins_blocked`` so a driver can
    leave the unassigned ones out of one launch, like native MCP."""
    if not any(is_native_plugin_record(e) for e in (*effective.entries, *effective.blocked)):
        return effective
    return effective.model_copy(update={
        "entries": tuple(e for e in effective.entries if not is_native_plugin_record(e)),
        "blocked": tuple(e for e in effective.blocked if not is_native_plugin_record(e)),
        "native_plugins": (*effective.native_plugins, *(e for e in effective.entries if is_native_plugin_record(e))),
        "native_plugins_blocked": (*effective.native_plugins_blocked,
                                   *(e for e in effective.blocked if is_native_plugin_record(e)))})


def without_native_mcp(effective: EffectiveCapabilities) -> EffectiveCapabilities:
    """Native MCP records never reach an adapter as project MCP.

    The source Agent already loads them from its own configuration, and their
    catalogue copy carries no credential values. Projecting them again would
    duplicate (or break) the server. Generic ``mcp`` rows are unaffected.

    The records are moved to ``native_mcp`` / ``native_mcp_blocked`` rather than
    dropped: a driver compares them with the Agent's actual native inventory to
    exclude the unassigned servers for one launch (design §3a, 2026-11-09).
    """
    if not any(is_native_mcp_record(e) for e in (*effective.entries, *effective.blocked)):
        return effective
    return effective.model_copy(update={
        "entries": tuple(e for e in effective.entries if not is_native_mcp_record(e)),
        "blocked": tuple(e for e in effective.blocked if not is_native_mcp_record(e)),
        "native_mcp": (*effective.native_mcp,
                       *(e for e in effective.entries if is_native_mcp_record(e))),
        "native_mcp_blocked": (*effective.native_mcp_blocked,
                               *(e for e in effective.blocked if is_native_mcp_record(e)))})


#: Library plugins: generic rows whose config is empty; the catalogue holds the location.
PLUGIN_CAPABILITY_TYPE = "plugins"


def with_plugin_locations(effective: EffectiveCapabilities, catalog: Any) -> EffectiveCapabilities:
    """Hydrate ``plugins`` rows with their library location for the adapter boundary (2026-11-09).

    The persistent row only says *which* plugin; the catalogue (``skill_tree.catalog.v1``,
    ``kind: "plugin"``) says where the installed package is and in which format.
    Drivers read the package from ``sourcePath``; a row with no catalogue item (or an
    unavailable package) keeps an empty config and is reported as not delivered.
    """
    if not any(e.capability_type == PLUGIN_CAPABILITY_TYPE for e in effective.entries):
        return effective
    by_key = {(item.get("capabilityType"), item.get("capabilityId")): item
              for item in (catalog or {}).values() if isinstance(item, dict)}
    entries = []
    for entry in effective.entries:
        if entry.capability_type != PLUGIN_CAPABILITY_TYPE:
            entries.append(entry)
            continue
        item = by_key.get((entry.capability_type, entry.capability_id))
        if not item or not item.get("sourcePath"):
            entries.append(entry)
            continue
        hydrated = {**entry.config, "name": item.get("name") or entry.capability_id,
                    "format": item.get("format"), "sourcePath": item["sourcePath"],
                    "libraryPath": item.get("libraryPath"), "available": item.get("available", True)}
        entries.append(entry.model_copy(update={"config": hydrated}))
    return effective.model_copy(update={"entries": tuple(entries)})


IMPORT_STATE_KEY = "skill_tree.import_state.v1"


async def _skill_scope(repositories: Any, backend_key: str) -> str:
    settings = getattr(repositories, "settings", None)
    scopes = await settings.get("skill_tree.backend_scopes.v1") if settings else None
    return (scopes or {}).get(backend_key, backend_key)


async def _import_state(repositories: Any, binding: Any) -> dict | None:
    """This Agent's entry in ``skill_tree.import_state.v1``; ``None`` before its explicit import."""
    settings = getattr(repositories, "settings", None)
    imports = await settings.get(IMPORT_STATE_KEY) if settings else None
    if not imports:
        return None
    if binding.backend_key in imports:
        return imports[binding.backend_key]
    return imports.get(await _skill_scope(repositories, binding.backend_key))


async def _tree_managed(repositories: Any, binding: Any) -> bool:
    """The Agent imports its own capabilities explicitly: only what is in the tree loads."""
    return await _import_state(repositories, binding) is not None


async def _shared_imports(repositories: Any, binding: Any, catalog: dict | None) -> tuple[EffectiveCapability, ...]:
    """Other Agents' imported capabilities assigned to this Project (design capability-import §2).

    Once imported they belong to the Kaus library and every Agent in the Project uses them:
    a Skill is selected from its library copy; an MCP server is read from its origin when the
    conversation starts (``nativeRef``, resolved in ``drivers.acp.mcp_projection``); a plugin
    package is delivered from where it is installed. This Agent's own imported records are
    already in its own scoped rows.
    """
    shared = {(item.get("capabilityType"), item.get("capabilityId")): item for item in (catalog or {}).values()
              if isinstance(item, dict) and item.get("shared") and not item.get("bound") and not item.get("importedAs")}
    if not shared:
        return ()
    own = {binding.backend_key, await _skill_scope(repositories, binding.backend_key)}
    full = await resolve_effective_for_project(repositories, binding.project_id)
    result = []
    for entry in full.entries:
        item = shared.get((entry.capability_type, entry.capability_id))
        ref = parse_capability_type(entry.capability_type)
        if item is None or not ref.is_backend_scoped or ref.backend_key in own:
            continue
        common = {"source_project_id": entry.source_project_id, "inherited": entry.inherited}
        if ref.name == "skills":
            result.append(entry.model_copy(update={"config": {**entry.config, "shared": True}}))
        elif ref.name == "mcp":
            result.append(EffectiveCapability(capability_type="mcp", capability_id="shared-" + str(item["id"]),
                config={"value": {"nativeRef": {key: item.get(key) for key in (
                    "name", "sourcePath", "sourceFormat", "sourceKey", "sourceShape")}}}, **common))
        elif ref.name == "plugins":
            result.append(EffectiveCapability(capability_type=PLUGIN_CAPABILITY_TYPE, capability_id=str(item["id"]),
                config={"name": item.get("name") or item["id"], "format": item.get("format"),
                        "sourcePath": item.get("sourcePath"), "available": item.get("available", True)}, **common))
    return tuple(result)


async def resolve_effective_for_binding(
    repositories: Any, binding: Any
) -> EffectiveCapabilities:
    """这条 Binding 的有效能力（带 ``backend_key`` 过滤，R-01）。"""
    effective = without_native_plugins(without_native_mcp(await _resolve_effective_for_binding(repositories, binding)))
    settings = getattr(repositories, "settings", None)
    catalog = await settings.get("skill_tree.catalog.v1") if settings else None

    def known(kind: str, suffix: str) -> tuple[str, ...]:
        return tuple(sorted({str(item.get("capabilityId")) for item in (catalog or {}).values()
                             if isinstance(item, dict) and item.get("kind") == kind and item.get("capabilityId")
                             and str(item.get("capabilityType", "")).endswith(suffix)}))
    update = {key: value for key, value in (("native_mcp_known", known("mcp", ":mcp")),
                                            ("native_plugins_known", known("plugin", ":plugins"))) if value}
    state = await _import_state(repositories, binding)
    if state is not None:
        update["tree_managed"] = True
        if ((state or {}).get("memory") or {}).get("bound") is True:
            update["native_memory_kept"] = True
    return effective.model_copy(update=update) if update else effective


async def _resolve_effective_for_binding(
    repositories: Any, binding: Any
) -> EffectiveCapabilities:
    frozen = binding.runtime_config.get("group_capabilities")
    if frozen is not None and binding.runtime_config.get("group_execution"):
        return EffectiveCapabilities.model_validate(frozen)
    effective = await resolve_effective_for_project(
        repositories, binding.project_id, backend_key=binding.backend_key
    )
    # Resolve opaque catalogue references only at the adapter boundary. Native
    # paths never become persistent capability config or general instructions.
    settings = getattr(repositories, "settings", None)
    catalog = await settings.get("skill_tree.catalog.v1") if settings else None
    effective = with_plugin_locations(effective, catalog)
    shared = await _shared_imports(repositories, binding, catalog)
    if shared:
        effective = effective.model_copy(update={"entries": (*effective.entries, *shared)})
    if binding.runtime_config.get("skill_mode") == "native":
        return effective
    managed = await settings.get("skill_tree.managed_projects.v1") if settings else None
    scopes = await settings.get("skill_tree.backend_scopes.v1") if settings else None
    skill_scope = (scopes or {}).get(binding.backend_key, binding.backend_key)
    # An Agent that imports explicitly always runs the managed range: what is not in the
    # tree does not load (design capability-import §2).
    forced = binding.runtime_config.get("skill_mode") == "managed" or await _tree_managed(repositories, binding)
    if managed:
        ancestry = await repositories.projects.ancestry(binding.project_id)
        forced = forced or any(
            any(key in managed.get(project.id, ()) for key in (binding.backend_key, skill_scope, "*"))
            for project in ancestry
        )
    if catalog is None and not forced:
        return effective
    by_key = {(item["capabilityType"], item["capabilityId"]): item for item in (catalog or {}).values()}
    # Skills inside a plugin the Agent installed itself follow that plugin as a whole
    # (2026-11-10): they are in scope exactly where the plugin is assigned, and any
    # older per-skill row for them is ignored.
    plugins_here = {entry.capability_id for entry in effective.entries if is_native_plugin_record(entry)}
    assets = []
    for entry in effective.entries:
        if entry.type_ref.name != "skills" or entry.capability_id == "ancestor-skill-dirs":
            continue
        item = by_key.get((entry.capability_type, entry.capability_id), {})
        if item.get("nativePluginId"):
            continue
        config = entry.config.get("value", entry.config)
        config = config if isinstance(config, dict) else entry.config
        assets.append({"id": item.get("id", entry.capability_id),
                       "name": item.get("name", entry.capability_id),
                       "capabilityType": entry.capability_type, "capabilityId": entry.capability_id,
                       "sourcePath": item.get("sourcePath") or config.get("sourcePath") or config.get("source_path") or config.get("path"),
                       "available": item.get("available", True),
                       **({"shared": True} if entry.config.get("shared") else {}),
                       # Imported Skills run from the Kaus library copy; drivers may link to it.
                       **({"stored": True} if item.get("importCopy") else {})})
    blocked = any(entry.capability_type.split(":")[-1] == "skills" and entry.capability_id != "ancestor-skill-dirs"
                  for entry in effective.blocked)
    if not assets and not blocked and not forced:
        return effective
    # Only once the skill range is managed anyway; an assigned plugin alone never
    # switches an Agent to managed selection.
    for item in (catalog or {}).values():
        if isinstance(item, dict) and item.get("nativePluginId") in plugins_here \
                and str(item.get("capabilityType", "")).split(":")[0] in {binding.backend_key, skill_scope}:
            assets.append({"id": item["id"], "name": item.get("name", item["id"]),
                           "capabilityType": item["capabilityType"], "capabilityId": item["capabilityId"],
                           "sourcePath": item.get("sourcePath"), "available": item.get("available", True)})
    selection = EffectiveCapability(capability_type="skill-selection", capability_id="default",
                                    config={"mode": "managed", "assets": assets,
                                            **({"scopeKey": skill_scope} if skill_scope != binding.backend_key else {})}, source_project_id=binding.project_id,
                                    inherited=False)
    return effective.model_copy(update={"entries": (*effective.entries, selection)})


__all__ = [
    "PLUGIN_CAPABILITY_TYPE",
    "is_native_mcp_record",
    "resolve_effective_for_binding",
    "resolve_effective_for_project",
    "with_plugin_locations",
    "without_native_mcp",
]
