"""持久化层：Repository 的具体实现与它们的装配点。

职责
----
领域层只依赖 ``app.*.repository`` 里的 Protocol；本包提供两种实现的**装配**：

- :func:`in_memory_repository_set`：进程内字典（测试、脚手架、一次性迁移器）；
- :mod:`app.persistence.sqlite`：v1.0 §11 的单文件 SQLite 领域数据库（生产）。

两者暴露同一个 :class:`RepositorySet`，因此同一套契约测试可以参数化跑两遍——
「换存储不换语义」是这层唯一的验收标准。

对应规范
--------
- v1.0 §11「持久化模型」：Dashboard 自己的 SQLite 领域数据库，**保留原生 Agent
  数据库不动**。本包因此不含任何读写原生存储的代码。
- N §12：``app/`` 是领域层；具体存储是它的可替换实现，不得反向依赖。
"""

from __future__ import annotations

from app.persistence.base import RepositorySet
from app.persistence.memory import in_memory_repository_set

__all__ = ["RepositorySet", "in_memory_repository_set"]
