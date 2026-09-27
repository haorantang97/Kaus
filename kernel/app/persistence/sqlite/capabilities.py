"""SQLite 实现：``project_capabilities``。

R-01：backend-scoped 能力（``<backend>:<name>``）与通用能力共用**同一张表**，
所以这里没有任何按 backend 分表的痕迹；按 backend 过滤发生在 Resolver 输出侧。

自然键是 ``(project_id, capability_type, capability_id)``（v1.0 §5.2：同一
Project 的同一条能力只应有一条赋值），主键是 ``id``。两者都要维护，因为上层可能
换一个新的 assignment id 重写同一条赋值——此时旧行必须让位，而不是并存成两条。
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

from app.capabilities.models import ProjectCapability
from app.errors import DomainInvariantError
from app.persistence.sqlite.codec import (
    dump_json,
    load_mapping,
    require_datetime,
    require_iso,
)
from app.persistence.sqlite.database import SqliteDatabase


def _row_to_assignment(row: sqlite3.Row) -> ProjectCapability:
    return ProjectCapability(
        id=row["id"],
        project_id=row["project_id"],
        capability_type=row["capability_type"],
        capability_id=row["capability_id"],
        assignment_mode=row["assignment_mode"],
        config=load_mapping(row["config_json"]),
        version=row["version"],
        source_project_id=row["source_project_id"],
        created_at=require_datetime(row["created_at"]),
        updated_at=require_datetime(row["updated_at"]),
    )


class SqliteProjectCapabilityRepository:
    """v1.0 §11.1 ``project_capabilities``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, assignment_id: str) -> ProjectCapability | None:
        row = self._db.query_one(
            "SELECT * FROM project_capabilities WHERE id = ?", (assignment_id,)
        )
        return _row_to_assignment(row) if row is not None else None

    async def list_for_project(self, project_id: str) -> Sequence[ProjectCapability]:
        return tuple(
            _row_to_assignment(row)
            for row in self._db.query_all(
                "SELECT * FROM project_capabilities WHERE project_id = ?"
                " ORDER BY capability_type, capability_id",
                (project_id,),
            )
        )

    async def list_for_projects(
        self, project_ids: Sequence[str]
    ) -> Mapping[str, Sequence[ProjectCapability]]:
        """一次取整条祖先链，喂给 Capability Resolver（R-01 树计算层）。"""
        ordered = list(project_ids)
        result: dict[str, Sequence[ProjectCapability]] = {pid: () for pid in ordered}
        if not ordered:
            return result
        placeholders = ",".join("?" for _ in ordered)
        rows = self._db.query_all(
            f"SELECT * FROM project_capabilities WHERE project_id IN ({placeholders})"
            " ORDER BY capability_type, capability_id",
            ordered,
        )
        grouped: dict[str, list[ProjectCapability]] = {pid: [] for pid in ordered}
        for row in rows:
            grouped[row["project_id"]].append(_row_to_assignment(row))
        return {pid: tuple(grouped[pid]) for pid in ordered}

    async def save(self, assignment: ProjectCapability) -> ProjectCapability:
        if assignment.assignment_mode == "block" and assignment.config:
            raise DomainInvariantError("block 赋值不得携带 config")
        with self._db.transaction() as connection:
            # 自然键覆盖：同一 (project, type, capability_id) 换了 assignment id
            # 也只留一条，避免同一条能力出现两份赋值。
            connection.execute(
                "DELETE FROM project_capabilities"
                " WHERE project_id = ? AND capability_type = ? AND capability_id = ?"
                "   AND id <> ?",
                (
                    assignment.project_id,
                    assignment.capability_type,
                    assignment.capability_id,
                    assignment.id,
                ),
            )
            connection.execute(
                """
                INSERT INTO project_capabilities (id, project_id, capability_type,
                                                  capability_id, assignment_mode,
                                                  config_json, version,
                                                  source_project_id,
                                                  created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id        = excluded.project_id,
                    capability_type   = excluded.capability_type,
                    capability_id     = excluded.capability_id,
                    assignment_mode   = excluded.assignment_mode,
                    config_json       = excluded.config_json,
                    version           = excluded.version,
                    source_project_id = excluded.source_project_id,
                    updated_at        = excluded.updated_at
                """,
                (
                    assignment.id,
                    assignment.project_id,
                    assignment.capability_type,
                    assignment.capability_id,
                    assignment.assignment_mode,
                    dump_json(assignment.config),
                    assignment.version,
                    assignment.source_project_id,
                    require_iso(assignment.created_at),
                    require_iso(assignment.updated_at),
                ),
            )
        return assignment

    async def delete(self, assignment_id: str) -> None:
        self._db.run(
            "DELETE FROM project_capabilities WHERE id = ?", (assignment_id,)
        )


__all__ = ["SqliteProjectCapabilityRepository"]
