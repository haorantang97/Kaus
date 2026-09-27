"""单文件 SQLite 领域数据库的连接封装。

职责
----
1. 打开一个**单文件**数据库并把它调到 v1.0 §11 想要的形态：WAL 日志、合理的
   busy timeout、行按列名访问；
2. 提供 :meth:`SqliteDatabase.transaction` —— 一个显式的 ``BEGIN IMMEDIATE``
   事务边界，多语句写入靠它保持原子性；
3. 提供一把连接级互斥锁：一个 :class:`SqliteDatabase` 只持有**一条**连接，
   多线程调用同一条连接必须串行（``check_same_thread=False`` 只是解除 sqlite3 的
   线程断言，不代表连接本身线程安全）。

为什么是 WAL
------------
Dashboard 的读多写少，且卡片渲染要在写入进行时照常读。WAL 下读不阻塞写、
写不阻塞读，正好；MEMORY / DELETE 日志都做不到这一点。
``:memory:`` 库不支持 WAL —— :attr:`SqliteDatabase.journal_mode` 如实报告实际
拿到的模式，不假装。

为什么不声明外键约束
--------------------
v1.0 §11.1 的十二张表在领域上是**并列的聚合**，不是一棵强引用树：

- ``runtime_leases`` 是信息性簿记（AD-11），它可以先于 Conversation 出现或滞后
  于它消失，不该因为外键把 lease 变成「必须有活着的会话」；
- ``event_store`` 是可随时整表丢弃的缓冲（D-16），它被清空不代表对话没了，
  反过来对话被删也不该级联删掉别的东西；
- ``conversations.project_id`` 这类跨聚合引用由服务层保证，不靠数据库级联——
  v1.0 §16.6 明令「删除 Dashboard 对象不得连带删除原生数据」，级联删除是这条
  约束最容易踩的坑。

因此本模块**不**打开 ``PRAGMA foreign_keys``，schema 里也没有 ``REFERENCES``。
唯一性/不变量由各 Repository 与表上的唯一索引、触发器保证（见
:mod:`app.persistence.sqlite.migrations`）。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.persistence.sqlite.errors import translating_integrity_errors

#: 忙等待毫秒数：并发写入时不立刻报 ``database is locked``，先重试一会儿。
BUSY_TIMEOUT_MS: int = 5_000


class SqliteDatabase:
    """一条到单文件领域库的连接，外加事务边界与串行化锁。"""

    def __init__(self, path: str | Path, *, apply_migrations: bool = True) -> None:
        self.path = Path(path)
        if self.path.name != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            # 解除 sqlite3 的「只能在创建它的线程用」断言；真正的串行化靠 _lock。
            check_same_thread=False,
            # 自己管事务：不要 sqlite3 模块那套「遇到 DML 隐式 BEGIN」的魔法。
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        if apply_migrations:
            from app.persistence.sqlite.migrations import migrate

            migrate(self)

    # ------------------------------------------------------------------ #
    # 连接配置
    # ------------------------------------------------------------------ #

    def _configure(self) -> None:
        cursor = self._connection.execute("PRAGMA journal_mode=WAL")
        try:
            self._journal_mode = str(cursor.fetchone()[0]).lower()
        finally:
            cursor.close()
        # PRAGMA 的参数不能用占位符绑定，因此这里是拼串——值来自本模块的常量，
        # 不是外部输入。
        self._connection.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}").close()
        # WAL + NORMAL 是 SQLite 官方推荐的桌面组合：崩溃安全，且不每次 fsync。
        self._connection.execute("PRAGMA synchronous=NORMAL").close()

    @property
    def journal_mode(self) -> str:
        """实际生效的日志模式。文件库应为 ``wal``；``:memory:`` 拿不到 WAL。"""
        return self._journal_mode

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters))

    def executemany(
        self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]
    ) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.executemany(
                sql, [tuple(p) for p in seq_of_parameters]
            )

    def run(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
        *,
        conflict_message: str = "违反存储层约束",
    ) -> int:
        """执行一条写语句并关掉游标，返回受影响行数。

        Repository 的写路径统一走这里而不是 :meth:`execute`，因为它多做两件事：

        1. 返回「改了几行」这个调用方真正关心的数字，而不是一个要自己收尾的游标；
        2. 把 ``sqlite3.IntegrityError`` 翻译成领域异常（见
           :mod:`app.persistence.sqlite.errors`）——唯一索引与触发器是并发下的
           最后一道闸，它拦下来的错误必须说领域的语言，否则调用方的
           ``except DomainInvariantError`` 接不住。

        :meth:`execute` 保留为**原始逃生舱**：不翻译异常，用于 PRAGMA、迁移 DDL，
        以及测试里故意直捣数据库那道闸的场景。
        """
        with translating_integrity_errors(conflict_message):
            cursor = self.execute(sql, parameters)
            try:
                return int(cursor.rowcount)
            finally:
                cursor.close()

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        cursor = self.execute(sql, parameters)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def query_all(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        cursor = self.execute(sql, parameters)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` 事务。

        用 IMMEDIATE 而不是默认的 DEFERRED：写事务一开始就拿到写锁，
        避免「读着读着才升级成写」时才发现被别人占了，从而在并发下退化成
        不可重试的 ``SQLITE_BUSY``。
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SqliteDatabase:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["BUSY_TIMEOUT_MS", "SqliteDatabase"]
