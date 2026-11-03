"""Legacy reader for the pre-2026-11 Kaus project configuration directories.

Only ``project_file_import`` uses it, once, to move that material into the domain
database. Nothing here writes."""
from __future__ import annotations

import json
import re
from pathlib import Path

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MARKER = ".kaus-project.json"


def root(state_root: Path) -> Path:
    return state_root / "state" / "project-configs"


def directory(state_root: Path, name: str) -> Path:
    if not _ID.fullmatch(name):
        raise ValueError("项目标识无效")
    return root(state_root) / name


def contains(state_root: Path, name: str) -> bool:
    return bool(_ID.fullmatch(name) and (root(state_root) / name / MARKER).is_file())


def names(state_root: Path) -> list[str]:
    base = root(state_root)
    return sorted(p.name for p in base.iterdir() if contains(state_root, p.name)) if base.is_dir() else []


DELETED_RECORD = ".kaus-deleted.json"
_STAMP = re.compile(r"^\d{8}T\d{12}Z$")


def archive_root(state_root: Path) -> Path:
    return state_root / "state" / "project-config-archive"


def latest_archive(state_root: Path, name: str) -> Path | None:
    """Newest archived copy of ``name`` (``<name>-<stamp>``), or ``None``."""
    directory(state_root, name)
    base = archive_root(state_root)
    if not base.is_dir():
        return None
    prefix = f"{name}-"
    copies = [p for p in base.iterdir() if p.is_dir() and p.name.startswith(prefix)
              and _STAMP.fullmatch(p.name[len(prefix):]) and (p / MARKER).is_file()]
    return max(copies, key=lambda p: p.name) if copies else None


def deleted_record(archived: Path) -> dict:
    try:
        value = json.loads((archived / DELETED_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
