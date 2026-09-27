"""SQLite 实现：``collaboration_sessions`` / ``collaboration_members`` /
``collaboration_messages``。

AD-13：Group 时间线的持久化归 Group 自己
----------------------------------------
``collaboration_messages`` 与 ``event_store`` 是两回事：前者是 Group 拥有的数据
（消息、Context Packet、写回），**没有** ``expires_at``，不随重放缓冲过期；后者是
可整表丢弃的短期缓冲。Group 关闭时对 Context Packet 做归档快照，也是往这张表里
写，而不是指望缓冲还在。

N §9.4：成员列表是动态的，不是创建时快照 —— 因此成员是独立可增删的行，
没有任何「成员列表 JSON」形态的列。
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from app.collaboration.models import (
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
    RoomThread,
)
from app.errors import DomainInvariantError
from app.persistence.sqlite.codec import (
    dump_json,
    from_iso,
    load_mapping,
    require_datetime,
    require_iso,
    to_iso,
)
from app.persistence.sqlite.database import SqliteDatabase


def _optional_column(row: sqlite3.Row, name: str) -> object | None:
    """迁移加的列在**旧行**上读不到时回 ``None``，而不是让整条查询炸。

    真机上 ``SELECT *`` 取回来的一定有这一列（迁移跑过了），但契约测试与工具脚本
    会拿手写的行喂进来；少一列不该表现成 ``IndexError``。
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_to_session(row: sqlite3.Row) -> CollaborationSession:
    thread_json = _optional_column(row, "thread_json")
    return CollaborationSession(
        id=row["id"],
        title=row["title"],
        home_project_id=row["home_project_id"],
        status=row["status"],
        context_policy=load_mapping(row["context_policy_json"]),
        settings=load_mapping(_optional_column(row, "settings_json")),  # type: ignore[arg-type]
        # NULL = 这个组还没有组长（老组，或者一个 active 成员都没有）。
        leader_member_id=_optional_column(row, "leader_member_id"),  # type: ignore[arg-type]
        # NULL = 这个组还没转过任何一条线程（与「转过、现在停了」是两句话）。
        thread=(
            RoomThread.model_validate(load_mapping(thread_json))  # type: ignore[arg-type]
            if thread_json
            else None
        ),
        created_at=require_datetime(row["created_at"]),
        updated_at=require_datetime(row["updated_at"]),
        closed_at=from_iso(row["closed_at"]),
    )


class SqliteCollaborationSessionRepository:
    """v1.0 §11.1 ``collaboration_sessions``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, collaboration_id: str) -> CollaborationSession | None:
        row = self._db.query_one(
            "SELECT * FROM collaboration_sessions WHERE id = ?", (collaboration_id,)
        )
        return _row_to_session(row) if row is not None else None

    async def list_active(self) -> Sequence[CollaborationSession]:
        return tuple(
            _row_to_session(row)
            for row in self._db.query_all(
                "SELECT * FROM collaboration_sessions"
                " WHERE status IN ('active', 'minimized')"
                " ORDER BY created_at, id"
            )
        )

    async def list_all(self) -> Sequence[CollaborationSession]:
        """含已关闭 / 已归档的全部 Group（``GET /api/groups?status=all``）。"""
        return tuple(
            _row_to_session(row)
            for row in self._db.query_all(
                "SELECT * FROM collaboration_sessions ORDER BY created_at, id"
            )
        )

    async def list_for_project(
        self, project_id: str
    ) -> Sequence[CollaborationSession]:
        return tuple(
            _row_to_session(row)
            for row in self._db.query_all(
                "SELECT * FROM collaboration_sessions WHERE home_project_id = ?"
                " ORDER BY created_at, id",
                (project_id,),
            )
        )

    async def save(self, session: CollaborationSession) -> CollaborationSession:
        self._db.run(
            """
            INSERT INTO collaboration_sessions (id, title, home_project_id, status,
                                                context_policy_json, settings_json,
                                                leader_member_id, thread_json,
                                                created_at, updated_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title               = excluded.title,
                home_project_id     = excluded.home_project_id,
                status              = excluded.status,
                context_policy_json = excluded.context_policy_json,
                settings_json       = excluded.settings_json,
                leader_member_id    = excluded.leader_member_id,
                thread_json         = excluded.thread_json,
                updated_at          = excluded.updated_at,
                closed_at           = excluded.closed_at
            """,
            (
                session.id,
                session.title,
                session.home_project_id,
                session.status,
                dump_json(session.context_policy),
                dump_json(session.settings),
                session.leader_member_id,
                # 没有线程就写 NULL，不写一个 `{}`：读回来要分得出「还没转过」。
                (
                    dump_json(session.thread.model_dump(mode="json", by_alias=True))
                    if session.thread is not None
                    else None
                ),
                require_iso(session.created_at),
                require_iso(session.updated_at),
                to_iso(session.closed_at),
            ),
        )
        return session


def _row_to_member(row: sqlite3.Row) -> CollaborationMember:
    return CollaborationMember(
        id=row["id"],
        collaboration_session_id=row["collaboration_session_id"],
        conversation_id=row["conversation_id"],
        source_conversation_id=_optional_column(row, "source_conversation_id"),
        join_mode=row["join_mode"],
        role_label=row["role_label"],
        participation_state=row["participation_state"],
        isolation_mode=row["isolation_mode"],
        worktree_or_runtime_ref=row["worktree_or_runtime_ref"],
        joined_at=require_datetime(row["joined_at"]),
        left_at=from_iso(row["left_at"]),
        last_delivered_sequence=(
            None
            if _optional_column(row, "last_delivered_sequence") is None
            else int(row["last_delivered_sequence"])
        ),
    )


class SqliteCollaborationMemberRepository:
    """v1.0 §11.1 ``collaboration_members``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, member_id: str) -> CollaborationMember | None:
        row = self._db.query_one(
            "SELECT * FROM collaboration_members WHERE id = ?", (member_id,)
        )
        return _row_to_member(row) if row is not None else None

    async def list_for_collaboration(
        self, collaboration_id: str, *, include_left: bool = False
    ) -> Sequence[CollaborationMember]:
        if include_left:
            rows = self._db.query_all(
                "SELECT * FROM collaboration_members"
                " WHERE collaboration_session_id = ? ORDER BY joined_at, id",
                (collaboration_id,),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM collaboration_members"
                " WHERE collaboration_session_id = ? AND participation_state <> 'left'"
                " ORDER BY joined_at, id",
                (collaboration_id,),
            )
        return tuple(_row_to_member(row) for row in rows)

    async def list_for_conversation(
        self, conversation_id: str
    ) -> Sequence[CollaborationMember]:
        """N §9.9：一条 Conversation 可参加多个 Group。"""
        return tuple(
            _row_to_member(row)
            for row in self._db.query_all(
                "SELECT * FROM collaboration_members WHERE conversation_id = ?"
                " ORDER BY joined_at, id",
                (conversation_id,),
            )
        )

    async def save(self, member: CollaborationMember) -> CollaborationMember:
        # N §9.2：唯一性只按 conversation_id 判定，不按 binding —— 同一 Binding
        # 可以在一个 Group 里同时开多条 Conversation，各自是独立成员。
        if member.participation_state != "left":
            row = self._db.query_one(
                "SELECT id FROM collaboration_members"
                " WHERE collaboration_session_id = ? AND COALESCE(source_conversation_id, conversation_id) = ?"
                "   AND participation_state <> 'left' AND id <> ?",
                (member.collaboration_session_id, member.source_conversation_id or member.conversation_id, member.id),
            )
            if row is not None:
                raise DomainInvariantError(
                    f"同一 Group 内 conversation 重复加入：{member.conversation_id!r}"
                )
        self._db.run(
            """
            INSERT INTO collaboration_members (id, collaboration_session_id,
                                               conversation_id, join_mode, role_label,
                                               participation_state, isolation_mode,
                                               worktree_or_runtime_ref, joined_at,
                                               left_at, last_delivered_sequence, source_conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                collaboration_session_id = excluded.collaboration_session_id,
                conversation_id          = excluded.conversation_id,
                join_mode                = excluded.join_mode,
                role_label               = excluded.role_label,
                participation_state      = excluded.participation_state,
                isolation_mode           = excluded.isolation_mode,
                worktree_or_runtime_ref  = excluded.worktree_or_runtime_ref,
                joined_at                = excluded.joined_at,
                left_at                  = excluded.left_at,
                last_delivered_sequence  = excluded.last_delivered_sequence,
                source_conversation_id = excluded.source_conversation_id
            """,
            (
                member.id,
                member.collaboration_session_id,
                member.conversation_id,
                member.join_mode,
                member.role_label,
                member.participation_state,
                member.isolation_mode,
                member.worktree_or_runtime_ref,
                require_iso(member.joined_at),
                to_iso(member.left_at),
                member.last_delivered_sequence,
                member.source_conversation_id,
            ),
        )
        return member

    async def delete(self, member_id: str) -> None:
        """N §9.4：成员移除后原 Conversation 与 Native Session 不被删除。"""
        self._db.run("DELETE FROM collaboration_members WHERE id = ?", (member_id,))


def _row_to_message(row: sqlite3.Row) -> CollaborationMessage:
    return CollaborationMessage(
        id=row["id"],
        collaboration_session_id=row["collaboration_session_id"],
        sequence=int(row["sequence"]),
        kind=row["kind"],
        author_type=row["author_type"],
        author_member_id=row["author_member_id"],
        conversation_id=row["conversation_id"],
        content=row["content"],
        metadata=load_mapping(row["metadata_json"]),
        created_at=require_datetime(row["created_at"]),
    )


class SqliteCollaborationMessageRepository:
    """v1.0 §11.1 ``collaboration_messages``（AD-13）。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, message_id: str) -> CollaborationMessage | None:
        row = self._db.query_one(
            "SELECT * FROM collaboration_messages WHERE id = ?", (message_id,)
        )
        return _row_to_message(row) if row is not None else None

    async def list_for_collaboration(
        self,
        collaboration_id: str,
        *,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Sequence[CollaborationMessage]:
        sql = (
            "SELECT * FROM collaboration_messages WHERE collaboration_session_id = ?"
        )
        parameters: list[object] = [collaboration_id]
        if after_sequence is not None:
            sql += " AND sequence > ?"
            parameters.append(after_sequence)
        if before_sequence is not None:
            sql += " AND sequence < ?"
            parameters.append(before_sequence)
        if kinds is not None:
            kind_list = tuple(kinds)
            if not kind_list:
                return ()
            sql += f" AND kind IN ({','.join('?' for _ in kind_list)})"
            parameters.extend(kind_list)
        if limit is not None:
            if limit <= 0:
                return ()
            # 「最近的 N 条」：让 SQLite 倒着取再翻回升序，而不是把整段读进内存
            # 再切尾巴——时间线会越攒越长，翻页的代价不该跟着它长。
            sql += " ORDER BY sequence DESC LIMIT ?"
            parameters.append(limit)
            rows = list(self._db.query_all(sql, parameters))
            rows.reverse()
            return tuple(_row_to_message(row) for row in rows)
        sql += " ORDER BY sequence"
        return tuple(_row_to_message(row) for row in self._db.query_all(sql, parameters))

    async def next_sequence(self, collaboration_id: str) -> int:
        row = self._db.query_one(
            "SELECT MAX(sequence) AS top FROM collaboration_messages"
            " WHERE collaboration_session_id = ?",
            (collaboration_id,),
        )
        top = row["top"] if row is not None else None
        return 0 if top is None else int(top) + 1

    async def append(self, message: CollaborationMessage) -> CollaborationMessage:
        row = self._db.query_one(
            "SELECT id FROM collaboration_messages"
            " WHERE collaboration_session_id = ? AND sequence = ? AND id <> ?",
            (message.collaboration_session_id, message.sequence, message.id),
        )
        if row is not None:
            raise DomainInvariantError(
                "同一 Group 内 sequence 必须唯一："
                f"{message.collaboration_session_id!r}#{message.sequence}"
            )
        self._db.run(
            """
            INSERT INTO collaboration_messages (id, collaboration_session_id, sequence,
                                                kind, author_type, author_member_id,
                                                conversation_id, content,
                                                metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind             = excluded.kind,
                author_type      = excluded.author_type,
                author_member_id = excluded.author_member_id,
                conversation_id  = excluded.conversation_id,
                content          = excluded.content,
                metadata_json    = excluded.metadata_json
            """,
            (
                message.id,
                message.collaboration_session_id,
                message.sequence,
                message.kind,
                message.author_type,
                message.author_member_id,
                message.conversation_id,
                message.content,
                dump_json(message.metadata),
                require_iso(message.created_at),
            ),
            conflict_message=(
                "同一 Group 内 sequence 必须唯一："
                f"{message.collaboration_session_id!r}#{message.sequence}"
            ),
        )
        return message

    async def delete_for_collaboration(self, collaboration_id: str) -> int:
        return self._db.run(
            "DELETE FROM collaboration_messages WHERE collaboration_session_id = ?",
            (collaboration_id,),
        )


__all__ = [
    "SqliteCollaborationMemberRepository",
    "SqliteCollaborationMessageRepository",
    "SqliteCollaborationSessionRepository",
]
