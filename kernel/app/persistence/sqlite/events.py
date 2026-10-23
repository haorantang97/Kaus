"""SQLite 本地会话记录：按 event_id 幂等，按 sequence 有序重放。

``expires_at`` 保存显式保留策略：正文随会话保留，临时事件按 TTL 清理。
过期时间是独立索引列，不属于原始 ``envelope_json``。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from app.errors import DomainInvariantError
from app.events.models import StoredEvent
from app.events.repository import latest_message_receipt
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
        native_reference=json.loads(row["native_reference_json"]) if "native_reference_json" in row.keys() and row["native_reference_json"] else None,
        expires_at=require_datetime(row["expires_at"]),
        created_at=require_datetime(row["created_at"]),
    )


class SqliteEventStoreRepository:
    """v1.0 §11.1 ``event_store``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get_native_storage(self, conversation_id: str) -> dict | None:
        row = self._db.query_one("SELECT state_json FROM native_history_storage WHERE conversation_id=?", (conversation_id,))
        return json.loads(row["state_json"]) if row else None

    async def save_native_storage(self, conversation_id: str, state: dict) -> None:
        self._db.run("INSERT INTO native_history_storage (conversation_id,state_json) VALUES (?,?) "
                     "ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json",
                     (conversation_id, json.dumps(state)))

    async def replace_with_native_references(self, events: Sequence[StoredEvent]) -> int:
        # No rows or replay cursors are removed. Reference and empty payload are
        # committed together, so a restart never sees half a conversion.
        with self._db.transaction():
            for event in events:
                count = self._db.run("UPDATE event_store SET envelope_json=?, native_reference_json=? "
                    "WHERE event_id=? AND conversation_id=? AND sequence=? AND native_reference_json IS NULL",
                    (event.envelope.model_dump_json(by_alias=True), json.dumps(event.native_reference),
                     event.event_id, event.conversation_id, event.sequence))
                if count != 1:
                    raise DomainInvariantError("native_reference_conflict")
        return len(events)

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

    async def message_receipt(self, conversation_id: str, client_ref: str) -> Sequence[StoredEvent]:
        # Filter in SQLite and return only two small receipt envelopes; do not
        # load the conversation's tool output and full history into the process.
        rows=self._db.query_all(
            """SELECT * FROM event_store WHERE conversation_id=? AND sequence IN (
                SELECT MAX(sequence) FROM event_store WHERE conversation_id=?
                AND json_extract(envelope_json, '$.event.type')='extension.event'
                AND json_extract(envelope_json, '$.event.namespace')='kaus'
                AND json_extract(envelope_json, '$.event.name') IN ('user.message','user.message.failed')
                AND json_extract(envelope_json, '$.event.data.clientRef')=?
                GROUP BY json_extract(envelope_json, '$.event.name')
            ) ORDER BY sequence""", (conversation_id,conversation_id,client_ref))
        return latest_message_receipt(tuple(_row_to_event(row) for row in rows))

    async def latest_sequence(self, conversation_id: str) -> int | None:
        row = self._db.query_one(
            "SELECT MAX(sequence) AS top FROM event_store WHERE conversation_id = ?",
            (conversation_id,),
        )
        top = row["top"] if row is not None else None
        return None if top is None else int(top)

    async def purge_expired(self, *, now: datetime | None = None, exclude_conversations: Sequence[str] = ()) -> int:
        """v1.0 §8.5 保留期清理。返回删除条数。"""
        at = now or datetime.now(tz=timezone.utc)
        retained = tuple(exclude_conversations)
        clause = " AND conversation_id NOT IN (" + ",".join("?" for _ in retained) + ")" if retained else ""
        return self._db.run("DELETE FROM event_store WHERE expires_at <= ?" + clause, (require_iso(at), *retained))

    async def delete_for_conversation(self, conversation_id: str) -> int:
        self._db.run("DELETE FROM native_history_storage WHERE conversation_id=?", (conversation_id,))
        return self._db.run(
            "DELETE FROM event_store WHERE conversation_id = ?", (conversation_id,)
        )


__all__ = ["SqliteEventStoreRepository"]
