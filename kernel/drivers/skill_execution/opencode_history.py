"""Read public OpenCode-family native messages from a private SQLite snapshot."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from .history_io import read_bytes


def read(spec, binding, ident, *, engine):
    if not isinstance(ident, str) or not re.fullmatch(r'ses[A-Za-z0-9_-]{1,128}', ident):
        raise UnsupportedCapabilityError('原生会话标识无效')
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home())
    root = Path(env.get('XDG_DATA_HOME') or home / '.local/share') / engine
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    relative = Path(engine + '.db')
    try:
        data = read_bytes(root, relative)
        try:
            wal = read_bytes(root, Path(str(relative) + '-wal'))
        except FileNotFoundError:
            wal = None
        if read_bytes(root, relative) != data:
            raise ValueError('native database changed')
        with tempfile.TemporaryDirectory(prefix='kaus-native-history-') as directory:
            path = Path(directory) / relative
            path.write_bytes(data); path.chmod(0o600)
            if wal is not None:
                sidecar = Path(str(path) + '-wal'); sidecar.write_bytes(wal); sidecar.chmod(0o600)
            conn = sqlite3.connect(path)
            try:
                conn.execute('PRAGMA query_only=ON')
                session = conn.execute('SELECT directory FROM session WHERE id=?', (ident,)).fetchone()
                if not cwd or not session or Path(session[0]).resolve() != Path(cwd).resolve():
                    raise ValueError('native history identity mismatch')
                messages = conn.execute('SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created,id', (ident,)).fetchall()
                parts = conn.execute('SELECT message_id,data FROM part WHERE session_id=? ORDER BY time_created,id', (ident,)).fetchall()
            finally:
                conn.close()
        by_message = {}
        omitted = False
        for key, raw in parts:
            item = json.loads(raw)
            if item.get('type') == 'file':
                omitted = True
            if item.get('type') == 'text' and not item.get('ignored') and not item.get('synthetic') and isinstance(item.get('text'), str):
                by_message.setdefault(key, []).append(item['text'])
        entries = []
        for key, created, raw in messages:
            role = json.loads(raw).get('role')
            text = '\n'.join(by_message.get(key, []))
            if role in ('user', 'assistant') and text:
                entries.append(NativeHistoryEntry(entry_id=ident + ':' + key, role=role, text=text,
                    occurred_at=datetime.fromtimestamp(created / 1000, timezone.utc)))
        return NativeHistory(native_session_id=ident, entries=tuple(entries), complete=not omitted,
            missing=('non_text_native_entries',) if omitted else ())
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, OverflowError):
        raise RuntimeError('原生会话历史暂不可读') from None


async def load(spec, binding, ident, *, engine):
    return await asyncio.to_thread(read, spec, binding, ident, engine=engine)
