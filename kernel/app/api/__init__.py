"""领域数据库的只读 HTTP 接口层（N §12 目录建议中的 ``app/api/``）。

职责
----
把 :class:`~app.persistence.base.RepositorySet` 里的 Project / Backend /
AgentBinding 三张表投射成 v1.0 §12.1 规定的只读端点，外加一个 Phase 1 专用的
对账端点（旧 JSON 状态 ↔ 领域库）。

边界
----
- **只读**：本层不提供任何写端点。Phase 1 的写入路径唯一入口是一次性迁移器。
- **Backend 中立**：本包属于公共层，受
  ``kernel/tests/test_public_type_purity.py`` 的私有名词扫描约束；任何具体
  Backend 的名字只能作为**数据**（``backend_id`` 的取值）出现，不得写进代码。
- **不依赖 web 框架的导入**：:mod:`app.api.views` 是纯函数模块；
  :mod:`app.api.router` 只在 :func:`~app.api.router.build_domain_router` 被调用时
  才导入 web 框架，因此公共层的模型扫描（会 import 每个子模块）不需要装框架。
"""

from __future__ import annotations

from app.api.views import (
    DomainDiff,
    binding_to_wire,
    build_domain_diff,
    project_to_wire,
    backend_to_wire,
)

__all__ = [
    "DomainDiff",
    "backend_to_wire",
    "binding_to_wire",
    "build_domain_diff",
    "project_to_wire",
]
