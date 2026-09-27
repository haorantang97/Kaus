"""单文件 SQLite 领域数据库（v1.0 §11）。

用法
----
::

    from app.persistence.sqlite import SqliteUnitOfWork

    with SqliteUnitOfWork("~/somewhere/dashboard.db") as uow:
        await uow.repositories.projects.save(project)

打开即迁移：:class:`SqliteUnitOfWork` 构造时把库推到最新 schema 版本
（幂等，见 :mod:`app.persistence.sqlite.migrations`），随后给出一整套
:class:`~app.persistence.base.RepositorySet`。

设计口径
--------
- **只用标准库 ``sqlite3``**，不引入 ORM。十二张表、清晰的列，SQL 直写比一层
  映射更容易对着 v1.0 §11.1 逐条核对。
- **WAL 模式**：读不阻塞写、写不阻塞读——卡片渲染要在写入进行时照常读。
- **Dashboard 自己的库，原生 Agent 数据库不动**（v1.0 §11 开宗明义）。本包不含
  任何读写原生存储的代码路径。
- Repository 方法是 ``async`` 的（契约如此），但底层 ``sqlite3`` 调用是同步的：
  单机单进程、本地文件、毫秒级查询，为它引入线程池得不偿失。若将来出现长事务
  或远程存储，再在这一层包 ``asyncio.to_thread``——接口形状已经预留好了。
"""

from __future__ import annotations

from pathlib import Path

from app.persistence.base import RepositorySet
from app.persistence.sqlite.capabilities import SqliteProjectCapabilityRepository
from app.persistence.sqlite.collaboration import (
    SqliteCollaborationMemberRepository,
    SqliteCollaborationMessageRepository,
    SqliteCollaborationSessionRepository,
)
from app.persistence.sqlite.conversations import SqliteConversationRepository
from app.persistence.sqlite.database import SqliteDatabase
from app.persistence.sqlite.events import SqliteEventStoreRepository
from app.persistence.sqlite.migrations import (
    MIGRATIONS,
    Migration,
    applied_migrations,
    current_version,
    migrate,
)
from app.persistence.sqlite.projections import SqliteProjectionResultRepository
from app.persistence.sqlite.projects import (
    SqliteAgentBindingRepository,
    SqliteBackendRepository,
    SqliteProjectRepository,
)
from app.persistence.sqlite.runtimes import (
    SqliteRuntimeLeaseRepository,
    SqliteTerminalLaunchRepository,
)
from app.persistence.sqlite.group_materials import SqliteGroupMaterialsRepository
from app.persistence.sqlite.settings import SqliteAppSettingsRepository


def build_repository_set(database: SqliteDatabase) -> RepositorySet:
    """在一条已迁移的连接上装配全部 Repository。"""
    return RepositorySet(
        projects=SqliteProjectRepository(database),
        capabilities=SqliteProjectCapabilityRepository(database),
        backends=SqliteBackendRepository(database),
        bindings=SqliteAgentBindingRepository(database),
        conversations=SqliteConversationRepository(database),
        leases=SqliteRuntimeLeaseRepository(database),
        terminal_launches=SqliteTerminalLaunchRepository(database),
        group_materials=SqliteGroupMaterialsRepository(database),
        collaborations=SqliteCollaborationSessionRepository(database),
        members=SqliteCollaborationMemberRepository(database),
        group_messages=SqliteCollaborationMessageRepository(database),
        events=SqliteEventStoreRepository(database),
        projections=SqliteProjectionResultRepository(database),
        settings=SqliteAppSettingsRepository(database),
    )


class SqliteUnitOfWork:
    """一条连接 + 一整套 Repository 的生命周期封装。

    刻意**不**叫 Session/Context：它不做变更跟踪，也不延迟提交——每个
    Repository 方法自己就是一个完整的写。这里管的只有「连接开着」和「schema
    是最新的」两件事。
    """

    def __init__(self, path: str | Path) -> None:
        self.database = SqliteDatabase(path)
        self.repositories = build_repository_set(self.database)

    @property
    def schema_version(self) -> int:
        return current_version(self.database)

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> SqliteUnitOfWork:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_sqlite_repository_set(path: str | Path) -> tuple[SqliteDatabase, RepositorySet]:
    """打开（并迁移）一个库，返回 ``(连接, 仓库集合)``。"""
    database = SqliteDatabase(path)
    return database, build_repository_set(database)


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SqliteAgentBindingRepository",
    "SqliteBackendRepository",
    "SqliteCollaborationMemberRepository",
    "SqliteCollaborationMessageRepository",
    "SqliteCollaborationSessionRepository",
    "SqliteConversationRepository",
    "SqliteDatabase",
    "SqliteEventStoreRepository",
    "SqliteProjectCapabilityRepository",
    "SqliteProjectRepository",
    "SqliteRuntimeLeaseRepository",
    "SqliteGroupMaterialsRepository",
    "SqliteTerminalLaunchRepository",
    "SqliteUnitOfWork",
    "applied_migrations",
    "build_repository_set",
    "current_version",
    "migrate",
    "open_sqlite_repository_set",
]
