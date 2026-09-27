"""SQLite 实现：``conversations``。

R-02：这张表只存 Conversation 的**元数据与原生 Session 映射**，没有任何消息列
——对话内容的唯一账本是原生存储（v1.0 §11.3）。

v1.0 §8.7：``(agent_binding_id, native_session_id)`` 唯一，由部分唯一索引 +
本类的显式检查双重保证；``native_session_id`` 为空（懒创建）时不参与唯一性。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Sequence

from app.conversations.models import Conversation
from app.conversations.repository import DEFAULT_RECENT_LIMIT
from app.errors import DomainInvariantError
from app.persistence.sqlite.codec import (
    dump_json,
    from_iso,
    to_iso,
    load_str_tuple,
    require_datetime,
    require_iso,
)
from app.persistence.sqlite.database import SqliteDatabase


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        project_id=row["project_id"],
        agent_binding_id=row["agent_binding_id"],
        title=row["title"],
        native_session_id=row["native_session_id"],
        native_session_head_id=row["native_session_head_id"],
        native_session_segments=load_str_tuple(row["native_session_segments_json"]),
        preferred_surface=row["preferred_surface"],
        model_id=row["model_id"],
        provider_id=row["provider_id"],
        reasoning_mode=row["reasoning_mode"],
        execution_mode=row["execution_mode"],
        approval_mode=row["approval_mode"],
        state=row["state"],
        archived_at=from_iso(row["archived_at"]),
        origin=row["origin"],
        visibility=row["visibility"],
        retention=row["retention"],
        created_by_collaboration_id=row["created_by_collaboration_id"],
        created_at=require_datetime(row["created_at"]),
        updated_at=require_datetime(row["updated_at"]),
    )


class SqliteConversationRepository:
    """v1.0 §11.1 ``conversations``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, conversation_id: str) -> Conversation | None:
        row = self._db.query_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        return _row_to_conversation(row) if row is not None else None

    async def list_for_project(
        self, project_id: str, *, include_group_only: bool = False, archived: bool = False
    ) -> Sequence[Conversation]:
        clauses = ["project_id = ?", "archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
        if not include_group_only:
            clauses.append("visibility = 'project_visible'")
        rows = self._db.query_all(
            "SELECT * FROM conversations WHERE " + " AND ".join(clauses) + " ORDER BY created_at, id",
            (project_id,),
        )
        return tuple(_row_to_conversation(row) for row in rows)

    async def list_for_binding(self, agent_binding_id: str) -> Sequence[Conversation]:
        """N §9.2：同一 Binding 可以有多条并行 Conversation。"""
        return tuple(
            _row_to_conversation(row)
            for row in self._db.query_all(
                "SELECT * FROM conversations WHERE agent_binding_id = ?"
                " ORDER BY created_at, id",
                (agent_binding_id,),
            )
        )

    async def list_recent(
        self,
        *,
        updated_after: datetime | None = None,
        project_id: str | None = None,
        limit: int = DEFAULT_RECENT_LIMIT,
        include_group_only: bool = False,
        archived: bool = False,
    ) -> Sequence[Conversation]:
        """跨项目按 ``updated_at`` 倒序取前 N（批次八第 1 件）。

        ``ORDER BY updated_at DESC, id DESC`` 与 ``conversations_by_recency``
        索引同序：同一毫秒的两条也有稳定次序，增量刷新不会来回跳。
        时间戳在库里是 ISO-8601 UTC 字符串（见 :func:`~app.persistence.sqlite.codec.require_iso`），
        字典序即时序，因此 ``updated_at > ?`` 可以直接交给 SQL 比。
        """
        clauses: list[str] = ["archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if not include_group_only:
            clauses.append("visibility = 'project_visible'")
        if updated_after is not None:
            clauses.append("updated_at > ?")
            params.append(require_iso(updated_after))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, int(limit)))
        return tuple(
            _row_to_conversation(row)
            for row in self._db.query_all(
                "SELECT * FROM conversations"
                f"{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                tuple(params),
            )
        )

    async def list_for_collaboration(
        self, collaboration_id: str
    ) -> Sequence[Conversation]:
        return tuple(
            _row_to_conversation(row)
            for row in self._db.query_all(
                "SELECT * FROM conversations WHERE created_by_collaboration_id = ?"
                " ORDER BY created_at, id",
                (collaboration_id,),
            )
        )

    async def get_by_native_session(
        self, agent_binding_id: str, native_session_id: str
    ) -> Conversation | None:
        """v1.0 §8.7：映射必须确定，断线重连不得创建重复 Conversation。"""
        row = self._db.query_one(
            "SELECT * FROM conversations"
            " WHERE agent_binding_id = ? AND native_session_id = ?",
            (agent_binding_id, native_session_id),
        )
        return _row_to_conversation(row) if row is not None else None

    async def save(self, conversation: Conversation) -> Conversation:
        if conversation.native_session_id is not None:
            existing = await self.get_by_native_session(
                conversation.agent_binding_id, conversation.native_session_id
            )
            if existing is not None and existing.id != conversation.id:
                raise DomainInvariantError(
                    "同一 Binding 下一个原生 Session 只能映射一条 Conversation"
                    f"（v1.0 §8.7）：{conversation.native_session_id!r}"
                )
        self._db.run(
            """
            INSERT INTO conversations (id, project_id, agent_binding_id, title,
                                       native_session_id, native_session_head_id,
                                       native_session_segments_json,
                                       preferred_surface, model_id, provider_id,
                                       reasoning_mode, execution_mode, approval_mode, state, origin, visibility,
                                       retention, created_by_collaboration_id,
                                       created_at, updated_at, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project_id                   = excluded.project_id,
                agent_binding_id             = excluded.agent_binding_id,
                title                        = excluded.title,
                native_session_id            = excluded.native_session_id,
                native_session_head_id       = excluded.native_session_head_id,
                native_session_segments_json = excluded.native_session_segments_json,
                preferred_surface            = excluded.preferred_surface,
                model_id                     = excluded.model_id,
                provider_id                  = excluded.provider_id,
                reasoning_mode               = excluded.reasoning_mode,
                execution_mode               = excluded.execution_mode,
                approval_mode                = excluded.approval_mode,
                state                        = excluded.state,
                origin                       = excluded.origin,
                visibility                   = excluded.visibility,
                retention                    = excluded.retention,
                created_by_collaboration_id  = excluded.created_by_collaboration_id,
                updated_at                   = excluded.updated_at,
                archived_at                  = excluded.archived_at
            """,
            (
                conversation.id,
                conversation.project_id,
                conversation.agent_binding_id,
                conversation.title,
                conversation.native_session_id,
                conversation.native_session_head_id,
                dump_json(list(conversation.native_session_segments)),
                conversation.preferred_surface,
                conversation.model_id,
                conversation.provider_id,
                conversation.reasoning_mode,
                conversation.execution_mode,
                conversation.approval_mode,
                conversation.state,
                conversation.origin,
                conversation.visibility,
                conversation.retention,
                conversation.created_by_collaboration_id,
                require_iso(conversation.created_at),
                require_iso(conversation.updated_at),
                to_iso(conversation.archived_at),
            ),
            conflict_message=(
                "同一 Binding 下一个原生 Session 只能映射一条 Conversation（v1.0 §8.7）"
            ),
        )
        return conversation

    async def delete(self, conversation_id: str) -> None:
        """v1.0 §16.6：删除 Conversation 不删除原生 Session，也不级联删别的表。"""
        self._db.run("DELETE FROM conversations WHERE id = ?", (conversation_id,))


__all__ = ["SqliteConversationRepository"]
