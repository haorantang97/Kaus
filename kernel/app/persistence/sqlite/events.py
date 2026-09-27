"""SQLite 实现：``event_store``（D-16 / AD-13 的短期重放缓冲）。

三条与「这不是账本」直接相关的实现选择：

1. ``expires_at`` 是 NOT NULL 列，并且有独立索引 —— 清理是这张表的日常操作，
   不是偶尔跑一次的运维脚本（v1.0 §8.5：必须有显式保留策略）。
2. ``append`` 遇到已存在的 ``event_id`` 直接返回旧行，不报错也不覆盖 ——
   断线重连会把同一段事件再推一遍（N §7.3 规则 9），这在这张表里是**正常流量**。
3. 没有任何按内容检索的方法 —— 一旦能按内容查，它就开始被当成账本用
   （v1.0 §17 风险 9：Event Store 悄悄变成第二本账）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from app.errors import DomainInvariantError
from app.events.models import StoredEvent
from app.persistence.sqlite.codec import require_datetime, require_iso
from app.persistence.sqlite.database import SqliteDatabase
from runtime.event_envelope import AgentEventEnvelope


def _row_to_event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        event_id=row["event_id"],
        conversation_id=row["conversation_id"],
        sequence=int(row["sequence"]),
        collaboration_session_id=row["collaboration_session_id"],
        envelope=AgentEventEnvelope.model_validate_json(row["envelope_json"]),
        native_event_id=row["native_event_id"],
        expires_at=require_datetime(row["expires_at"]),
        created_at=require_datetime(row["created_at"]),
    )


class SqliteEventStoreRepository:
    """v1.0 §11.1 ``event_store``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, event_id: str) -> StoredEvent | None:
        row = self._db.query_one(
            "SELECT * FROM event_store WHERE event_id = ?", (event_id,)
        )
        return _row_to_event(row) if row is not None else None

    def _insert_parameters(self, event: StoredEvent) -> tuple[object, ...]:
        return (
            event.event_id,
            event.conversation_id,
            event.sequence,
            event.collaboration_session_id,
            # 落盘用驼峰 wire 形态：同一份 JSON 既能进库也能上线。
            event.envelope.model_dump_json(by_alias=True),
            event.native_event_id,
            require_iso(event.expires_at),
            require_iso(event.created_at),
        )

    async def append(self, event: StoredEvent) -> StoredEvent:
        existing = await self.get(event.event_id)
        if existing is not None:
            # 去重键命中：重放幂等。
            return existing
        row = self._db.query_one(
            "SELECT event_id FROM event_store WHERE conversation_id = ? AND sequence = ?",
            (event.conversation_id, event.sequence),
        )
        if row is not None:
            raise DomainInvariantError(
                "同一 Conversation 内 sequence 必须唯一："
                f"{event.conversation_id!r}#{event.sequence}"
            )
        self._db.run(
            """
            INSERT INTO event_store (event_id, conversation_id, sequence,
                                     collaboration_session_id, envelope_json,
                                     native_event_id, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._insert_parameters(event),
            conflict_message=(
                "同一 Conversation 内 sequence 必须唯一："
                f"{event.conversation_id!r}#{event.sequence}"
            ),
        )
        return event

    async def append_many(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        """整批写在一个事务里：要么全进，要么一条都不进。"""
        if not events:
            return ()
        stored: list[StoredEvent] = []
        with self._db.transaction():
            for event in events:
                stored.append(await self.append(event))
        return tuple(stored)

    async def list_after(
        self,
        conversation_id: str,
        *,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> Sequence[StoredEvent]:
        sql = "SELECT * FROM event_store WHERE conversation_id = ?"
        parameters: list[object] = [conversation_id]
        if after_sequence is not None:
            sql += " AND sequence > ?"
            parameters.append(after_sequence)
        sql += " ORDER BY sequence"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return tuple(_row_to_event(row) for row in self._db.query_all(sql, parameters))

    async def list_for_collaboration(
        self, collaboration_id: str, *, after_sequence: int | None = None
    ) -> Sequence[StoredEvent]:
        sql = "SELECT * FROM event_store WHERE collaboration_session_id = ?"
        parameters: list[object] = [collaboration_id]
        if after_sequence is not None:
            sql += " AND sequence > ?"
            parameters.append(after_sequence)
        sql += " ORDER BY sequence, conversation_id"
        return tuple(_row_to_event(row) for row in self._db.query_all(sql, parameters))

    async def latest_sequence(self, conversation_id: str) -> int | None:
        row = self._db.query_one(
            "SELECT MAX(sequence) AS top FROM event_store WHERE conversation_id = ?",
            (conversation_id,),
        )
        top = row["top"] if row is not None else None
        return None if top is None else int(top)

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """v1.0 §8.5 保留期清理。返回删除条数。"""
        at = now or datetime.now(tz=timezone.utc)
        return self._db.run(
            "DELETE FROM event_store WHERE expires_at <= ?", (require_iso(at),)
        )

    async def delete_for_conversation(self, conversation_id: str) -> int:
        return self._db.run(
            "DELETE FROM event_store WHERE conversation_id = ?", (conversation_id,)
        )


__all__ = ["SqliteEventStoreRepository"]
