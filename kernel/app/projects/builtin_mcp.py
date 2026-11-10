"""每条会话启动时附上 Kaus 内置 MCP 和它的说明（项目记忆设计稿第 5 节；2026-11-10 扩名）。

内置 MCP（``mcp_servers/kaus_builtin_mcp.py``）提供项目记忆和「占用 / 释放屏幕」，不进技能树。
在 Session Host 取有效能力的那一处包一层（``session_bootstrap`` 的 ``capability_projector``），
所以无论 Agent 走哪条送达通道（启动注入或工作目录文件）都会带上，驱动不必认识它。
组内执行的成员会话只附屏幕两项和屏幕说明：组员写入的是组内资料，不进 Project 记忆。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.projects.memory import INDEX_LINE_LIMIT, ProjectMemory

MEMORY_SECTION_ID = "kaus-project-memory"
SCREEN_SECTION_ID = "kaus-screen"
BUILTIN_MCP_ID = "kaus"
#: 排在项目自己的指令之后。
SECTION_ORDER = 900
SERVER_SCRIPT = Path(__file__).resolve().parents[3] / "mcp_servers" / "kaus_builtin_mcp.py"

_GUIDANCE = {
    "zh": ("项目记忆",
           "这个 Project 在 Kaus 里有一份共享记忆，换 Agent 也不变。下面是索引，需要细节时用 kaus_memory_search 查。\n"
           "发现值得长期记住的内容（项目事实、约定、用户偏好、踩过的坑和解决办法、当前目标）时，直接用 kaus_memory_write 记下，"
           "不用征求用户同意。一次性的细节、密钥和密码、能从代码里直接看出来的内容不要记。"
           "只在一段时间内成立的状态（进行中的任务、本周安排）写 type=temporary，到期自动收起；完全或几乎相同的内容 Kaus 会自动合并。"
           "用户说「整理一下项目记忆」时，用同一组工具合并重复、作废过时条目。",
           "（还没有记忆）"),
    "en": ("Project memory",
           "This Project has one shared memory in Kaus that stays the same across Agents. The index is below; use "
           "kaus_memory_search for details.\nWhen you learn something worth keeping (project facts, conventions, user "
           "preferences, pitfalls and fixes, current goals), record it with kaus_memory_write without asking. Do not store "
           "one-off details, secrets or anything obvious from the code. Write state that holds only for a while (work in "
           "progress, this week's plan) with type=temporary; it is collapsed when it expires. Kaus merges identical or nearly "
           "identical entries itself. When the user asks to tidy the project memory, merge "
           "duplicates and invalidate stale entries with the same tools.",
           "(no memories yet)"),
}

#: 「谁进门谁挂牌」（2026-11-10）：Kaus 不识别电脑操作工具，只给一块牌子和这条规则。
_SCREEN_GUIDANCE = {
    "zh": ("屏幕",
           "接管整个屏幕（移动真实鼠标、往前台窗口打字、抢窗口焦点）之前先调用 kaus_screen_occupy，"
           "结束后立刻调用 kaus_screen_release；返回「等待」时再调用一次继续排队，等到之前不要接管屏幕。"
           "只在后台操作某个窗口时不用占用。"),
    "en": ("Screen",
           "Before taking over the whole screen (moving the real pointer, typing into the foreground window or "
           "taking window focus), call kaus_screen_occupy, and call kaus_screen_release as soon as you are done. "
           "When it returns waiting, call it again to keep your place and do not take over the screen meanwhile. "
           "Working on one window in the background needs no occupation."),
}


def _section(capability_id: str, project_id: str, title: str, body: str, order: int) -> EffectiveCapability:
    return EffectiveCapability(
        capability_type="instructions", capability_id=capability_id,
        # auxiliary：只为它不拦启动、不写用户文件；有项目指令时随它们一起送达。
        config={"body": body, "order": order, "title": title, "auxiliary": True},
        source_project_id=project_id, inherited=False, contributing_project_ids=(project_id,))


async def with_builtin_mcp(effective: EffectiveCapabilities, binding: Any, repositories: Any, *,
                           token_file: Path, url: str, locale: str = "zh",
                           python: str | None = None) -> EffectiveCapabilities:
    project_id = binding.project_id
    memory = not binding.runtime_config.get("group_execution")
    added = []
    if memory:
        lines, _overview = await ProjectMemory(repositories).index(project_id)
        title, guidance, empty = _GUIDANCE.get(locale, _GUIDANCE["zh"])
        shown = lines[:INDEX_LINE_LIMIT] or [empty]
        added.append(_section(MEMORY_SECTION_ID, project_id, title, guidance + "\n\n" + "\n".join(shown), SECTION_ORDER))
    title, guidance = _SCREEN_GUIDANCE.get(locale, _SCREEN_GUIDANCE["zh"])
    added.append(_section(SCREEN_SECTION_ID, project_id, title, guidance, SECTION_ORDER + 1))
    args = [str(SERVER_SCRIPT), "--project", project_id, "--agent", binding.backend_id,
            "--url", url, "--token-file", str(token_file), *(() if memory else ("--no-memory",))]
    added.append(EffectiveCapability(
        capability_type="mcp", capability_id=BUILTIN_MCP_ID,
        config={"value": {"command": python or sys.executable, "args": args},
                # 送不到 MCP 的 Agent 跳过它，不拒绝启动、不报未生效（索引节里仍有记忆）。
                "auxiliary": True,
                # 送达层据此判断 Project 记忆是否在场（按次关掉 Agent 自带记忆）。
                **({"memory": True} if memory else {})},
        source_project_id=project_id, inherited=False, contributing_project_ids=(project_id,)))
    ids = {entry.capability_id for entry in added}
    kept = tuple(e for e in effective.entries if e.capability_id not in ids)
    return effective.model_copy(update={"entries": (*kept, *added)})


__all__ = ["BUILTIN_MCP_ID", "MEMORY_SECTION_ID", "SCREEN_SECTION_ID", "with_builtin_mcp"]
