"""Project tree, instruction rows and archives on the domain database.

The tree (parent, display name, draft/pinned/killed flags, description) lives in
``projects``; instruction content in ``project_capabilities``; deleted Projects
in ``project_archives``. Files are never a source: a native Agent that needs a
file gets it generated from these rows by its driver or the host.

Every public write is one transaction and bumps ``app_settings[REVISION_KEY]``
so readers (SSE, background ticks) can detect changes without scanning. The
``tx_*`` helpers take an open connection so an importer can batch many writes
into a single transaction; they do not bump the revision themselves.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

REVISION_KEY = "project_tree.revision.v1"
ARCHIVED_FLAG = "configurationArchived"
INSTRUCTIONS_TYPE = "instructions"
PERSONA_ID = "project-persona"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(text) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_id(slug: str) -> str:
    return f"project:{slug}"


def _slug(identifier: str | None) -> str | None:
    if not identifier:
        return None
    return identifier[len("project:"):] if identifier.startswith("project:") else identifier


class ProjectTreeStore:
    def __init__(self, database, *, root_slug: str = "default"):
        self.db = database
        self.root_slug = root_slug

    # ------------------------------------------------------------------ #
    # low-level helpers (inside a caller-owned transaction)
    # ------------------------------------------------------------------ #
    @staticmethod
    def tx_bump(conn) -> int:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (REVISION_KEY,)).fetchone()
        current = _load(row[0]).get("revision", 0) if row else 0
        revision = int(current) + 1
        conn.execute("INSERT INTO app_settings(key, value_json) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                     (REVISION_KEY, _dump({"revision": revision})))
        return revision

    @staticmethod
    def tx_node(conn, slug: str) -> dict | None:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return ProjectTreeStore._as_node(row) if row else None

    @staticmethod
    def _as_node(row) -> dict:
        metadata = _load(row["metadata_json"])
        ui = metadata.get("ui_state") if isinstance(metadata.get("ui_state"), dict) else {}
        return {
            "slug": row["slug"], "id": row["id"], "display_name": row["display_name"],
            "parent": _slug(row["parent_project_id"]), "status": row["status"],
            "draft": row["status"] == "draft" or bool(ui.get("draft")),
            "pinned": bool(ui.get("pinned")), "pinned_rank": ui.get("pinned_rank"),
            "killed": bool(ui.get("killed")),
            "description": str(metadata.get("description") or ""),
            "archived": bool(metadata.get(ARCHIVED_FLAG)),
            "metadata": metadata,
        }

    @staticmethod
    def tx_write_node(conn, slug: str, *, parent: str | None = None, display_name: str | None = None,
                      draft: bool = False, pinned: bool = False, killed: bool = False,
                      description: str | None = None, pinned_rank: int | None = None,
                      status: str | None = None, keep_metadata: bool = True) -> dict:
        """Insert or update a tree row. ``None`` display keeps the current one (or the slug)."""
        now = _now()
        existing = ProjectTreeStore.tx_node(conn, slug)
        metadata = dict(existing["metadata"]) if existing and keep_metadata else {}
        ui = dict(metadata.get("ui_state") or {})
        ui.update({"draft": bool(draft), "pinned": bool(pinned), "killed": bool(killed)})
        if pinned:
            if pinned_rank is not None:
                ui["pinned_rank"] = pinned_rank
            elif "pinned_rank" not in ui:
                ui["pinned_rank"] = ProjectTreeStore._next_rank(conn)
        else:
            ui.pop("pinned_rank", None)
        metadata["ui_state"] = ui
        if description is not None:
            metadata["description"] = description
        metadata.pop(ARCHIVED_FLAG, None)
        resolved_status = status or ("draft" if draft else "active")
        display = display_name or (existing["display_name"] if existing else slug)
        conn.execute(
            "INSERT INTO projects (id, slug, display_name, parent_project_id, workspace_root, status,"
            " metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name,"
            " parent_project_id = excluded.parent_project_id, status = excluded.status,"
            " metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
            (project_id(slug), slug, display, project_id(parent) if parent else None, resolved_status,
             _dump(metadata), now, now))
        return ProjectTreeStore.tx_node(conn, slug)

    @staticmethod
    def _next_rank(conn) -> int:
        ranks = [ProjectTreeStore._as_node(row).get("pinned_rank") for row in conn.execute("SELECT * FROM projects")]
        numeric = [int(r) for r in ranks if isinstance(r, (int, float))]
        return (max(numeric) + 1) if numeric else 0

    @staticmethod
    def tx_update_metadata(conn, slug: str, changes: Mapping[str, Any]) -> None:
        node = ProjectTreeStore.tx_node(conn, slug)
        if node is None:
            raise KeyError(slug)
        metadata = dict(node["metadata"])
        for key, value in changes.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        conn.execute("UPDATE projects SET metadata_json = ?, updated_at = ? WHERE slug = ?",
                     (_dump(metadata), _now(), slug))

    @staticmethod
    def tx_put_row(conn, slug: str, capability_type: str, capability_id: str, mode: str = "local",
                   config: Mapping[str, Any] | None = None) -> None:
        if mode == "block" and config:
            raise ValueError("block rows carry no config")
        now = _now()
        conn.execute(
            "INSERT INTO project_capabilities (id, project_id, capability_type, capability_id, assignment_mode,"
            " config_json, version, source_project_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)"
            " ON CONFLICT(project_id, capability_type, capability_id) DO UPDATE SET"
            " assignment_mode = excluded.assignment_mode, config_json = excluded.config_json,"
            " updated_at = excluded.updated_at",
            (f"capability:{uuid.uuid4()}", project_id(slug), capability_type, capability_id, mode,
             _dump(dict(config or {})), now, now))

    @staticmethod
    def tx_delete_row(conn, slug: str, capability_type: str, capability_id: str) -> None:
        conn.execute("DELETE FROM project_capabilities WHERE project_id = ? AND capability_type = ? AND capability_id = ?",
                     (project_id(slug), capability_type, capability_id))

    @staticmethod
    def tx_rows(conn, slug: str, capability_type: str | None = None) -> list[dict]:
        sql = "SELECT capability_type, capability_id, assignment_mode, config_json FROM project_capabilities WHERE project_id = ?"
        params: list = [project_id(slug)]
        if capability_type is not None:
            sql += " AND capability_type = ?"
            params.append(capability_type)
        return [{"capability_type": r["capability_type"], "capability_id": r["capability_id"],
                 "assignment_mode": r["assignment_mode"], "config": _load(r["config_json"])}
                for r in conn.execute(sql + " ORDER BY capability_type, capability_id", params)]

    @staticmethod
    def tx_insert_archive(conn, slug: str, record: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None,
                          archived_at: str | None = None) -> str:
        archive_id = f"archive:{uuid.uuid4()}"
        conn.execute(
            "INSERT INTO project_archives (id, project_id, batch_id, batch_root, archived_at, record_json,"
            " snapshot_json, restored_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (archive_id, project_id(slug), record.get("batchId"), record.get("batchRoot"),
             archived_at or _now(), _dump(dict(record)), _dump(dict(snapshot or {}))))
        return archive_id

    @staticmethod
    def tx_disable(conn, slug: str) -> None:
        node = ProjectTreeStore.tx_node(conn, slug)
        if node is None:
            return
        metadata = {**node["metadata"], ARCHIVED_FLAG: True}
        conn.execute("UPDATE projects SET status = 'disabled', metadata_json = ?, updated_at = ? WHERE slug = ?",
                     (_dump(metadata), _now(), slug))

    @staticmethod
    def tx_add_binding(conn, slug: str, backend_key: str, scope_ref: str) -> bool:
        binding_id = f"binding:{slug}:{backend_key}"
        if conn.execute("SELECT 1 FROM agent_bindings WHERE id = ?", (binding_id,)).fetchone():
            return False
        has_default = conn.execute("SELECT 1 FROM agent_bindings WHERE project_id = ? AND is_default = 1",
                                   (project_id(slug),)).fetchone()
        now = _now()
        conn.execute(
            "INSERT INTO agent_bindings (id, project_id, backend_id, display_name, native_scope_ref, enabled,"
            " is_default, default_model_id, default_provider_id, runtime_config_json, compatibility_state,"
            " origin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, NULL, NULL, '{}', 'unknown',"
            " 'imported', ?, ?)",
            (binding_id, project_id(slug), f"backend:{backend_key}", scope_ref, scope_ref,
             0 if has_default else 1, now, now))
        return True

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def revision(self) -> int:
        row = self.db.query_one("SELECT value_json FROM app_settings WHERE key = ?", (REVISION_KEY,))
        return int(_load(row["value_json"]).get("revision", 0)) if row else 0

    def nodes(self, *, include_disabled: bool = False) -> dict[str, dict]:
        rows = self.db.query_all("SELECT * FROM projects ORDER BY slug")
        out = {}
        for row in rows:
            node = self._as_node(row)
            if node["status"] == "disabled" and not include_disabled:
                continue
            out[node["slug"]] = node
        return out

    def node(self, slug: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM projects WHERE slug = ?", (slug,))
        return self._as_node(row) if row else None

    def exists(self, slug: str) -> bool:
        node = self.node(slug)
        return node is not None and node["status"] != "disabled"

    def pinned(self) -> list[str]:
        pinned = [n for n in self.nodes().values() if n["pinned"]]
        pinned.sort(key=lambda n: (n["pinned_rank"] if isinstance(n["pinned_rank"], (int, float)) else 1 << 30, n["slug"]))
        return [n["slug"] for n in pinned]

    def rows(self, slug: str, capability_type: str | None = None) -> list[dict]:
        with self.db.transaction() as conn:
            return self.tx_rows(conn, slug, capability_type)

    def block_rows(self) -> list[tuple[str, str, str]]:
        """``(slug, capability_type, capability_id)`` of every block row on a live Project."""
        rows = self.db.query_all(
            "SELECT p.slug AS slug, c.capability_type AS capability_type, c.capability_id AS capability_id"
            " FROM project_capabilities c JOIN projects p ON p.id = c.project_id"
            " WHERE c.assignment_mode = 'block' AND p.status <> 'disabled'"
            " ORDER BY p.slug, c.capability_type, c.capability_id")
        return [(r["slug"], r["capability_type"], r["capability_id"]) for r in rows]

    def latest_archive(self, slug: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM project_archives WHERE project_id = ? AND restored_at IS NULL"
            " ORDER BY archived_at DESC, rowid DESC LIMIT 1", (project_id(slug),))
        if row is None:
            return None
        return {"id": row["id"], "slug": slug, "record": _load(row["record_json"]),
                "snapshot": _load(row["snapshot_json"]), "archived_at": row["archived_at"],
                "batch_id": row["batch_id"], "batch_root": row["batch_root"]}

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def ensure_root(self, display_name: str) -> bool:
        with self.db.transaction() as conn:
            if self.tx_node(conn, self.root_slug) is not None:
                return False
            self.tx_write_node(conn, self.root_slug, display_name=display_name)
            self.tx_bump(conn)
            return True

    def create(self, slug: str, *, display_name: str | None = None, description: str = "",
               draft: bool = True, parent: str | None = None) -> dict:
        """New tree row. A disabled (deleted) row with this slug is reused: its remaining
        capability rows are snapshotted into its latest archive, then cleared."""
        with self.db.transaction() as conn:
            existing = self.tx_node(conn, slug)
            if existing is not None and existing["status"] != "disabled":
                raise FileExistsError(slug)
            if existing is not None:
                rows = self.tx_rows(conn, slug)
                latest = conn.execute(
                    "SELECT id, snapshot_json FROM project_archives WHERE project_id = ? AND restored_at IS NULL"
                    " ORDER BY archived_at DESC, rowid DESC LIMIT 1", (project_id(slug),)).fetchone()
                if latest is not None and rows:
                    snapshot = _load(latest["snapshot_json"])
                    snapshot["clearedRows"] = rows
                    conn.execute("UPDATE project_archives SET snapshot_json = ? WHERE id = ?", (_dump(snapshot), latest["id"]))
                conn.execute("DELETE FROM project_capabilities WHERE project_id = ?", (project_id(slug),))
            node = self.tx_write_node(conn, slug, parent=parent, display_name=display_name or slug,
                                      draft=draft, description=description, keep_metadata=False)
            self.tx_bump(conn)
            return node

    def ensure_node(self, slug: str, *, parent: str | None = None, display_name: str | None = None,
                    binding: tuple[str, str] | None = None) -> bool:
        """Create a live row (and optionally a default binding) when the slug has none."""
        with self.db.transaction() as conn:
            existing = self.tx_node(conn, slug)
            created = False
            if existing is None or existing["status"] == "disabled":
                self.tx_write_node(conn, slug, parent=parent, display_name=display_name or slug, keep_metadata=False)
                created = True
            if binding is not None:
                created = self.tx_add_binding(conn, slug, *binding) or created
            if created:
                self.tx_bump(conn)
            return created

    def set_parent(self, slug: str, parent: str | None) -> dict:
        with self.db.transaction() as conn:
            node = self._require(conn, slug)
            result = self.tx_write_node(conn, slug, parent=parent, draft=False, pinned=node["pinned"],
                                        killed=node["killed"], pinned_rank=node["pinned_rank"])
            self.tx_bump(conn)
            return result

    def set_display(self, slug: str, display_name: str | None) -> dict:
        with self.db.transaction() as conn:
            self._require(conn, slug)
            conn.execute("UPDATE projects SET display_name = ?, updated_at = ? WHERE slug = ?",
                         ((display_name or "").strip() or slug, _now(), slug))
            self.tx_bump(conn)
            return self.tx_node(conn, slug)

    def set_description(self, slug: str, description: str) -> None:
        with self.db.transaction() as conn:
            self.tx_update_metadata(conn, slug, {"description": description or None})
            self.tx_bump(conn)

    def set_flag(self, slug: str, flag: str, value: bool) -> dict:
        if flag not in ("pinned", "killed", "draft"):
            raise ValueError(flag)
        with self.db.transaction() as conn:
            node = self._require(conn, slug)
            flags = {"draft": node["draft"], "pinned": node["pinned"], "killed": node["killed"], flag: bool(value)}
            result = self.tx_write_node(conn, slug, parent=node["parent"], pinned_rank=node["pinned_rank"], **flags)
            self.tx_bump(conn)
            return result

    def update_metadata(self, slug: str, changes: Mapping[str, Any]) -> None:
        with self.db.transaction() as conn:
            self.tx_update_metadata(conn, slug, changes)
            self.tx_bump(conn)

    def put_row(self, slug: str, capability_type: str, capability_id: str, mode: str = "local",
                config: Mapping[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            self._require(conn, slug)
            self.tx_put_row(conn, slug, capability_type, capability_id, mode, config)
            self.tx_bump(conn)

    def delete_row(self, slug: str, capability_type: str, capability_id: str) -> None:
        with self.db.transaction() as conn:
            self.tx_delete_row(conn, slug, capability_type, capability_id)
            self.tx_bump(conn)

    def replace_rows(self, slug: str, capability_type: str, rows: Iterable[Mapping[str, Any]],
                     select: Callable[[str], bool], metadata: Mapping[str, Any] | None = None) -> None:
        """Replace the rows of one type whose capability_id passes ``select``; optionally
        update metadata keys in the same transaction."""
        with self.db.transaction() as conn:
            self._require(conn, slug)
            for row in self.tx_rows(conn, slug, capability_type):
                if select(row["capability_id"]):
                    self.tx_delete_row(conn, slug, capability_type, row["capability_id"])
            for row in rows:
                self.tx_put_row(conn, slug, capability_type, row["capability_id"],
                                row.get("assignment_mode", "local"), row.get("config"))
            if metadata:
                self.tx_update_metadata(conn, slug, metadata)
            self.tx_bump(conn)

    def copy_rows(self, source: str, target: str, select: Callable[[dict], bool]) -> int:
        with self.db.transaction() as conn:
            self._require(conn, target)
            copied = 0
            for row in self.tx_rows(conn, source):
                if select(row):
                    self.tx_put_row(conn, target, row["capability_type"], row["capability_id"],
                                    row["assignment_mode"], row["config"])
                    copied += 1
            if copied:
                self.tx_bump(conn)
            return copied

    def archive(self, slug: str, record: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None) -> str:
        """Mark a Project deleted and keep its record. Capability rows stay in place."""
        with self.db.transaction() as conn:
            if self.tx_node(conn, slug) is None:
                self.tx_write_node(conn, slug, keep_metadata=False)
            archive_id = self.tx_insert_archive(conn, slug, record, snapshot)
            self.tx_disable(conn, slug)
            self.tx_bump(conn)
            return archive_id

    def disable(self, slug: str) -> bool:
        with self.db.transaction() as conn:
            node = self.tx_node(conn, slug)
            if node is None or node["status"] == "disabled":
                return False
            self.tx_disable(conn, slug)
            self.tx_bump(conn)
            return True

    def restore(self, slug: str, archive_id: str, *, parent: str | None, draft: bool = False,
                display_name: str | None = None, pinned: bool = False, killed: bool = False) -> dict:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM project_archives WHERE id = ? AND project_id = ?",
                               (archive_id, project_id(slug))).fetchone()
            if row is None:
                raise KeyError(archive_id)
            existing = self.tx_node(conn, slug)
            if existing is not None and existing["status"] != "disabled":
                raise FileExistsError(slug)
            snapshot = _load(row["snapshot_json"])
            node = self.tx_write_node(conn, slug, parent=parent, display_name=display_name or (existing or {}).get("display_name"),
                                      draft=draft, pinned=pinned, killed=killed,
                                      description=snapshot.get("description"))
            present = {(r["capability_type"], r["capability_id"]) for r in self.tx_rows(conn, slug)}
            for cleared in snapshot.get("clearedRows") or []:
                key = (cleared.get("capability_type"), cleared.get("capability_id"))
                if key[0] and key[1] and key not in present:
                    self.tx_put_row(conn, slug, key[0], key[1], cleared.get("assignment_mode", "local"), cleared.get("config"))
            conn.execute("UPDATE project_archives SET restored_at = ? WHERE id = ?", (_now(), archive_id))
            self.tx_bump(conn)
            return node

    @staticmethod
    def _require(conn, slug: str) -> dict:
        node = ProjectTreeStore.tx_node(conn, slug)
        if node is None or node["status"] == "disabled":
            raise KeyError(slug)
        return node


__all__ = ["ARCHIVED_FLAG", "INSTRUCTIONS_TYPE", "PERSONA_ID", "REVISION_KEY", "ProjectTreeStore", "project_id"]
