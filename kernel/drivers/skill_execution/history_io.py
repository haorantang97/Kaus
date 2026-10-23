"""Bounded, read-only native transcript primitives used at adapter boundaries."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import stat
import uuid

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError

MAX_BYTES = 64 * 1024 * 1024


def active_branch(rows, *, id_key='id', parent_key='parentId'):
    records = {}
    for row in rows:
        key = row.get(id_key)
        if not isinstance(key, str) or not key or key in records:
            raise ValueError('invalid native entry identity')
        records[key] = row
    chain, seen = [], set()
    current = rows[-1].get(id_key) if rows else None
    while current is not None:
        if current in seen or current not in records:
            raise ValueError('broken native parent chain')
        seen.add(current)
        row = records[current]
        chain.append(row)
        current = row.get(parent_key)
    return list(reversed(chain))


def occurred(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return None


def canonical_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        raise UnsupportedCapabilityError('原生会话标识无效') from None


def read_bytes(root: Path, relative: Path) -> bytes:
    """Open every component through directory FDs; never follow a leaf/link/FIFO."""
    if relative.is_absolute() or not relative.parts or any(p in ('', '.', '..') for p in relative.parts):
        raise ValueError('invalid transcript path')
    fd = os.open(root.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError('invalid transcript file')
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > MAX_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('transcript changed while reading')
            return raw
    finally:
        os.close(fd)


def tree_history(raw: bytes, *, ident: str, cwd: str, public_id: str | None = None) -> NativeHistory:
    """Read the active parent chain, exposing public user/assistant text only."""
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError('invalid native transcript')
    header = rows[0]
    if header.get('type') != 'session' or header.get('version') != 3 or header.get('id') != ident or not isinstance(header.get('cwd'), str) or Path(header['cwd']).resolve() != Path(cwd).resolve():
        raise ValueError('native transcript identity mismatch')
    chain = active_branch(rows[1:])
    entries = []
    omitted = False
    for row in chain:
        if row.get('type') != 'message':
            continue
        msg = row.get('message')
        if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant'):
            omitted = True
            continue
        content = msg.get('content')
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = '\n'.join(item['text'] for item in content if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str))
            omitted |= any(not isinstance(item, dict) or item.get('type') != 'text' for item in content)
        else:
            omitted = True
            continue
        if not text:
            continue
        try:
            occurred = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
        except (ValueError, KeyError, TypeError, AttributeError):
            occurred = None
        entries.append(NativeHistoryEntry(entry_id=f'{ident}:{row["id"]}', role=msg['role'], text=text,
                                         occurred_at=occurred))
    return NativeHistory(native_session_id=public_id or ident, entries=tuple(entries), complete=not omitted,
                         missing=('non_text_native_entries',) if omitted else ())
