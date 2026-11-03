"""Mirror managed-copy skill usage back into the source profile's usage sidecar.

Hermes records skill telemetry in ``<HERMES_HOME>/skills/.usage.json``
(``tools/skill_usage.py``, keyed by the skill's frontmatter name). A Kaus
execution home carries private copies of the selected skills, so the source
profile never observes their use, and the native curator may archive an
agent-created source skill that a Kaus project keeps using.

This is the one place Kaus writes into a user's native Hermes directory. The
write is limited to ``<source>/skills/.usage.json``:

* only records that already exist in the source file are touched; nothing is
  created, removed or renamed, and no skill file is read or written;
* ``last_used_at`` / ``last_viewed_at`` are raised to the newer value;
* ``use_count`` / ``view_count`` grow by the delta not yet reported, tracked in
  a Kaus-owned ledger inside the execution home, so repeated merges are
  idempotent;
* the read-modify-write cycle holds the same ``flock`` on
  ``.usage.json.lock`` that Hermes uses, and the write is temp + ``os.replace``
  with Hermes's serialisation (``indent=2, sort_keys=True, ensure_ascii=False``).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

MANIFEST = ".kaus-skills.json"
LEDGER = ".kaus-usage-merged.json"
USAGE_FILE = ".usage.json"
LOCK_FILE = ".usage.json.lock"
TIMESTAMP_FIELDS = ("last_used_at", "last_viewed_at")
COUNT_FIELDS = ("use_count", "view_count")
_FRONTMATTER_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.M)


@dataclass(frozen=True)
class UsageMergeReport:
    merged: tuple[str, ...] = ()
    written: bool = False


def _load_records(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _frontmatter_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[:4000]
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    header = text[3:].split("\n---", 1)[0]
    match = _FRONTMATTER_NAME.search(header)
    return match.group(1).strip().strip("\"'") or None if match else None


def selected_names(scope_home: Path) -> set[str]:
    """Hermes usage keys of the skills Kaus copied into this execution home."""
    manifest_path = scope_home / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return set()
    try:
        entries = json.loads(manifest_path.read_text()).get("entries", {})
    except (OSError, ValueError, AttributeError):
        return set()
    names: set[str] = set()
    for entry in entries.values() if isinstance(entries, dict) else ():
        if not isinstance(entry, dict):
            continue
        if entry.get("name"):
            names.add(str(entry["name"]))
        copied = scope_home / "skills" / str(entry.get("directory") or "") / "SKILL.md"
        if entry.get("directory") and not copied.is_symlink() and copied.is_file():
            visible = _frontmatter_name(copied)
            if visible:
                names.add(visible)
    return names


@contextlib.contextmanager
def _locked(lock_path: Path):
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write_json(path: Path, payload, *, prefix: str) -> None:
    mode = None
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def merge_usage(scope_home: Path, source_home: Path) -> UsageMergeReport:
    """Raise source usage records to what the managed copies observed.

    Returns which skills were merged. Safe to call repeatedly and concurrently
    with Hermes processes; a missing scope, manifest or source file is a no-op.
    """
    scope_home, source_home = Path(scope_home), Path(source_home)
    scope_skills = scope_home / "skills"
    if (scope_home.resolve() == source_home.resolve() or scope_home.is_symlink()
            or not (scope_home / ".kaus-scope.json").is_file()
            or scope_skills.is_symlink() or not scope_skills.is_dir()):
        # A native-mode home links the source skills directory: Hermes already
        # records usage there and nothing needs mirroring.
        return UsageMergeReport()
    observed_path = scope_skills / USAGE_FILE
    if observed_path.is_symlink() or not observed_path.is_file():
        return UsageMergeReport()
    names = selected_names(scope_home)
    observed = {name: record for name, record in _load_records(observed_path).items() if name in names}
    if not observed:
        return UsageMergeReport()
    source_skills = source_home / "skills"
    target = source_skills / USAGE_FILE
    if source_skills.is_symlink() or not source_skills.is_dir() or target.is_symlink() or not target.is_file():
        return UsageMergeReport()
    ledger_path = scope_home / LEDGER
    if ledger_path.is_symlink():
        return UsageMergeReport()
    merged: list[str] = []
    written = False
    # The ledger is read and rewritten under the same lock as the source file,
    # so two Kaus merges of one execution home cannot both report a delta.
    with _locked(source_skills / LOCK_FILE):
        ledger = _load_records(ledger_path)
        records = _load_records(target)
        dirty = False
        for name, seen in observed.items():
            record = records.get(name)
            if not isinstance(record, dict):
                continue
            merged.append(name)
            baseline = ledger.get(name, {})
            for field in TIMESTAMP_FIELDS:
                newer = _parse(seen.get(field))
                if newer is None:
                    continue
                current = _parse(record.get(field))
                if current is None or newer > current:
                    record[field] = str(seen[field])
                    dirty = True
            for field in COUNT_FIELDS:
                delta = _count(seen.get(field)) - _count(baseline.get(field))
                if delta > 0:
                    record[field] = _count(record.get(field)) + delta
                    dirty = True
        if dirty:
            _write_json(target, records, prefix=".usage_")
            written = True
        if merged:
            for name in merged:
                ledger[name] = {field: _count(observed[name].get(field)) for field in COUNT_FIELDS}
            _write_json(ledger_path, ledger, prefix=".kaus-usage-")
    return UsageMergeReport(merged=tuple(merged), written=written)


__all__ = ["LEDGER", "UsageMergeReport", "merge_usage", "selected_names"]
