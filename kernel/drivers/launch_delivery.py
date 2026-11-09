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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities
from drivers.base import UnappliedCapability, WorkspaceRequiredError, WORKSPACE_REQUIRED, FailureHint
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
)
from drivers.projection_gaps import INSTRUCTIONS, INVALID_CONFIG, MCP, UNSUPPORTED
from drivers.workspace_projection import REASON_NO_WORKSPACE, materialize_block, resolve_directory

#: 送达通道。
CHANNEL_LAUNCH: Final[str] = "launch"
CHANNEL_WORKSPACE_FILE: Final[str] = "workspace_file"

#: ``unapplied`` 里的原因码（前端按码本地化）。
#: 已写进文件，但同一目录另有会话在跑——那条会话的进程读的是旧文件，重启后才生效。
REASON_WORKSPACE_IN_USE: Final[str] = "workspace_in_use"
#: 该 Agent 自带的这些 MCP 未分配给当前 Project，但它没有按次启动排除的机制。
REASON_NATIVE_NOT_ISOLATED: Final[str] = "native_not_isolated"

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
    workspace_in_use: bool = False,
    require_workspace: bool = True,
) -> LaunchPlan:
    """算出这次启动的送达计划；文件通道在这里**真的**写受管块。

    ``native_inventory`` 是该 Agent 此刻从自己全局配置里读到的 MCP 记录
    （``drivers.mcp_inventory.discover_mcp``）。``workspace_in_use`` 为真表示同一个
    工作目录里另有会话在跑：照写，另加一条提示（设计稿第 3 节第 2 档）。

    ``require_workspace``：有指令、通道是文件、却没有工作目录时抛
    :class:`WorkspaceRequiredError`（界面让用户先选目录）；关掉它只会把这件事报进
    ``unapplied``——给不该拦住启动的场合（外部终端预创建等）用。
    """
    plan = LaunchPlan()
    unapplied: list[UnappliedCapability] = []
    warnings: list[str] = []

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
    elif not effective.native_mcp and not effective.native_mcp_blocked:
        # 从没整理过：用户没分配也没屏蔽过任何一条，不替他关掉正在用的东西。
        plan.native_mcp_mode = MCP_UNMANAGED
    else:
        assigned_ids = {entry.capability_id for entry in effective.native_mcp}
        assigned = tuple(row for row in inventory if row.get("capabilityId") in assigned_ids)
        excluded = tuple(row for row in inventory if row.get("capabilityId") not in assigned_ids)
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
            plan.native_mcp_mode = MCP_EXCLUDED if verified else MCP_UNVERIFIED
            plan.summary["nativeMcpExcluded"] = list(plan.excluded_names)
            if not verified:
                warnings.append("原生 MCP 排除机制尚未在真实 Agent 上验证；项目页按「未隔离」对待")
    if plan.native_mcp_mode != MCP_NONE:
        plan.summary["nativeMcp"] = [plan.native_mcp_mode]
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
    return spec, metadata, tuple(warnings)


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
    "REASON_NATIVE_NOT_ISOLATED",
    "REASON_WORKSPACE_IN_USE",
    "apply_launch_plan",
    "home_of",
    "plan_launch",
    "same_directory",
]
