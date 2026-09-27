"""``projection_results`` 的 SQLite 实现（批次二十四 / AD-149）。"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from app.capabilities.projection_records import (
    RETENTION,
    ProjectionRecord,
    ProjectionResultRepository,
)
from app.persistence.sqlite.codec import (
    dump_json,
    load_mapping,
    require_datetime,
    require_iso,
)
from app.persistence.sqlite.database import SqliteDatabase


def _row_to_record(row: sqlite3.Row) -> ProjectionRecord:
    return ProjectionRecord(
        id=row["id"],
        binding_id=row["binding_id"],
        project_id=row["project_id"],
        applied_at=require_datetime(row["applied_at"]),
        summary=load_mapping(row["summary_json"]),
    )


class SqliteProjectionResultRepository(ProjectionResultRepository):
    """每写一行就把该 Binding 修剪到最近 :data:`RETENTION` 条。

    修剪用一条 ``DELETE … WHERE id NOT IN (SELECT … LIMIT n)``，而不是先查再删：
    这张表的写入者只有物化端点，但「查出来再删」在并发下会删掉刚写进来的行。
    """

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save(self, record: ProjectionRecord) -> ProjectionRecord:
        self._db.run(
            """
            INSERT INTO projection_results (id, binding_id, project_id,
                                            applied_at, summary_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                binding_id   = excluded.binding_id,
                project_id   = excluded.project_id,
                applied_at   = excluded.applied_at,
                summary_json = excluded.summary_json
            """,
            (
                record.id,
                record.binding_id,
                record.project_id,
                require_iso(record.applied_at),
                dump_json(record.summary),
            ),
        )
        self._db.run(
            """
            DELETE FROM projection_results
             WHERE binding_id = ?
               AND id NOT IN (
                   SELECT id FROM projection_results
                    WHERE binding_id = ?
                    ORDER BY applied_at DESC, id DESC
                    LIMIT ?
               )
            """,
            (record.binding_id, record.binding_id, RETENTION),
        )
        return record

    async def list_for_binding(
        self, binding_id: str, *, limit: int = RETENTION
    ) -> Sequence[ProjectionRecord]:
        rows = self._db.query_all(
            "SELECT * FROM projection_results WHERE binding_id = ?"
            " ORDER BY applied_at DESC, id DESC LIMIT ?",
            (binding_id, limit),
        )
        return tuple(_row_to_record(row) for row in rows)


__all__ = ["SqliteProjectionResultRepository"]
