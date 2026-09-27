"""SQLite 实现：``projects`` / ``backends`` / ``agent_bindings``。

三条必须逐字落实的裁决
----------------------
- **AD/R-05**：``Project.slug`` 不可变。这里是**存储层的第二道**：
  :meth:`SqliteProjectRepository.save` 先读回旧行比对 slug，不一致就抛
  :class:`~app.errors.SlugImmutableError`；即便有人绕过 Repository 直接 UPDATE，
  迁移里的 ``projects_slug_is_immutable`` 触发器还会再拦一次。
  写入统一走 ``INSERT ... ON CONFLICT(id) DO UPDATE``（而不是 ``INSERT OR
  REPLACE``）——后者是「删了再插」，会绕过 UPDATE 触发器，那道闸就白设了。
- **AD-01**：Binding 主键支持四段式 ``binding:<slug>:<backend>:<discriminator>``。
  存储层只把它当 TEXT 主键，形态校验在 :mod:`app.ids`。
- **AD-09**：同一 Project 可存多条同 backend Binding。因此这里**没有**
  ``(project_id, backend_id)`` 唯一约束，按 backend 的查询一律返回序列。
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from app.errors import DomainInvariantError, SlugImmutableError
from app.persistence.sqlite.codec import (
    dump_json,
    from_bool,
    from_iso,
    load_mapping,
    require_datetime,
    require_iso,
    to_bool,
    to_iso,
)
from app.persistence.sqlite.database import SqliteDatabase
from app.projects.models import AgentBinding, Backend, CapabilitySnapshot, Project
from runtime.capability_matrix import BackendCapabilities

def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        parent_project_id=row["parent_project_id"],
        workspace_root=row["workspace_root"],
        status=row["status"],
        metadata=load_mapping(row["metadata_json"]),
        created_at=require_datetime(row["created_at"]),
        updated_at=require_datetime(row["updated_at"]),
    )


class SqliteProjectRepository:
    """v1.0 §11.1 ``projects``。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, project_id: str) -> Project | None:
        row = self._db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row_to_project(row) if row is not None else None

    async def get_by_slug(self, slug: str) -> Project | None:
        row = self._db.query_one("SELECT * FROM projects WHERE slug = ?", (slug,))
        return _row_to_project(row) if row is not None else None

    async def list_all(self) -> Sequence[Project]:
        return tuple(
            _row_to_project(row)
            for row in self._db.query_all("SELECT * FROM projects ORDER BY slug")
        )

    async def list_children(self, parent_project_id: str | None) -> Sequence[Project]:
        if parent_project_id is None:
            rows = self._db.query_all(
                "SELECT * FROM projects WHERE parent_project_id IS NULL ORDER BY slug"
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM projects WHERE parent_project_id = ? ORDER BY slug",
                (parent_project_id,),
            )
        return tuple(_row_to_project(row) for row in rows)

    async def ancestry(self, project_id: str) -> Sequence[Project]:
        """从根到该 Project（含自身）。逐级上溯并检测环。"""
        chain: list[Project] = []
        seen: set[str] = set()
        cursor = await self.get(project_id)
        while cursor is not None:
            if cursor.id in seen:
                raise DomainInvariantError(f"Project 树出现环：{cursor.id!r}")
            seen.add(cursor.id)
            chain.append(cursor)
            cursor = (
                await self.get(cursor.parent_project_id)
                if cursor.parent_project_id
                else None
            )
        chain.reverse()
        return tuple(chain)

    async def save(self, project: Project) -> Project:
        existing = await self.get(project.id)
        if existing is not None and existing.slug != project.slug:
            raise SlugImmutableError(
                f"R-05：project.slug 不可变（{existing.slug!r} → {project.slug!r}）"
            )
        clash = await self.get_by_slug(project.slug)
        if clash is not None and clash.id != project.id:
            raise DomainInvariantError(f"slug 已被占用：{project.slug!r}")
        self._db.run(
            """
            INSERT INTO projects (id, slug, display_name, parent_project_id,
                                  workspace_root, status, metadata_json,
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                slug              = excluded.slug,
                display_name      = excluded.display_name,
                parent_project_id = excluded.parent_project_id,
                workspace_root    = excluded.workspace_root,
                status            = excluded.status,
                metadata_json     = excluded.metadata_json,
                updated_at        = excluded.updated_at
            """,
            (
                project.id,
                project.slug,
                project.display_name,
                project.parent_project_id,
                project.workspace_root,
                project.status,
                dump_json(project.metadata),
                require_iso(project.created_at),
                require_iso(project.updated_at),
            ),
            conflict_message="projects 违反唯一约束（slug 或 R-05 触发器）",
        )
        return project

    async def delete(self, project_id: str) -> None:
        """C-4 / v1.0 §16.6：只删 Dashboard 自己的行，不碰任何原生数据。"""
        self._db.run("DELETE FROM projects WHERE id = ?", (project_id,))


def _row_to_backend(row: sqlite3.Row) -> Backend:
    return Backend(
        id=row["id"],
        key=row["key"],
        display_name=row["display_name"],
        driver_kind=row["driver_kind"],
        installed=to_bool(row["installed"]),
        version=row["installed_version"],
        driver_version=row["driver_version"],
        capabilities=BackendCapabilities.model_validate_json(
            row["capabilities_json"] or "{}"
        ),
        # AD-28：老库里这一列可能是 NULL（提升为领域字段之前写的行），
        # 读成 "unknown"——那正是它当时的含义：还没探测过。
        probe_state=row["probe_state"] or "unknown",
        # AD-127：迁移 5 之前的行没有这两列，读成 None——「没有说明 / 从未成功
        # 探测过」，与它们当时的含义一致。
        probe_message=_optional_column(row, "probe_message"),
        capability_snapshot=_row_to_capability_snapshot(row),
        last_probe_at=from_iso(row["last_probe_at"]),
    )


def _optional_column(row: sqlite3.Row, name: str) -> str | None:
    """读一列可能还不存在的列（迁移落后的库）。缺列就是 ``None``，不是错。"""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return None
    return value


def _row_to_capability_snapshot(row: sqlite3.Row) -> CapabilitySnapshot | None:
    """AD-127：``capability_snapshot`` 列 → 领域快照。

    列不存在、为空、或存的 JSON 已经读不回来时一律返回 ``None``：宁可退回
    「没有缓存」（界面上就是全 unknown 的老行为），也不要拿一份解析不出的东西
    冒充上次的探测结论。
    """
    raw = _optional_column(row, "capability_snapshot")
    if not raw:
        return None
    try:
        return CapabilitySnapshot.model_validate_json(raw)
    except ValueError:
        return None


class SqliteBackendRepository:
    """v1.0 §11.1 ``backends``。

    AD-127：``capability_snapshot`` / ``probe_message`` 与 ``probe_state`` 同处一行
    ——「上次探到了什么」与「这次探得怎么样」必须一起落库，否则进程一重启，缓存
    就没了，界面又会退回全 unknown。

    AD-28：``probe_state`` 已是领域字段（``unknown | available | unavailable |
    degraded``），由 Registry 的探测结果写回，这一列不再留空。老库里写成 NULL
    的行读回来是 ``unknown``，语义与当初一致（那时就是「没探测过」）。
    """

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, backend_id: str) -> Backend | None:
        row = self._db.query_one("SELECT * FROM backends WHERE id = ?", (backend_id,))
        return _row_to_backend(row) if row is not None else None

    async def list_all(self) -> Sequence[Backend]:
        return tuple(
            _row_to_backend(row)
            for row in self._db.query_all("SELECT * FROM backends ORDER BY key")
        )

    async def save(self, backend: Backend) -> Backend:
        self._db.run(
            """
            INSERT INTO backends (id, key, display_name, driver_kind, installed,
                                  installed_version, driver_version, probe_state,
                                  probe_message, capabilities_json,
                                  capability_snapshot, last_probe_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                key                = excluded.key,
                display_name       = excluded.display_name,
                driver_kind        = excluded.driver_kind,
                installed          = excluded.installed,
                installed_version  = excluded.installed_version,
                driver_version     = excluded.driver_version,
                probe_state        = excluded.probe_state,
                probe_message      = excluded.probe_message,
                capabilities_json  = excluded.capabilities_json,
                capability_snapshot = excluded.capability_snapshot,
                last_probe_at      = excluded.last_probe_at
            """,
            (
                backend.id,
                backend.key,
                backend.display_name,
                backend.driver_kind,
                from_bool(backend.installed),
                backend.version,
                backend.driver_version,
                backend.probe_state,
                backend.probe_message,
                backend.capabilities.model_dump_json(by_alias=True),
                (
                    backend.capability_snapshot.model_dump_json(by_alias=True)
                    if backend.capability_snapshot is not None
                    else None
                ),
                to_iso(backend.last_probe_at),
            ),
        )
        return backend


def _row_to_binding(row: sqlite3.Row) -> AgentBinding:
    return AgentBinding(
        id=row["id"],
        project_id=row["project_id"],
        backend_id=row["backend_id"],
        display_name=row["display_name"],
        # AD-02：字段名与公共层一致，不出现任何 Agent 私有概念。
        native_scope_ref=row["native_scope_ref"],
        enabled=to_bool(row["enabled"]),
        is_default=to_bool(row["is_default"]),
        default_model_id=row["default_model_id"],
        default_provider_id=row["default_provider_id"],
        runtime_config=load_mapping(row["runtime_config_json"]),
        compatibility_state=row["compatibility_state"],
        # 批次八第 2 件：老库里这一列是迁移加的，默认 'imported'。
        origin=row["origin"] or "imported",
        created_at=require_datetime(row["created_at"]),
        updated_at=require_datetime(row["updated_at"]),
    )


class SqliteAgentBindingRepository:
    """v1.0 §11.1 ``agent_bindings``；AD-01 四段式 ID + AD-09 多 Binding。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def get(self, binding_id: str) -> AgentBinding | None:
        row = self._db.query_one(
            "SELECT * FROM agent_bindings WHERE id = ?", (binding_id,)
        )
        return _row_to_binding(row) if row is not None else None

    async def list_for_project(self, project_id: str) -> Sequence[AgentBinding]:
        return tuple(
            _row_to_binding(row)
            for row in self._db.query_all(
                "SELECT * FROM agent_bindings WHERE project_id = ? ORDER BY id",
                (project_id,),
            )
        )

    async def list_for_backend(
        self, project_id: str, backend_id: str
    ) -> Sequence[AgentBinding]:
        """AD-09：返回**序列**——同一 Project 同一 Backend 可能有多条（twin）。"""
        return tuple(
            _row_to_binding(row)
            for row in self._db.query_all(
                "SELECT * FROM agent_bindings"
                " WHERE project_id = ? AND backend_id = ? ORDER BY id",
                (project_id, backend_id),
            )
        )

    async def get_default_for_project(self, project_id: str) -> AgentBinding | None:
        row = self._db.query_one(
            "SELECT * FROM agent_bindings WHERE project_id = ? AND is_default = 1",
            (project_id,),
        )
        return _row_to_binding(row) if row is not None else None

    async def save(self, binding: AgentBinding) -> AgentBinding:
        if binding.is_default:
            existing_default = await self.get_default_for_project(binding.project_id)
            if existing_default is not None and existing_default.id != binding.id:
                raise DomainInvariantError(
                    f"这些 Project 有多个默认 Binding：{[binding.project_id]}"
                )
        self._db.run(
            """
            INSERT INTO agent_bindings (id, project_id, backend_id, display_name,
                                        native_scope_ref, enabled, is_default,
                                        default_model_id, default_provider_id,
                                        runtime_config_json, compatibility_state,
                                        origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project_id          = excluded.project_id,
                backend_id          = excluded.backend_id,
                display_name        = excluded.display_name,
                native_scope_ref    = excluded.native_scope_ref,
                enabled             = excluded.enabled,
                is_default          = excluded.is_default,
                default_model_id    = excluded.default_model_id,
                default_provider_id = excluded.default_provider_id,
                runtime_config_json = excluded.runtime_config_json,
                compatibility_state = excluded.compatibility_state,
                origin              = excluded.origin,
                updated_at          = excluded.updated_at
            """,
            (
                binding.id,
                binding.project_id,
                binding.backend_id,
                binding.display_name,
                binding.native_scope_ref,
                from_bool(binding.enabled),
                from_bool(binding.is_default),
                binding.default_model_id,
                binding.default_provider_id,
                dump_json(binding.runtime_config),
                binding.compatibility_state,
                binding.origin,
                require_iso(binding.created_at),
                require_iso(binding.updated_at),
            ),
            # AD-09 允许同 backend 多 Binding，能撞的只有「一个 Project 一个默认」。
            conflict_message=f"这些 Project 有多个默认 Binding：{[binding.project_id]}",
        )
        return binding

    async def delete(self, binding_id: str) -> None:
        """C-4：detach Binding 不删除任何原生数据。"""
        self._db.run("DELETE FROM agent_bindings WHERE id = ?", (binding_id,))


__all__ = [
    "SqliteAgentBindingRepository",
    "SqliteBackendRepository",
    "SqliteProjectRepository",
]
