"""One-time import of the legacy Project files into the domain database (host layer).

**Legacy migration only.** Before this import the Project tree lived in JSON files
next to ``server.py`` (``hierarchy.json`` …), Project instruction content in
``state/project-configs/`` and deleted Projects in ``state/project-config-archive/``.
Afterwards the database is the only source (``app.projects.tree_store``).

Older installations also kept the root rule layer in ``<native home>/constitution.md``
and per-Project layers in ``<native home>/profiles/<slug>/constitution.md``. Those
files are read once here, read-only, so existing rules are not lost; nothing under
a native Agent home is ever written, renamed or treated as a Project source. Native
Agent directories do not become Projects and no Agent binding is created: a Project
is bound to an Agent through the connection flow, not by directory layout.

Order of a run (``run_import``)
------------------------------
1. Read everything (legacy JSON files, instruction files, archives).
2. SQLite online backup next to the database (``<db>.pre-project-import-<stamp>``)
   when there is legacy material to import and the database already holds rows.
3. One transaction: tree rows (file values win for parent, display name, pinned
   order, draft/killed flags, description), persona and rule rows, block rows,
   configuration rows from Kaus-owned directories, archives and the marker
   ``app_settings[MARKER_KEY]``.
4. Move the Kaus-owned legacy files into ``state/legacy-project-files-<stamp>/``
   (rename only, never delete).

Idempotent: marker present → only the move step runs when files are still there.
Auto-import happens only when the database is the default one under the state
root (``is_paired``); a custom ``DASH_DOMAIN_DB`` gets a ``skipped:not_paired``
marker and ``scripts/import_project_files.py`` can import explicitly.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _root in (KERNEL_ROOT, REPO_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

import project_config_store  # noqa: E402  (legacy reader only)
import project_instructions  # noqa: E402

MARKER_KEY = "project_store.legacy_import.v1"
DEFAULT_DB_RELATIVE = Path("state") / "domain.sqlite3"
LEGACY_FILES = ("hierarchy.json", "labels.json", "drafts.json", "pinned.json", "killed.json",
                "skill_inherit_off.json", "config_inherit_block.json")
LEGACY_DIRS = (Path("state") / "project-configs", Path("state") / "project-config-archive")
LEGACY_DIR_PREFIX = "legacy-project-files-"
ROOT_SLUG = "default"
ROOT_DISPLAY_NAME = "X"
#: Environment variable older installations used for the native home that held the rule layers.
LEGACY_HOME_ENV = "HERMES_HOME"
LEGACY_HOME_DEFAULT = "~/.hermes"
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STAMP_RE = re.compile(r"^\d{8}T\d{12}Z$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def legacy_native_home() -> Path:
    """The native home older installations kept rule layers in (read-only in the import)."""
    return Path(os.environ.get(LEGACY_HOME_ENV) or os.path.expanduser(LEGACY_HOME_DEFAULT)).expanduser().resolve()


@dataclass
class ImportReport:
    status: str = "noop"           # imported | already_imported | nothing_to_import | skipped:not_paired
    imported: bool = False
    backup: str | None = None
    legacy_dir: str | None = None
    moved: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    personas: list[str] = field(default_factory=list)
    dropped_personas: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    capability_rows: int = 0
    skill_files: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"status={self.status}"]
        if self.imported:
            parts.append(f"projects={len(self.projects)} personas={len(self.personas)} layers={len(self.layers)}"
                         f" archives={len(self.archives)} capability_rows={self.capability_rows}")
            if self.dropped_personas:
                parts.append(f"dropped_native_personas={len(self.dropped_personas)}")
            if self.skill_files:
                parts.append(f"skill_files_not_imported={self.skill_files}")
        if self.backup:
            parts.append(f"backup={self.backup}")
        if self.moved:
            parts.append(f"moved={len(self.moved)}→{self.legacy_dir}")
        if self.warnings:
            parts.append(f"warnings={len(self.warnings)}")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# marker / pairing
# --------------------------------------------------------------------------- #

def marker(database) -> dict | None:
    row = database.query_one("SELECT value_json FROM app_settings WHERE key = ?", (MARKER_KEY,))
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _write_marker(conn, value: dict) -> None:
    conn.execute("INSERT INTO app_settings(key, value_json) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                 (MARKER_KEY, json.dumps(value, ensure_ascii=False, sort_keys=True)))


def mark_skipped(database, reason: str) -> None:
    with database.transaction() as conn:
        _write_marker(conn, {"status": f"skipped:{reason}", "at": datetime.now(timezone.utc).isoformat()})


def is_paired(db_path: Path | str, state_root: Path | str) -> bool:
    try:
        return Path(db_path).resolve() == (Path(state_root) / DEFAULT_DB_RELATIVE).resolve()
    except OSError:
        return False


def legacy_paths(state_root: Path) -> list[Path]:
    root = Path(state_root)
    found = [root / name for name in LEGACY_FILES if (root / name).exists()]
    found.extend(root / rel for rel in LEGACY_DIRS if (root / rel).exists())
    return found


# --------------------------------------------------------------------------- #
# readers (all read-only)
# --------------------------------------------------------------------------- #

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _native_dir(slug: str, home: Path | None, profiles_dir: Path | None) -> Path | None:
    if home is None:
        return None
    profiles = profiles_dir or home / "profiles"
    return home if slug == ROOT_SLUG else profiles / slug


def _count_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def _config_rows(directory: Path, slug: str) -> list[dict]:
    """Capability rows for a Kaus directory's config.yaml (classified, redacted)."""
    import capability_import
    rows = []
    try:
        own = capability_import.own_config(directory)
    except Exception:  # noqa: BLE001 - unreadable config contributes nothing
        return rows
    for key in capability_import.IMPORTED_KEYS:
        if key not in own:
            continue
        redacted, _hits = capability_import.redact_credentials(own[key], profile=slug, config_key=key)
        emitted, _notes = capability_import.capability_rows_for_key(key, redacted)
        for capability_type, capability_id, config in emitted:
            rows.append({"capability_type": capability_type, "capability_id": capability_id,
                         "assignment_mode": "local", "config": config})
    return rows


def _block_rows(config_keys: list[str]) -> list[tuple[str, str]]:
    """Capability coordinates a legacy ``config_inherit_block.json`` entry blocks."""
    import capability_import
    out = []
    for key in config_keys:
        entry = capability_import.classification_for_key(key)
        if entry is None:
            continue
        out.append((entry.capability_type, entry.capability_id))
        if entry.splits:
            out.append((entry.extras_capability_type, entry.extras_capability_id))
    return out


@dataclass
class LegacySource:
    nodes: list[str]
    parents: dict[str, str | None]
    labels: dict[str, str]
    drafts: set[str]
    pinned: list[str]
    killed: set[str]
    blocks: dict[str, list[str]]
    warnings: list[str] = field(default_factory=list)


def read_legacy_tree(state_root: Path) -> LegacySource:
    """The tree the legacy JSON files describe: nodes, parents (cycles and dangling
    parents cut to top level), display names and UI flags."""
    import capability_import
    root = Path(state_root)
    raw_hierarchy = _read_json(root / "hierarchy.json")
    hierarchy = {k: (v if isinstance(v, str) and v else None) for k, v in raw_hierarchy.items()
                 if isinstance(k, str)} if isinstance(raw_hierarchy, dict) else {}
    raw_labels = _read_json(root / "labels.json")
    labels = {k: v for k, v in raw_labels.items() if isinstance(k, str) and isinstance(v, str) and v} \
        if isinstance(raw_labels, dict) else {}
    drafts = set(_str_list(_read_json(root / "drafts.json")))
    pinned = _str_list(_read_json(root / "pinned.json"))
    killed = set(_str_list(_read_json(root / "killed.json")))
    blocks = capability_import.load_config_blocks(root)
    warnings: list[str] = []

    candidates = {ROOT_SLUG} | set(hierarchy) | {p for p in hierarchy.values() if p} | drafts
    candidates |= set(project_config_store.names(root))
    nodes = sorted(n for n in candidates if n == ROOT_SLUG or _PROJECT_ID_RE.match(n))
    for name in sorted(candidates - set(nodes)):
        warnings.append(f"`{name}` is not a valid Project id; skipped.")
    node_set = set(nodes)

    parents: dict[str, str | None] = {}
    for name in nodes:
        if name == ROOT_SLUG or name in drafts:
            parents[name] = None
            continue
        parent = hierarchy.get(name, ROOT_SLUG) if name in hierarchy else ROOT_SLUG
        if parent == name:
            warnings.append(f"`{name}` lists itself as parent; placed at top level.")
            parent = None
        elif parent is not None and parent not in node_set:
            warnings.append(f"`{name}` names the missing parent `{parent}`; placed at top level.")
            parent = None
        parents[name] = parent
    # Break cycles: the alphabetically first member of a cycle becomes top level.
    color: dict[str, int] = {}
    for start in nodes:
        if color.get(start, 0):
            continue
        path: list[str] = []
        cur: str | None = start
        while cur is not None and not color.get(cur, 0):
            color[cur] = 1
            path.append(cur)
            cur = parents[cur]
        if cur is not None and color.get(cur) == 1:
            cycle = path[path.index(cur):]
            victim = min(cycle)
            parents[victim] = None
            warnings.append("hierarchy.json contains a cycle: " + " → ".join([*cycle, cycle[0]])
                            + f"; `{victim}` placed at top level.")
        for item in path:
            color[item] = 2
    return LegacySource(nodes=nodes, parents=parents, labels=labels, drafts=drafts & node_set,
                        pinned=[p for p in pinned if p in node_set], killed=killed & node_set,
                        blocks={k: v for k, v in blocks.items() if k in node_set}, warnings=warnings)


def _archives(state_root: Path) -> list[dict]:
    base = project_config_store.archive_root(state_root)
    out = []
    if not base.is_dir():
        return out
    for path in sorted(base.iterdir()):
        if not path.is_dir() or not (path / project_config_store.MARKER).is_file():
            continue
        slug, _, stamp = path.name.rpartition("-")
        if not _PROJECT_ID_RE.match(slug) or not _STAMP_RE.match(stamp):
            continue
        try:
            archived_at = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            archived_at = datetime.now(timezone.utc).isoformat()
        record = project_config_store.deleted_record(path)
        try:
            description = json.loads((path / project_config_store.MARKER).read_text(encoding="utf-8")).get("description")
        except (OSError, ValueError, AttributeError):
            description = None
        rows = []
        persona = project_instructions.persona_from_file_text(_read_text(path / "SOUL.md"))
        if persona.strip():
            rows.append({"capability_type": project_instructions.INSTRUCTIONS_TYPE,
                         "capability_id": project_instructions.PERSONA_ID, "assignment_mode": "local",
                         "config": {"body": persona}})
        layer_rows, layout = project_instructions.text_to_rows(slug, _read_text(path / "constitution.md"))
        rows.extend({"capability_type": project_instructions.INSTRUCTIONS_TYPE, **row} for row in layer_rows)
        rows.extend(_config_rows(path, slug))
        snapshot: dict[str, Any] = {"clearedRows": rows, "source": str(path)}
        if description:
            snapshot["description"] = str(description)
        if layout:
            snapshot["metadata"] = {project_instructions.INSTRUCTION_BLOCKS_KEY: layout}
        out.append({"slug": slug, "record": record, "snapshot": snapshot, "archived_at": archived_at,
                    "path": path})
    return out


# --------------------------------------------------------------------------- #
# the import
# --------------------------------------------------------------------------- #

def _backup(database, stamp: str) -> str | None:
    path = Path(getattr(database, "path", ":memory:"))
    if path.name == ":memory:" or not path.exists():
        return None
    dest = path.with_name(f"{path.name}.pre-project-import-{stamp}")
    target = sqlite3.connect(str(dest))
    try:
        with database._lock:  # noqa: SLF001 - the online backup needs the live connection quiescent
            database.connection.backup(target)
    finally:
        target.close()
    return str(dest)


def move_legacy_files(state_root: Path, stamp: str | None = None) -> tuple[str | None, list[str]]:
    """Rename the Kaus-owned legacy files into ``state/legacy-project-files-<stamp>/``."""
    root = Path(state_root)
    present = legacy_paths(root)
    if not present:
        return None, []
    legacy = root / "state" / f"{LEGACY_DIR_PREFIX}{stamp or _stamp()}"
    legacy.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in present:
        target = legacy / path.name
        if target.exists():
            target = legacy / f"{path.name}.{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
        path.rename(target)
        moved.append(str(path))
    return str(legacy), moved


def run_import(*, database, state_root: Path, hermes_home: Path | None, profiles_dir: Path | None = None,
               log: Callable[[str], None] | None = None, move_files: bool = True) -> ImportReport:
    """Import the legacy files once. ``hermes_home`` is the native home whose
    ``constitution.md`` files are read (read-only); ``None`` reads none."""
    from app.projects.tree_store import ProjectTreeStore

    state_root = Path(state_root)
    home = Path(hermes_home) if hermes_home is not None else None
    profiles = Path(profiles_dir) if profiles_dir is not None else (home / "profiles" if home else None)
    report = ImportReport()
    existing = marker(database)
    if existing is not None and existing.get("status") == "imported":
        report.status = "already_imported"
        if move_files:
            report.legacy_dir, report.moved = move_legacy_files(state_root)
            if report.moved:
                with database.transaction() as conn:
                    _write_marker(conn, {**existing, "legacyDir": report.legacy_dir,
                                         "moved": [*existing.get("moved", []), *report.moved]})
        return report

    stamp = _stamp()
    legacy = legacy_paths(state_root)
    source = read_legacy_tree(state_root)
    report.warnings.extend(source.warnings)
    archives = _archives(state_root)
    has_rows = bool(database.query_one("SELECT 1 FROM projects LIMIT 1"))
    if legacy and has_rows:
        report.backup = _backup(database, stamp)

    store = ProjectTreeStore(database, root_slug=ROOT_SLUG)
    with database.transaction() as conn:
        for slug in source.nodes:
            node = store.tx_node(conn, slug)
            if node is not None and node["status"] == "disabled" and slug != ROOT_SLUG:
                # Deleted in the database already: the file tree does not bring it back.
                continue
            report.projects.append(slug)
            kaus_dir = project_config_store.directory(state_root, slug) if project_config_store.contains(state_root, slug) else None
            native = _native_dir(slug, home, profiles)
            native_exists = native is not None and native.is_dir()
            if kaus_dir is not None and native_exists:
                report.warnings.append(f"`{slug}` has both a Kaus configuration directory and a native directory;"
                                       " the native directory stays untouched and is not a Project source.")
            description = None
            if kaus_dir is not None:
                try:
                    description = json.loads((kaus_dir / project_config_store.MARKER).read_text(encoding="utf-8")).get("description")
                except (OSError, ValueError, AttributeError):
                    description = None
                report.skill_files += _count_files(kaus_dir / "skills")
            rank = source.pinned.index(slug) if slug in source.pinned else None
            display = source.labels.get(slug) or (node["display_name"] if node else None) or (
                ROOT_DISPLAY_NAME if slug == ROOT_SLUG else slug)
            store.tx_write_node(conn, slug, parent=source.parents.get(slug), display_name=display,
                                draft=slug in source.drafts, pinned=slug in source.pinned,
                                killed=slug in source.killed, pinned_rank=rank,
                                description=str(description) if description else None)
            present = {(r["capability_type"], r["capability_id"]): r for r in store.tx_rows(conn, slug)}
            persona_key = (project_instructions.INSTRUCTIONS_TYPE, project_instructions.PERSONA_ID)
            if kaus_dir is not None:
                persona = project_instructions.persona_from_file_text(_read_text(kaus_dir / "SOUL.md"))
                if persona.strip() and persona_key not in present:
                    store.tx_put_row(conn, slug, *persona_key, "local", {"body": persona})
                    report.personas.append(slug)
            elif native_exists and persona_key in present and present[persona_key]["assignment_mode"] != "block":
                # A native SOUL.md is the Agent's own identity, not a Project persona (D2).
                store.tx_delete_row(conn, slug, *persona_key)
                report.dropped_personas.append(slug)
            layer_path = (kaus_dir / "constitution.md") if kaus_dir is not None else (
                (native / "constitution.md") if native_exists else None)
            has_layer = any(project_instructions.is_layer_row(slug, cid) for (ctype, cid) in present
                            if ctype == project_instructions.INSTRUCTIONS_TYPE)
            if layer_path is not None and not has_layer:
                rows, layout = project_instructions.text_to_rows(slug, _read_text(layer_path))
                if rows:
                    for row in rows:
                        store.tx_put_row(conn, slug, project_instructions.INSTRUCTIONS_TYPE, row["capability_id"],
                                         row["assignment_mode"], row["config"])
                    if layout:
                        store.tx_update_metadata(conn, slug, {project_instructions.INSTRUCTION_BLOCKS_KEY: layout})
                    report.layers.append(slug)
            present = {(r["capability_type"], r["capability_id"]) for r in store.tx_rows(conn, slug)}
            rows = _config_rows(kaus_dir, slug) if kaus_dir is not None else []
            for row in rows:
                key = (row["capability_type"], row["capability_id"])
                if key in present:
                    continue
                store.tx_put_row(conn, slug, key[0], key[1], row["assignment_mode"], row["config"])
                present.add(key)
                report.capability_rows += 1
            for key in _block_rows(source.blocks.get(slug, [])):
                if key in present:   # a local value at the same coordinates wins over the block
                    continue
                store.tx_put_row(conn, slug, key[0], key[1], "block", {})
                present.add(key)
                report.capability_rows += 1
        for item in archives:
            slug = item["slug"]
            if store.tx_node(conn, slug) is None:
                label = (item["record"] or {}).get("label")
                store.tx_write_node(conn, slug, display_name=label or slug, keep_metadata=False)
                store.tx_disable(conn, slug)
            store.tx_insert_archive(conn, slug, item["record"], item["snapshot"], archived_at=item["archived_at"])
            report.archives.append(item["path"].name)
        store.tx_bump(conn)
        _write_marker(conn, {"status": "imported", "at": datetime.now(timezone.utc).isoformat(), "backup": report.backup,
                             "counts": {"projects": len(report.projects), "personas": len(report.personas),
                                        "layers": len(report.layers), "archives": len(report.archives),
                                        "capabilityRows": report.capability_rows, "skillFiles": report.skill_files},
                             "warnings": report.warnings})
    report.imported = True
    report.status = "imported" if legacy else "nothing_to_import"
    if move_files:
        report.legacy_dir, report.moved = move_legacy_files(state_root, stamp)
        if report.moved:
            with database.transaction() as conn:
                current = marker(database) or {}
                _write_marker(conn, {**current, "legacyDir": report.legacy_dir, "moved": report.moved})
    if log is not None:
        log(f"[project_store] {report.summary()}")
    return report


def auto_import(*, database, db_path: Path | str, state_root: Path, hermes_home: Path | None,
                profiles_dir: Path | None = None, log: Callable[[str], None] | None = None) -> ImportReport:
    """Startup entry: import only when the database is the default one under ``state_root``."""
    if is_paired(db_path, state_root):
        return run_import(database=database, state_root=state_root, hermes_home=hermes_home,
                          profiles_dir=profiles_dir, log=log)
    report = ImportReport(status="skipped:not_paired")
    current = marker(database)
    if current is None or current.get("status") != "imported":
        if current is None or current.get("status") != "skipped:not_paired":
            mark_skipped(database, "not_paired")
        if log is not None:
            log(f"[project_store] 数据库不是 {Path(state_root) / DEFAULT_DB_RELATIVE} 下的默认库，跳过旧文件导入；"
                f"需要时运行 scripts/import_project_files.py --db {db_path} --state-root {state_root}")
    else:
        report.status = "already_imported"
    return report


__all__ = ["ImportReport", "LEGACY_DIRS", "LEGACY_FILES", "LegacySource", "MARKER_KEY", "auto_import", "is_paired",
           "legacy_native_home", "legacy_paths", "mark_skipped", "marker", "move_legacy_files", "read_legacy_tree",
           "run_import"]
