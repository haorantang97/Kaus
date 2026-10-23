"""Read public native messages from the exact owned Crush session, without running it."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from .bridge_terminal import session_record


def read(spec, binding, native_session_id):
    value = session_record(spec, binding, native_session_id, 'crush-bridge')
    path = Path(value['directory']) / 'data/crush.db'
    if path.is_symlink() or not path.is_file():
        raise UnsupportedCapabilityError('原生会话历史不可用')
    entries = []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for ident, role, parts, created in db.execute(
                'SELECT id,role,parts,created_at FROM messages WHERE session_id=? AND role IN (?,?) ORDER BY created_at,rowid',
                (value['nativeId'], 'user', 'assistant')):
            if len(parts) > 32 * 1024 * 1024:
                raise UnsupportedCapabilityError('原生消息超出读取上限')
            text = '\n'.join(str(part['data'].get('text', '')) for part in json.loads(parts)
                if part.get('type') == 'text' and isinstance(part.get('data'), dict) and not part['data'].get('hidden'))
            if text:
                entries.append(NativeHistoryEntry(entry_id=ident, role=role, text=text,
                    occurred_at=datetime.fromtimestamp(created, timezone.utc)))
    return NativeHistory(native_session_id=native_session_id, entries=tuple(entries), complete=False,
                         missing=('tool_details', 'attachments', 'permission_decisions'))
