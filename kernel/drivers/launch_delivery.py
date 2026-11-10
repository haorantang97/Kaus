"""**开对话前的自动送达**：项目指令与原生 MCP 排除，各按 Agent 最稳的通道（2026-11-09）。

设计稿 ``docs/product/project-config-generalization-20261102.md`` 第 3 节与第 3a 节。
这一层仍然**不认识任何引擎名**：通道由适配器模块（预设的 ``runtime_adapter`` /
``skill_projection_adapter``）用两个**可选**模块级函数声明，没声明就是没有这条路。

适配器可选钩子
--------------
``deliver_instructions(spec, metadata, body) -> (spec, metadata)``
    启动时注入，不碰工作目录文件（设计稿第 3 节第 1 档）。有这个函数的 Agent 走
    这条路，没有的才写工作目录指令文件的受管块（第 2 档）。

``exclude_native_mcp(spec, metadata, *, excluded, assigned, cwd) -> (spec, metadata, warnings)``
    只在本次启动中排除「该 Agent 自带、但未分配给当前 Project」的 MCP（第 3a 节）。
    ``excluded`` / ``assigned`` 是清单记录（``drivers.mcp_inventory`` 的形状：
    ``name`` / ``sourcePath`` / ``server``，不含任何凭据值）。不改用户配置文件、
    不落盘任何密钥。

``NATIVE_MCP_EXCLUSION_VERIFIED: bool``
    这条排除机制有没有在真实 Agent 上跑通过。缺省 ``False``：报告里把这一组标成
    「未验证」，而不是装成已隔离（AD-71）。

``disable_native_memory(spec, metadata) -> (spec, metadata)``
    只在本次启动中关掉该 Agent 自带的记忆（设计稿第 5 节「改向」）：Project 记忆
    （``kaus-memory`` MCP）在场时，不让两份记忆并存。不改用户全局配置。只有有效能力里
    带着 Project 记忆 MCP 时才调用——组内成员会话没有 Project 记忆，原生行为照旧。

``NATIVE_MEMORY_DISABLE_VERIFIED: bool``
    同上，缺省 ``False`` → ``nativeMemory`` 报 ``unverified``。

``HAS_NATIVE_MEMORY: bool``
    适配器声明「这个 Agent 根本没有自带记忆」（``False``）时报 ``none``；没有钩子也没有
    这条声明的，一律按「自带记忆未接管」报（``active`` + ``native_memory_active``）。

``NATIVE_PLUGIN_FORMATS: tuple[str, ...]`` + ``deliver_plugins(spec, metadata, plugins) -> (spec, metadata)``
    该 Agent 原生能加载哪些插件格式（``claude`` / ``codex`` / ``gemini``，见
    ``drivers.plugin_packages``），以及怎样在**本次启动**里把一个已安装的包原样交给它
    （设计稿第 6 节）。``plugins`` 是 :func:`expand_plugins` 算出的记录（``id`` / ``name`` /
    ``format`` / ``path`` / ``root`` / ``skills``）。不是原生格式、或没有这个钩子的，走
    :func:`expand_plugins` 的通用拆解：技能目录并入托管技能投射，MCP 作为 Project MCP；
    hooks / 命令 / 子 agent 报 ``plugin_parts_unsupported``。

``NATIVE_PLUGIN_DELIVERY_VERIFIED: bool``
    同上，缺省 ``False`` → ``summary["pluginNative"]`` 报 ``unverified``。

``exclude_native_plugins(spec, metadata, *, excluded, kept) -> (spec, metadata, warnings)``
    Agent 自己装的插件（``drivers.native_plugins``）：本次不加载 ``excluded``，``kept`` 照常
    （只加载传入 MCP 的模式下要显式带上它们的 MCP）。用户没整理过就不调用。

两个阶段
--------
1. :func:`plan_launch`——在 ``session_options_for`` 里算：拼正文、选通道、必要时
   **现在就**写受管块、算排除名单、把送不出去的部分报进 ``unapplied``（D-03）。
2. :func:`apply_launch_plan`——在 ``start_runtime`` 里把计划落到这一次进程的
   ``spec``（argv / env）与 ``metadata``（``_meta``）上。两步分开是因为 ``effective``
   只在第 1 步手上，而真正的进程规格（技能副本、组长隔离）要到第 2 步才定下来。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from drivers.base import UnappliedCapability, WorkspaceRequiredError, WORKSPACE_REQUIRED, FailureHint
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
    is_auxiliary,
)
from drivers.projection_gaps import INSTRUCTIONS, INVALID_CONFIG, MCP, PLUGINS, UNSUPPORTED
from drivers.workspace_projection import REASON_NO_WORKSPACE, materialize_block, resolve_directory

#: 送达通道。
CHANNEL_LAUNCH: Final[str] = "launch"
CHANNEL_WORKSPACE_FILE: Final[str] = "workspace_file"

#: ``unapplied`` 里的原因码（前端按码本地化）。
#: 已写进文件，但同一目录另有会话在跑——那条会话的进程读的是旧文件，重启后才生效。
REASON_WORKSPACE_IN_USE: Final[str] = "workspace_in_use"
#: 该 Agent 自带的这些 MCP 未分配给当前 Project，但它没有按次启动排除的机制。
REASON_NATIVE_NOT_ISOLATED: Final[str] = "native_not_isolated"
#: Project 记忆在场，但该 Agent 自带的记忆没有按次启动关掉的机制（「自带记忆未接管」）。
REASON_NATIVE_MEMORY_ACTIVE: Final[str] = "native_memory_active"

#: 插件（设计稿第 6 节）：拆解送达时包里的 hooks / 命令 / 子 agent 送不过去。
REASON_PLUGIN_PARTS_UNSUPPORTED: Final[str] = "plugin_parts_unsupported"
#: 插件在能力库里的包已不在（被手动删了、目录不可读），这次什么也送不出去。
REASON_PLUGIN_UNAVAILABLE: Final[str] = "plugin_unavailable"

#: ``unapplied`` 里记忆这一组的 ``kind``。
MEMORY_KIND: Final[str] = "memory"

#: 原生插件送达这一组的状态（``summary["pluginNative"]`` 只有一个词；没有原生送达的插件时不出现）。
PLUGIN_DELIVERED: Final[str] = "delivered"    # 原样交给 Agent，且机制在真实 Agent 上验证过
PLUGIN_UNVERIFIED: Final[str] = "unverified"  # 原样交给 Agent，但机制没验证过

#: 原生记忆这一组的状态（``summary["nativeMemory"]`` 只有一个词；没有 Project 记忆时不出现）。
MEMORY_NONE: Final[str] = "none"              # 适配器声明该 Agent 没有自带记忆
MEMORY_DISABLED: Final[str] = "disabled"      # 本次启动关掉了，且机制在真实 Agent 上验证过
MEMORY_UNVERIFIED: Final[str] = "unverified"  # 本次启动关掉了，但机制没验证过
MEMORY_ACTIVE: Final[str] = "active"          # 关不掉（或不知道能不能关）：自带记忆未接管

#: 原生 MCP 这一组的状态（``summary["nativeMcp"]`` 只有一个词）。
MCP_NONE: Final[str] = "none"          # 该 Agent 没有自带的全局 MCP
MCP_UNMANAGED: Final[str] = "unmanaged"  # 自带 MCP 还没整理过（没有任何分配/屏蔽行），不动
MCP_EXCLUDED: Final[str] = "excluded"  # 本次启动排除了未分配的那些
MCP_UNVERIFIED: Final[str] = "unverified"  # 同上，但机制没在真实 Agent 上验证过
MCP_UNISOLATED: Final[str] = "unisolated"  # 有未分配的，但该 Agent 没有排除机制
MCP_CLEAN: Final[str] = "clean"        # 自带的全部已分配，无需排除


@dataclass
class LaunchPlan:
    """一条会话这次启动要送什么、怎么送。只有名字与正文，不含任何凭据值。"""

    instructions_body: str = ""
    instruction_names: tuple[str, ...] = ()
    instructions_channel: str | None = None
    native_mcp_mode: str = MCP_NONE
    native_excluded: tuple[dict[str, Any], ...] = ()
    native_assigned: tuple[dict[str, Any], ...] = ()
    #: Agent 自己装的插件：本次不加载的、照常加载的（``drivers.native_plugins`` 的记录）。
    native_plugins_excluded: tuple[dict[str, Any], ...] = ()
    native_plugins_kept: tuple[dict[str, Any], ...] = ()
    #: ``None`` = 这条会话没有 Project 记忆（组内成员等），自带记忆不动也不报。
    native_memory_mode: str | None = None
    #: 原样交给 Agent 的插件（:func:`expand_plugins` 的记录），``apply_launch_plan`` 调钩子送出。
    native_plugins: tuple[dict[str, Any], ...] = ()
    #: 第 1 步就能确定的「没送出去」（D-03）。
    unapplied: tuple[UnappliedCapability, ...] = ()
    warnings: tuple[str, ...] = ()
    #: ``GET /api/conversations/{id}`` 回显用，只有名字。
    summary: dict[str, list[str]] = field(default_factory=dict)

    @property
    def excluded_names(self) -> tuple[str, ...]:
        return tuple(str(row.get("name")) for row in self.native_excluded)


def _hook(adapter: Any, name: str):
    hook = getattr(adapter, name, None) if adapter is not None else None
    return hook if callable(hook) else None


def plan_launch(
    effective: EffectiveCapabilities,
    *,
    adapter: Any,
    instructions_file: str | None,
    workspace_root: str | None,
    project_id: str,
    native_inventory: Sequence[Mapping[str, Any]] = (),
    native_plugin_inventory: Sequence[Mapping[str, Any]] = (),
    workspace_in_use: bool = False,
    require_workspace: bool = True,
    plugins: "PluginExpansion | None" = None,
) -> LaunchPlan:
    """算出这次启动的送达计划；文件通道在这里**真的**写受管块。

    ``native_inventory`` 是该 Agent 此刻从自己全局配置里读到的 MCP 记录
    （``drivers.mcp_inventory.discover_mcp``）。``workspace_in_use`` 为真表示同一个
    工作目录里另有会话在跑：照写，另加一条提示（设计稿第 3 节第 2 档）。

    ``require_workspace``：有指令、通道是文件、却没有工作目录时抛
    :class:`WorkspaceRequiredError`（界面让用户先选目录）；关掉它只会把这件事报进
    ``unapplied``——给不该拦住启动的场合（外部终端预创建等）用。

    ``plugins``：调用方**先**用 :func:`expand_plugins` 把插件拆开（它改的是 ``effective``，
    要在技能 / MCP 投射之前），再把结果交到这里并进计划：原生送达的记录、送不出去的部分、
    摘要与提醒。
    """
    plan = LaunchPlan()
    unapplied: list[UnappliedCapability] = []
    warnings: list[str] = []
    # ---------------------------------------------------- Agent 自己装的插件
    # 与自带 MCP 同一口径：没整理过就不动；整理过的，只不加载配置库里有、但没分到这里的。
    installed = [dict(row) for row in native_plugin_inventory if isinstance(row, Mapping) and row.get("capabilityId")]
    if installed:
        excluded_plugins: list[dict[str, Any]] = []
        if effective.tree_managed or effective.native_plugins or effective.native_plugins_blocked:
            assigned_ids = {entry.capability_id for entry in effective.native_plugins}
            known_plugins = set(effective.native_plugins_known)
            excluded_plugins = [row for row in installed if row["capabilityId"] not in assigned_ids
                                and (effective.tree_managed or not known_plugins or row["capabilityId"] in known_plugins)]
        plan.native_plugins_excluded = tuple(excluded_plugins)
        plan.native_plugins_kept = tuple(row for row in installed if row not in excluded_plugins)
        if excluded_plugins:
            names = tuple(str(row.get("name")) for row in excluded_plugins)
            if _hook(adapter, "exclude_native_plugins") is None:
                plan.native_plugins_excluded, plan.native_plugins_kept = (), tuple(installed)
                plan.summary["nativePluginsUnisolated"] = list(names)
                unapplied.append(UnappliedCapability(kind=PLUGINS, names=names, reason=REASON_NATIVE_NOT_ISOLATED))
            else:
                plan.summary["nativePluginsExcluded"] = list(names)
    if plugins is not None:
        plan.native_plugins = tuple(plugins.native)
        unapplied.extend(plugins.unapplied)
        warnings.extend(plugins.warnings)
        plan.summary.update(plugins.summary)

    # ---------------------------------------------------------------- 指令
    valid = instruction_entries(effective)
    invalid = invalid_instruction_entries(effective)
    names = tuple(entry.capability_id for entry in valid)
    if invalid:
        unapplied.append(UnappliedCapability(
            kind=INSTRUCTIONS,
            names=tuple(dict.fromkeys(entry.capability_id for entry in invalid)),
            reason=INVALID_CONFIG,
        ))
    if valid:
        body = compose_instructions(effective)
        plan.instructions_body = body
        plan.instruction_names = names
        if _hook(adapter, "deliver_instructions") is not None:
            plan.instructions_channel = CHANNEL_LAUNCH
            plan.summary["instructions"] = list(names)
        elif all(is_auxiliary(entry) for entry in valid):
            # 只有宿主附加的辅助节（例如项目记忆索引）：不为它拦住启动、不往用户文件夹里
            # 写东西，也不报「未生效」；这类内容另有通道（记忆 MCP）。
            plan.instructions_body = ""
            plan.instruction_names = ()
        elif instructions_file:
            if not (workspace_root and str(workspace_root).strip()):
                if require_workspace:
                    raise WorkspaceRequiredError(
                        "这个项目还没有工作目录，项目指令无处可写；先选一个文件夹再开始对话",
                        failure=FailureHint(
                            code=WORKSPACE_REQUIRED,
                            message="这个项目还没有工作目录",
                            hint="先为项目选择一个文件夹，再发送第一条消息",
                        ),
                    )
                unapplied.append(UnappliedCapability(kind=INSTRUCTIONS, names=names, reason=REASON_NO_WORKSPACE))
            else:
                outcome = materialize_block(
                    rows=tuple((entry.capability_type, entry.capability_id) for entry in valid),
                    body=body,
                    workspace_root=workspace_root,
                    filename=instructions_file,
                    marker=INSTRUCTIONS_MARKER,
                    project_id=project_id,
                    dry_run=False,
                )
                warnings.extend(outcome.warnings)
                if outcome.unsupported:
                    reason = next((row.reason for row in outcome.unsupported if row.reason), None) or UNSUPPORTED
                    unapplied.append(UnappliedCapability(kind=INSTRUCTIONS, names=names, reason=reason))
                else:
                    plan.instructions_channel = CHANNEL_WORKSPACE_FILE
                    plan.summary["instructions"] = list(names)
                    if workspace_in_use:
                        # 写是写了；另一条会话的进程读的还是旧文件，说一声（设计稿第 3 节）。
                        unapplied.append(UnappliedCapability(
                            kind=INSTRUCTIONS, names=names, reason=REASON_WORKSPACE_IN_USE))
        else:
            unapplied.append(UnappliedCapability(kind=INSTRUCTIONS, names=names, reason="no_convention"))

    # ---------------------------------------------------------- 原生 MCP
    inventory = [dict(row) for row in native_inventory if isinstance(row, Mapping) and row.get("name")]
    if not inventory:
        plan.native_mcp_mode = MCP_NONE
    elif not effective.tree_managed and not effective.native_mcp and not effective.native_mcp_blocked:
        # 从没整理过：用户没分配也没屏蔽过任何一条，不替他关掉正在用的东西。
        plan.native_mcp_mode = MCP_UNMANAGED
    else:
        assigned_ids = {entry.capability_id for entry in effective.native_mcp}
        # 只排除用户安排过的（配置库里有、但没分到这个 Project）。还没进配置库的新 MCP 照常
        # 加载：算进 assigned，严格模式下也会被显式传进去。没有目录信息时按旧口径全部视为已知。
        known = set(effective.native_mcp_known)
        # 显式导入的 Agent 只认树：新出现的也不加载，等用户在变化提醒里导入。
        fresh = () if effective.tree_managed else \
            tuple(row for row in inventory if known and row.get("capabilityId") not in known)
        assigned = tuple(row for row in inventory if row.get("capabilityId") in assigned_ids) + fresh
        excluded = tuple(row for row in inventory
                         if row.get("capabilityId") not in assigned_ids and row not in fresh)
        if fresh:
            plan.summary["nativeMcpNew"] = [str(row.get("name")) for row in fresh]
        plan.native_assigned = assigned
        plan.native_excluded = excluded
        if not excluded:
            plan.native_mcp_mode = MCP_CLEAN
        elif _hook(adapter, "exclude_native_mcp") is None:
            plan.native_mcp_mode = MCP_UNISOLATED
            plan.summary["nativeMcpUnisolated"] = list(plan.excluded_names)
            unapplied.append(UnappliedCapability(kind=MCP, names=plan.excluded_names,
                                                 reason=REASON_NATIVE_NOT_ISOLATED))
        else:
            verified = getattr(adapter, "NATIVE_MCP_EXCLUSION_VERIFIED", False) is True
            # 未验证只记在摘要里，不在每次启动时提醒（与自带记忆、插件同一口径）。
            plan.native_mcp_mode = MCP_EXCLUDED if verified else MCP_UNVERIFIED
            plan.summary["nativeMcpExcluded"] = list(plan.excluded_names)
    if plan.native_mcp_mode != MCP_NONE:
        plan.summary["nativeMcp"] = [plan.native_mcp_mode]

    # ---------------------------------------------------------- 原生记忆
    memory_id = project_memory_id(effective)
    # 没有适配器 = 不知道这是哪个 Agent（没有预设的裸 ACP），这一组无从判断，不报。
    if memory_id is not None and adapter is not None:
        if _hook(adapter, "disable_native_memory") is not None:
            verified = getattr(adapter, "NATIVE_MEMORY_DISABLE_VERIFIED", False) is True
            plan.native_memory_mode = MEMORY_DISABLED if verified else MEMORY_UNVERIFIED
        elif getattr(adapter, "HAS_NATIVE_MEMORY", True) is False:
            plan.native_memory_mode = MEMORY_NONE
        else:
            plan.native_memory_mode = MEMORY_ACTIVE
        # 只记在送达摘要里，不进会话里的「未生效」提示：关不掉自带记忆的 Agent 很常见，
        # 每条会话都提醒一遍太吵；设计稿要的是项目页上标「未接管」。
        plan.summary["nativeMemory"] = [plan.native_memory_mode]
    plan.unapplied = tuple(unapplied)
    plan.warnings = tuple(warnings)
    return plan


def apply_launch_plan(
    plan: LaunchPlan | None,
    spec: Any,
    metadata: Mapping[str, Any],
    *,
    adapter: Any,
    cwd: str | None,
) -> tuple[Any, dict[str, Any], tuple[str, ...]]:
    """把计划落到这一次进程的 ``spec`` 与 ``metadata`` 上。没有计划就原样返回。"""
    metadata = dict(metadata)
    warnings: list[str] = []
    if plan is None:
        return spec, metadata, ()
    if plan.instructions_channel == CHANNEL_LAUNCH and plan.instructions_body:
        deliver = _hook(adapter, "deliver_instructions")
        if deliver is not None:
            spec, metadata = deliver(spec, metadata, plan.instructions_body)
            metadata = dict(metadata)
    if plan.native_mcp_mode in (MCP_EXCLUDED, MCP_UNVERIFIED) and plan.native_excluded:
        exclude = _hook(adapter, "exclude_native_mcp")
        if exclude is not None:
            spec, metadata, extra = exclude(
                spec, metadata, excluded=plan.native_excluded, assigned=plan.native_assigned, cwd=cwd)
            metadata = dict(metadata)
            warnings.extend(extra or ())
    if plan.native_plugins_excluded or plan.native_plugins_kept:
        # 先于库插件送达：适配器要知道这次哪些自装插件不加载、哪些照常加载（严格 MCP 模式下
        # 照常加载的插件自带的 MCP 要显式带上）。
        exclude_plugins = _hook(adapter, "exclude_native_plugins")
        if exclude_plugins is not None:
            spec, metadata, extra = exclude_plugins(
                spec, metadata, excluded=plan.native_plugins_excluded, kept=plan.native_plugins_kept)
            metadata = dict(metadata)
            warnings.extend(extra or ())
    if plan.native_memory_mode in (MEMORY_DISABLED, MEMORY_UNVERIFIED):
        disable = _hook(adapter, "disable_native_memory")
        if disable is not None:
            spec, metadata = disable(spec, metadata)
            metadata = dict(metadata)
    if plan.native_plugins:
        deliver = _hook(adapter, "deliver_plugins")
        if deliver is not None:
            spec, metadata = deliver(spec, metadata, plan.native_plugins)
            metadata = dict(metadata)
    return spec, metadata, tuple(warnings)


# --------------------------------------------------------------------------- #
# 插件（2026-11-09，设计稿第 6 节）
# --------------------------------------------------------------------------- #

#: 插件能力类型与托管技能选择项（与 ``app.capabilities.effective`` / ``skill_execution.common`` 同名）。
PLUGINS_TYPE: Final[str] = "plugins"
SELECTION_TYPE: Final[str] = "skill-selection"


@dataclass
class PluginExpansion:
    """:func:`expand_plugins` 的产物：改好的有效能力 + 要并进 :class:`LaunchPlan` 的部分。"""

    effective: EffectiveCapabilities
    #: 原样交给 Agent 的插件记录：``id`` / ``name`` / ``format`` / ``path``（包目录）/
    #: ``root``（能力库里这个插件的目录，包的上一级）/ ``skills``（技能目录名）。
    native: tuple[dict[str, Any], ...] = ()
    #: 拆解送达的插件名（技能并入托管投射、MCP 并入 Project MCP）。
    extracted: tuple[str, ...] = ()
    unapplied: tuple[UnappliedCapability, ...] = ()
    warnings: tuple[str, ...] = ()
    summary: dict[str, list[str]] = field(default_factory=dict)


def _canonical_mcp(definition: Any) -> str:
    """用来判断「是不是同一个 MCP」：只比启动方式（命令、参数、地址、类型、变量名）。"""
    import json
    if not isinstance(definition, Mapping):
        return json.dumps(definition, sort_keys=True, default=str)
    env = definition.get("env")
    names = sorted(env) if isinstance(env, Mapping) else sorted(map(str, env or ()))
    keys = {key: definition.get(key) for key in ("command", "args", "url", "type", "transport") if key in definition}
    return json.dumps({**keys, "env": names}, sort_keys=True, default=str)


def _mcp_definitions(effective: EffectiveCapabilities) -> dict[str, str]:
    from drivers.acp.mcp_projection import _Acc, _servers_of, _value_of
    taken: dict[str, str] = {}
    for entry in effective.by_type("mcp"):
        for name, raw in _servers_of(entry.capability_id, _value_of(entry.config), _Acc()):
            if name:
                taken[name] = _canonical_mcp(raw)
    return taken


def _plugin_mcp_name(name: str, definition: Any, plugin: str, taken: Mapping[str, str]) -> str | None:
    """插件 MCP 的最终名字；``None`` = 同名同定义已经在送，不再重复。"""
    canonical = _canonical_mcp(definition)
    if name not in taken:
        return name
    if taken[name] == canonical:
        return None
    base = f"{re.sub(r'[^A-Za-z0-9_-]+', '-', plugin).strip('-') or 'plugin'}-{name}"
    final, index = base, 2
    while final in taken and taken[final] != canonical:
        final, index = f"{base}-{index}", index + 1
    return None if final in taken else final


def plugin_entries(effective: EffectiveCapabilities) -> tuple[Any, ...]:
    return tuple(e for e in effective.entries if e.capability_type == PLUGINS_TYPE)


def expand_plugins(effective: EffectiveCapabilities, *, adapter: Any, can_project_skills: bool = True) -> PluginExpansion:
    """把有效能力里的插件按这个 Agent 的能力拆开（在技能 / MCP 投射**之前**调用）。

    每个 ``plugins`` 行的 config 由 ``app.capabilities.effective.with_plugin_locations``
    补上了 ``sourcePath``（能力库里的包目录）与 ``format``。这里：

    - 适配器 ``NATIVE_PLUGIN_FORMATS`` 含该格式且有 ``deliver_plugins`` → 原样交给它
      （记录进 ``native``，启动时由 :func:`apply_launch_plan` 调钩子）；
    - 否则拆解：包里的技能目录追加进托管技能选择（``skill-selection`` 的 ``assets``，
      没有就新建一条 ``mode: managed``），MCP 定义追加为 ``mcp`` 行；包里还有 hooks /
      命令 / 子 agent 的，这一部分报 ``plugin_parts_unsupported``；
    - 包不在了（``available: false`` 或目录缺失）→ 整个插件报 ``plugin_unavailable``。

    ``can_project_skills`` 为假（这个 Agent 没有托管技能投射适配器）时，拆解出的技能目录
    不进选择项、报 ``unsupported``，不拦住启动——插件不该让一个本来能开的会话开不了。

    不读包里任何可执行内容；MCP 定义里只有命令行与变量名，值在投射时按名字取。
    """
    from drivers.plugin_packages import PackageError, read_package

    rows = plugin_entries(effective)
    if not rows:
        return PluginExpansion(effective=effective)
    native_formats = tuple(getattr(adapter, "NATIVE_PLUGIN_FORMATS", ()) or ()) if adapter is not None else ()
    # 已经要送的 MCP（项目自己配的 + 前面插件拆出来的）：名字 → 规范化后的定义。
    taken = _mcp_definitions(effective)
    renamed: list[str] = []
    can_deliver = _hook(adapter, "deliver_plugins") is not None
    native: list[dict[str, Any]] = []
    extracted: list[str] = []
    missing: list[str] = []
    partial: list[str] = []
    skills_unsupported: list[str] = []
    skill_assets: list[dict[str, Any]] = []
    mcp_entries: list[Any] = []
    warnings: list[str] = []
    for entry in rows:
        config = entry.config
        name = str(config.get("name") or entry.capability_id)
        path = config.get("sourcePath")
        if not path or config.get("available", True) is False:
            missing.append(entry.capability_id)
            continue
        try:
            package = read_package(Path(path))
        except PackageError:
            missing.append(entry.capability_id)
            continue
        if package.format in native_formats and can_deliver:
            native.append({"id": entry.capability_id, "name": package.name, "format": package.format,
                           "path": str(package.path), "root": str(config.get("libraryPath") or package.path.parent),
                           "skills": [Path(rel).name for rel in package.skills],
                           # 适配器在「只加载传入的 MCP」模式下要显式带上这些（见 claude.deliver_plugins）。
                           "mcpServers": {name: dict(d) for name, d in package.mcp_servers.items()}})
            continue
        extracted.append(name)
        if package.skills and not can_project_skills:
            skills_unsupported.append(entry.capability_id)
        for relative in package.skills if can_project_skills else ():
            folder = package.path / relative
            skill_assets.append({"id": f"{entry.capability_id}:{Path(relative).name}", "name": Path(relative).name,
                                 "capabilityType": "skills", "capabilityId": f"{entry.capability_id}/{Path(relative).name}",
                                 "sourcePath": str(folder), "available": True, "pluginId": entry.capability_id})
        for server_name, definition in package.mcp_servers.items():
            # 同名同定义：本来就是同一个 MCP，只送一次。同名不同定义：加插件名前缀，两个都保留并记下。
            final = _plugin_mcp_name(server_name, definition, package.name, taken)
            if final is None:
                continue
            if final != server_name:
                renamed.append(f"{server_name} → {final}")
            taken[final] = _canonical_mcp(definition)
            mcp_entries.append(entry.model_copy(update={
                "capability_type": "mcp", "capability_id": final,
                "config": {"value": {**dict(definition), "name": final}, "pluginId": entry.capability_id}}))
        if package.hooks or package.commands or package.agents:
            partial.append(entry.capability_id)
    entries = list(effective.entries)
    if skill_assets:
        selection = effective.get(SELECTION_TYPE, "default")
        if selection is not None and selection.config.get("mode") == "native":
            warnings.append("该 Agent 接入为原生技能模式，插件里的技能本次未投射")
        elif selection is not None:
            merged = selection.model_copy(update={"config": {
                **selection.config, "assets": [*selection.config.get("assets", []), *skill_assets]}})
            entries = [merged if e is selection else e for e in entries]
        else:
            entries.append(EffectiveCapability(
                capability_type=SELECTION_TYPE, capability_id="default",
                config={"mode": "managed", "assets": skill_assets},
                source_project_id=effective.project_id, inherited=False))
    entries.extend(mcp_entries)
    unapplied: list[UnappliedCapability] = []
    if missing:
        unapplied.append(UnappliedCapability(kind=PLUGINS, names=tuple(dict.fromkeys(missing)),
                                             reason=REASON_PLUGIN_UNAVAILABLE))
    if partial:
        unapplied.append(UnappliedCapability(kind=PLUGINS, names=tuple(dict.fromkeys(partial)),
                                             reason=REASON_PLUGIN_PARTS_UNSUPPORTED))
    if skills_unsupported:
        unapplied.append(UnappliedCapability(kind=PLUGINS, names=tuple(dict.fromkeys(skills_unsupported)),
                                             reason=UNSUPPORTED))
    summary: dict[str, list[str]] = {}
    delivered = [row["name"] for row in native] + extracted
    if delivered:
        summary["plugins"] = delivered
    if native:
        summary["pluginsNative"] = [row["name"] for row in native]
        verified = getattr(adapter, "NATIVE_PLUGIN_DELIVERY_VERIFIED", False) is True
        # 未验证只记在摘要里，不在每次启动时提醒（与自带记忆同一口径）。
        summary["pluginNative"] = [PLUGIN_DELIVERED if verified else PLUGIN_UNVERIFIED]
    if extracted:
        summary["pluginsExtracted"] = extracted
    if renamed:
        summary["pluginMcpRenamed"] = renamed
    return PluginExpansion(
        effective=effective.model_copy(update={"entries": tuple(entries)}) if entries != list(effective.entries) else effective,
        native=tuple(native), extracted=tuple(extracted), unapplied=tuple(unapplied),
        warnings=tuple(warnings), summary=summary)


def project_memory_id(effective: EffectiveCapabilities) -> str | None:
    """有效能力里带着 Project 记忆 MCP 时返回它的 id，否则 ``None``。

    这一层不认识「记忆」是什么，只认宿主在 ``memory_injection`` 里声明的那一个 MCP id；
    组内成员会话拿不到它，自带记忆就不动。
    """
    from app.projects.memory_injection import MEMORY_MCP_ID
    for entry in effective.entries:
        if entry.capability_type == "mcp" and entry.capability_id == MEMORY_MCP_ID:
            return entry.capability_id
    return None


def same_directory(left: str | None, right: str | None) -> bool:
    """两个工作目录是不是同一个地方（展开 ``~``、解链接后比较）。"""
    if not left or not right:
        return False
    try:
        return resolve_directory(left) == resolve_directory(right)
    except OSError:
        return os.path.abspath(os.path.expanduser(left)) == os.path.abspath(os.path.expanduser(right))


def home_of(env: Mapping[str, str]) -> Path:
    """清单读哪个家目录：子进程环境里的 ``HOME``（探测作用域会改它），否则当前用户的。"""
    raw = env.get("HOME")
    return Path(raw) if raw else Path.home()


__all__ = [
    "CHANNEL_LAUNCH",
    "CHANNEL_WORKSPACE_FILE",
    "LaunchPlan",
    "MCP_CLEAN",
    "MCP_EXCLUDED",
    "MCP_NONE",
    "MCP_UNISOLATED",
    "MCP_UNMANAGED",
    "MCP_UNVERIFIED",
    "MEMORY_ACTIVE",
    "MEMORY_DISABLED",
    "MEMORY_KIND",
    "MEMORY_NONE",
    "MEMORY_UNVERIFIED",
    "PLUGIN_DELIVERED",
    "PLUGIN_UNVERIFIED",
    "PluginExpansion",
    "REASON_NATIVE_MEMORY_ACTIVE",
    "REASON_PLUGIN_PARTS_UNSUPPORTED",
    "REASON_PLUGIN_UNAVAILABLE",
    "REASON_NATIVE_NOT_ISOLATED",
    "REASON_WORKSPACE_IN_USE",
    "apply_launch_plan",
    "expand_plugins",
    "home_of",
    "plugin_entries",
    "plan_launch",
    "project_memory_id",
    "same_directory",
]
