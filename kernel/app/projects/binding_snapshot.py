"""交给 Driver 的那份 Binding 快照（AD-58 的灌入点 + AD-175 的一把尺子）。

为什么有这个模块
----------------
「工作目录」在这个仓里一度有**两把尺子**：

- **投影写文件**走 ``AcpDriver._workspace_of(project, binding)``：**项目的**
  ``workspace_root`` 优先，Binding 的只是回落（AD-166「工作目录就是项目的边界」）；
- **起会话**走 ``AcpDriver.start_runtime`` → ``_workspace_root_for(binding_id)``：
  只读 Binding 的 ``runtime_config.workspace_root``，**压根不看项目**。

于是「在项目上设了工作目录、Binding 上没设」这条最常见的路，会让引擎在后端进程
的当前目录里起来（``_resolve_cwd(None)`` → ``os.getcwd()``）。2026-09-22 真机上
它**恰好**读到了本仓自己的 ``AGENTS.md`` 并照着回答——不是报错，是静默走错。

AD-175 把两把尺子收成一把，**收在接入层**：AD-58 的原话是「Driver 侧的 Binding
缓存由接入层在开 Runtime 前灌一次」，那就在**灌进去之前**把项目的工作目录补上。
Driver 读 ``runtime_config.workspace_root`` 的契约一个字不改，``cwd``（起会话）与
客户端 fs 的根（AD-152）同时被纠正到同一个地方。

口径（与 ``binding_router._workspace_meta`` 逐字相同）
------------------------------------------------------
1. **Binding 上已有的值优先**：它更具体，而且 AD-152 说它是「用户授权了哪个目录」；
2. 它为空时才用**项目的** ``workspace_root``；
3. 两边都没有就仍然是「没有」——**不**拿进程 cwd 顶替。

住在 ``app/projects/`` 而不是 ``app/api/``
------------------------------------------
六个调用点里有一个在 ``runtime/surface_handoff.py``，而 ``runtime`` 层至今没有
import 过 ``app.api``（只 import ``app.conversations`` / ``app.persistence`` /
``app.projects``）。把这个辅助函数放进 ``app.projects`` 就不用为了一个函数开一条
新的层间依赖方向。补的那个值是 Binding 与 Project 的领域口径，本来也属这一层。

**只补快照，不落库**：这份 Binding 只活在 Driver 的内存缓存里，仓库里那条记录
一个字节都不动——项目改了工作目录，下一次灌入自然就跟着变。
"""

from __future__ import annotations

import inspect
from typing import Any

from app.projects.models import AgentBinding, Project

#: Driver 侧读的那个键（``AcpDriver._workspace_root_for``）。
WORKSPACE_ROOT_KEY = "workspace_root"

__all__ = [
    "WORKSPACE_ROOT_KEY",
    "binding_workspace_root",
    "binding_with_project_workspace",
    "register_binding_snapshot",
]


def _clean(value: Any) -> str | None:
    """非空字符串才算「填了」；空串、空白、非字符串一律当没填。"""
    if isinstance(value, str) and value.strip():
        return value
    return None


def binding_workspace_root(binding: AgentBinding) -> str | None:
    """这条 Binding 自己写没写工作目录。"""
    return _clean(binding.runtime_config.get(WORKSPACE_ROOT_KEY))


def binding_with_project_workspace(
    binding: AgentBinding, project: Project | None
) -> AgentBinding:
    """把项目的工作目录补进 Binding 快照——**只在 Binding 自己没填时**。

    两边都没填就原样返回（``None`` 会让 Driver 既不声明 fs 能力，也不猜一个
    cwd）。返回的是新实例，入参不被改动。
    """
    if binding_workspace_root(binding) is not None:
        return binding
    root = _clean(project.workspace_root if project else None)
    if root is None:
        return binding
    runtime_config = dict(binding.runtime_config)
    runtime_config[WORKSPACE_ROOT_KEY] = root
    return binding.evolve(runtime_config=runtime_config)


async def register_binding_snapshot(
    driver: Any, binding: AgentBinding, projects: Any
) -> AgentBinding:
    """AD-58 的那一次灌入，带上 AD-175 的工作目录补全。

    ``driver`` 没有 ``register_binding``（Mock Driver）就只算出快照不灌。
    ``projects`` 是 :class:`~app.projects.repository.ProjectRepository`；查不到
    项目就当项目没填工作目录。返回**实际交给 Driver 的那一份**，方便断言。
    """
    project = await projects.get(binding.project_id) if projects is not None else None
    snapshot = binding_with_project_workspace(binding, project)
    register = getattr(driver, "register_binding", None)
    if register is not None:
        outcome = register(snapshot)
        if inspect.isawaitable(outcome):
            await outcome
    return snapshot
