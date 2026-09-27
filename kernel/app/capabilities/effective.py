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

from app.capabilities.models import EffectiveCapabilities
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
    return TreeCapabilityResolver().resolve(
        CapabilityResolutionRequest(
            ancestry=tuple(ancestry), backend_key=backend_key
        ),
        assignments,
    )


async def resolve_effective_for_binding(
    repositories: Any, binding: Any
) -> EffectiveCapabilities:
    """这条 Binding 的有效能力（带 ``backend_key`` 过滤，R-01）。"""
    frozen = binding.runtime_config.get("group_capabilities")
    if frozen is not None and binding.runtime_config.get("group_execution"):
        return EffectiveCapabilities.model_validate(frozen)
    return await resolve_effective_for_project(
        repositories, binding.project_id, backend_key=binding.backend_key
    )


__all__ = [
    "resolve_effective_for_binding",
    "resolve_effective_for_project",
]
