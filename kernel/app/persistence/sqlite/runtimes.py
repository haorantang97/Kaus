"""SQLite 实现：``runtime_leases`` / ``terminal_launches``。

AD-11：lease 是**信息性**的
---------------------------
这张表回答的是「现在谁在写这条会话」，用来驱动软提示与 Header 状态，**不是锁**。
落到实现上就是三件事而已：

- 写入（Card runtime 启动、Dashboard 拉起外部 CLI）；
- 释放（停止 / 退出）；
- 列出（Header 显示、R-10 的 reconcile）。

因此表上没有任何互斥约束，``acquire`` 在默认的 ``advisory`` 策略下**永远成功**
（后来者直接覆盖），只有 ``exclusive`` 这一档才拒绝抢占——那是留给开源多用户版
的升级位，第一期不走。带外启动的 CLI 根本没有 lease 行，靠原生存储 mtime 检测
（AD-20），这也是为什么这里不能把「没有 lease」当成「没人在写」。

``terminal_launches``（v1.0 §16.6）：**不得保存 Secret**。领域模型只收环境变量
名，本模块也就只落这些名字；命令一栏是给人看的摘要，不是可重放的完整命令行。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Sequence

from app.errors import DomainInvariantError
from app.persistence.sqlite.codec import (
    dump_json,
    from_iso,
    load_mapping,
    load_str_tuple,
    require_datetime,
    require_iso,
    to_iso,
)
from app.persistence.sqlite.database import SqliteDatabase
from app.runtimes.models import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    RuntimeLease,
    TerminalLaunch,
    reconcile_leases,
)


def _row_to_lease(row: sqlite3.Row) -> RuntimeLease:
    return RuntimeLease(
        conversation_id=row["conversation_id"],
        owner_type=row["owner_type"],
        owner_id=row["owner_id"],
        backend_process_id=row["backend_process_id"],
        policy=row["policy"],
        acquired_at=require_datetime(row["acquired_at"]),
        heartbeat_at=require_datetime(row["heartbeat_at"]),
        expires_at=from_iso(row["expires_at"]),
        metadata=load_mapping(row["metadata_json"]),
    )


class SqliteRuntimeLeaseRepository:
    """v1.0 §11.1 ``runtime_leases``（AD-11：信息性簿记）。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, conversation_id: str) -> RuntimeLease | None:
        row = self._db.query_one(
            "SELECT * FROM runtime_leases WHERE conversation_id = ?",
            (conversation_id,),
        )
        return _row_to_lease(row) if row is not None else None

    async def list_all(self) -> Sequence[RuntimeLease]:
        return tuple(
            _row_to_lease(row)
            for row in self._db.query_all(
                "SELECT * FROM runtime_leases ORDER BY acquired_at, conversation_id"
            )
        )

    async def acquire(self, lease: RuntimeLease) -> RuntimeLease:
        """登记归属。``advisory``（默认）允许覆盖，``exclusive`` 拒绝抢占。"""
        existing = await self.get(lease.conversation_id)
        if (
            existing is not None
            and existing.blocks_writes
            and existing.owner_id != lease.owner_id
        ):
            raise DomainInvariantError(
                "exclusive lease 已被他方持有，需显式强制接管（v1.0 §8.6）："
                f"{existing.owner_type}/{existing.owner_id}"
            )
        self._db.run(
            """
            INSERT INTO runtime_leases (conversation_id, owner_type, owner_id,
                                        backend_process_id, policy, acquired_at,
                                        heartbeat_at, expires_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                owner_type         = excluded.owner_type,
                owner_id           = excluded.owner_id,
                backend_process_id = excluded.backend_process_id,
                policy             = excluded.policy,
                acquired_at        = excluded.acquired_at,
                heartbeat_at       = excluded.heartbeat_at,
                expires_at         = excluded.expires_at,
                metadata_json      = excluded.metadata_json
            """,
            (
                lease.conversation_id,
                lease.owner_type,
                lease.owner_id,
                lease.backend_process_id,
                lease.policy,
                require_iso(lease.acquired_at),
                require_iso(lease.heartbeat_at),
                to_iso(lease.expires_at),
                dump_json(lease.metadata),
            ),
        )
        return lease

    async def heartbeat(
        self, conversation_id: str, at: datetime | None = None
    ) -> RuntimeLease | None:
        lease = await self.get(conversation_id)
        if lease is None:
            return None
        refreshed = lease.beat(at)
        self._db.run(
            "UPDATE runtime_leases SET heartbeat_at = ? WHERE conversation_id = ?",
            (require_iso(refreshed.heartbeat_at), conversation_id),
        )
        return refreshed

    async def release(self, conversation_id: str) -> None:
        self._db.run(
            "DELETE FROM runtime_leases WHERE conversation_id = ?", (conversation_id,)
        )

    async def reconcile(
        self,
        *,
        is_process_alive_by_id: dict[str, bool],
        now: datetime | None = None,
        heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> Sequence[RuntimeLease]:
        """R-10：Session Host 启动时清理 stale lease，返回被清理的那些。"""
        _, stale = reconcile_leases(
            await self.list_all(),
            is_process_alive=lambda pid: is_process_alive_by_id.get(pid, False),
            now=now,
            heartbeat_timeout=heartbeat_timeout,
        )
        if stale:
            with self._db.transaction() as connection:
                connection.executemany(
                    "DELETE FROM runtime_leases WHERE conversation_id = ?",
                    [(lease.conversation_id,) for lease in stale],
                )
        return stale


def _row_to_launch(row: sqlite3.Row) -> TerminalLaunch:
    return TerminalLaunch(
        id=row["id"],
        conversation_id=row["conversation_id"],
        launcher=row["launcher"],
        command_summary=row["command_summary"],
        correlation_id=row["correlation_id"],
        external_process_ref=row["external_process_ref"],
        env_passthrough=load_str_tuple(row["env_passthrough_json"]),
        status=row["status"],
        exit_code=row["exit_code"],
        launched_at=require_datetime(row["launched_at"]),
        exited_at=from_iso(row["exited_at"]),
        metadata=load_mapping(row["metadata_json"]),
    )


class SqliteTerminalLaunchRepository:
    """v1.0 §11.1 ``terminal_launches``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, launch_id: str) -> TerminalLaunch | None:
        row = self._db.query_one(
            "SELECT * FROM terminal_launches WHERE id = ?", (launch_id,)
        )
        return _row_to_launch(row) if row is not None else None

    async def get_by_correlation_id(self, correlation_id: str) -> TerminalLaunch | None:
        row = self._db.query_one(
            "SELECT * FROM terminal_launches WHERE correlation_id = ?",
            (correlation_id,),
        )
        return _row_to_launch(row) if row is not None else None

    async def list_for_conversation(
        self, conversation_id: str, *, include_finished: bool = True
    ) -> Sequence[TerminalLaunch]:
        if include_finished:
            rows = self._db.query_all(
                "SELECT * FROM terminal_launches WHERE conversation_id = ?"
                " ORDER BY launched_at, id",
                (conversation_id,),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM terminal_launches"
                " WHERE conversation_id = ? AND status IN ('launched', 'running')"
                " ORDER BY launched_at, id",
                (conversation_id,),
            )
        return tuple(_row_to_launch(row) for row in rows)

    async def save(self, launch: TerminalLaunch) -> TerminalLaunch:
        clash = await self.get_by_correlation_id(launch.correlation_id)
        if clash is not None and clash.id != launch.id:
            raise DomainInvariantError(
                f"Correlation ID 已被占用（v1.0 §8.7）：{launch.correlation_id!r}"
            )
        self._db.run(
            """
            INSERT INTO terminal_launches (id, conversation_id, launcher,
                                           command_summary, correlation_id,
                                           external_process_ref,
                                           env_passthrough_json, status, exit_code,
                                           launched_at, exited_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                conversation_id      = excluded.conversation_id,
                launcher             = excluded.launcher,
                command_summary      = excluded.command_summary,
                correlation_id       = excluded.correlation_id,
                external_process_ref = excluded.external_process_ref,
                env_passthrough_json = excluded.env_passthrough_json,
                status               = excluded.status,
                exit_code            = excluded.exit_code,
                exited_at            = excluded.exited_at,
                metadata_json        = excluded.metadata_json
            """,
            (
                launch.id,
                launch.conversation_id,
                launch.launcher,
                launch.command_summary,
                launch.correlation_id,
                launch.external_process_ref,
                # v1.0 §16.6：只有变量名，模型层已拒绝 NAME=VALUE 形态。
                dump_json(list(launch.env_passthrough)),
                launch.status,
                launch.exit_code,
                require_iso(launch.launched_at),
                to_iso(launch.exited_at),
                dump_json(launch.metadata),
            ),
            conflict_message=f"Correlation ID 已被占用（v1.0 §8.7）：{launch.correlation_id!r}",
        )
        return launch

    async def delete(self, launch_id: str) -> None:
        self._db.run("DELETE FROM terminal_launches WHERE id = ?", (launch_id,))


__all__ = ["SqliteRuntimeLeaseRepository", "SqliteTerminalLaunchRepository"]
