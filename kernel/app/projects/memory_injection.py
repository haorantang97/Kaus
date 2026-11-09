"""每条会话启动时附上 Project 记忆：一节短索引指令 + 记忆 MCP（设计稿第 5 节）。

在 Session Host 取有效能力的那一处包一层（``session_bootstrap`` 的 ``capability_projector``），
所以无论 Agent 走哪条送达通道（启动注入或工作目录文件）都会带上，驱动不必认识「记忆」。
组内执行的成员会话不附：组员写入的是组内资料，不进 Project 记忆。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.projects.memory import INDEX_LINE_LIMIT, ProjectMemory

MEMORY_SECTION_ID = "kaus-project-memory"
MEMORY_MCP_ID = "kaus-memory"
#: 排在项目自己的指令之后。
SECTION_ORDER = 900
SERVER_SCRIPT = Path(__file__).resolve().parents[3] / "mcp_servers" / "kaus_memory_mcp.py"

_GUIDANCE = {
    "zh": ("项目记忆",
           "这个 Project 在 Kaus 里有一份共享记忆，换 Agent 也不变。下面是索引，需要细节时用 kaus_memory_search 查。\n"
           "发现值得长期记住的内容（项目事实、约定、用户偏好、踩过的坑和解决办法、当前目标）时，直接用 kaus_memory_write 记下，"
           "不用征求用户同意。一次性的细节、密钥和密码、能从代码里直接看出来的内容不要记。"
           "用户说「整理一下项目记忆」时，用同一组工具合并重复、作废过时条目。",
           "（还没有记忆）"),
    "en": ("Project memory",
           "This Project has one shared memory in Kaus that stays the same across Agents. The index is below; use "
           "kaus_memory_search for details.\nWhen you learn something worth keeping (project facts, conventions, user "
           "preferences, pitfalls and fixes, current goals), record it with kaus_memory_write without asking. Do not store "
           "one-off details, secrets or anything obvious from the code. When the user asks to tidy the project memory, merge "
           "duplicates and invalidate stale entries with the same tools.",
           "(no memories yet)"),
}


async def with_project_memory(effective: EffectiveCapabilities, binding: Any, repositories: Any, *,
                              token_file: Path, url: str, locale: str = "zh",
                              python: str | None = None) -> EffectiveCapabilities:
    if binding.runtime_config.get("group_execution"):
        return effective
    project_id = binding.project_id
    lines, _overview = await ProjectMemory(repositories).index(project_id)
    title, guidance, empty = _GUIDANCE.get(locale, _GUIDANCE["zh"])
    shown = lines[:INDEX_LINE_LIMIT] or [empty]
    section = EffectiveCapability(
        capability_type="instructions", capability_id=MEMORY_SECTION_ID,
        # auxiliary：只为它不拦启动、不写用户文件；有项目指令时随它们一起送达。
        config={"body": guidance + "\n\n" + "\n".join(shown), "order": SECTION_ORDER, "title": title, "auxiliary": True},
        source_project_id=project_id, inherited=False, contributing_project_ids=(project_id,))
    server = EffectiveCapability(
        capability_type="mcp", capability_id=MEMORY_MCP_ID,
        config={"value": {"command": python or sys.executable,
                          "args": [str(SERVER_SCRIPT), "--project", project_id, "--agent", binding.backend_id,
                                   "--url", url, "--token-file", str(token_file)]},
                # 送不到 MCP 的 Agent 跳过它，不拒绝启动、不报未生效（索引节里仍有记忆）。
                "auxiliary": True},
        source_project_id=project_id, inherited=False, contributing_project_ids=(project_id,))
    kept = tuple(e for e in effective.entries if e.capability_id not in (MEMORY_SECTION_ID, MEMORY_MCP_ID))
    return effective.model_copy(update={"entries": (*kept, section, server)})


__all__ = ["MEMORY_MCP_ID", "MEMORY_SECTION_ID", "with_project_memory"]
