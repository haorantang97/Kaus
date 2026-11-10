"""Project 记忆的 SQLite 实现（领域规则在 :mod:`app.projects.memory`）。"""

from __future__ import annotations

from typing import Sequence

from app.persistence.sqlite.codec import dump_json, load_mapping
from app.persistence.sqlite.database import SqliteDatabase
from app.projects.memory import ACTIVE

_COLUMNS = ("id", "project_id", "kind", "topic", "body", "status", "source", "created_at", "updated_at",
            "invalidated_at", "replaced_by", "memory_type", "expires_at")
_KEYS = ("id", "projectId", "kind", "topic", "body", "status", "source", "createdAt", "updatedAt",
         "invalidatedAt", "replacedBy", "type", "expiresAt")


def _entry(row) -> dict:
    return {key: row[column] for key, column in zip(_KEYS, _COLUMNS)}


def _values(entry: dict) -> tuple:
    # Entries saved before 2026-11-11 (history snapshots, undo) carry no type: they are facts.
    return tuple((entry.get(key) or "fact") if key == "type" else entry.get(key) for key in _KEYS)


class SqliteMemoryRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def list_for_projects(self, project_ids: Sequence[str], statuses: Sequence[str] = (ACTIVE,)) -> list[dict]:
        if not project_ids or not statuses:
            return []
        ids = ",".join("?" for _ in project_ids)
        states = ",".join("?" for _ in statuses)
        rows = self._db.query_all(
            f"SELECT * FROM project_memories WHERE project_id IN ({ids}) AND status IN ({states}) ORDER BY created_at",
            (*project_ids, *statuses))
        return [_entry(row) for row in rows]

    async def get(self, memory_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM project_memories WHERE id = ?", (memory_id,))
        return _entry(row) if row is not None else None

    async def save(self, entry: dict) -> dict:
        values = _values(entry)
        updates = ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
        self._db.run(
            f"INSERT INTO project_memories ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})"
            f" ON CONFLICT(id) DO UPDATE SET {updates}", values)
        return dict(entry)

    async def add_history(self, record: dict) -> None:
        self._db.run(
            "INSERT INTO project_memory_history (id, memory_id, project_id, action, before_json, after_json, actor, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record["id"], record["memoryId"], record["projectId"], record["action"],
             dump_json(record["before"]) if record["before"] is not None else None,
             dump_json(record["after"]) if record["after"] is not None else None, record["actor"], record["at"]))

    @staticmethod
    def _record(row) -> dict:
        return {"id": row["id"], "memoryId": row["memory_id"], "projectId": row["project_id"], "action": row["action"],
                "before": load_mapping(row["before_json"]) if row["before_json"] else None,
                "after": load_mapping(row["after_json"]) if row["after_json"] else None,
                "actor": row["actor"], "at": row["at"]}

    async def history(self, project_id: str, limit: int = 50) -> list[dict]:
        rows = self._db.query_all(
            "SELECT * FROM project_memory_history WHERE project_id = ? ORDER BY at DESC, rowid DESC LIMIT ?",
            (project_id, limit))
        return [self._record(row) for row in rows]

    async def get_history(self, history_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM project_memory_history WHERE id = ?", (history_id,))
        return self._record(row) if row is not None else None


__all__ = ["SqliteMemoryRepository"]
