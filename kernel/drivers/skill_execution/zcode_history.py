"""Read public persisted ZCode text with its stable message IDs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from .bridge_terminal import session_record


def read(spec, binding, native_session_id):
    value = session_record(spec, binding, native_session_id, 'zcode-bridge')
    path = Path(value['directory']) / 'sessions.sqlite'
    if path.is_symlink() or not path.is_file():
        raise UnsupportedCapabilityError('原生会话历史不可用')
    entries = []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for ident, raw, created in db.execute(
                'SELECT id,data,time_created FROM message WHERE session_id=? ORDER BY sequence,time_created,id', (value['nativeId'],)):
            if len(raw) > 32 * 1024 * 1024:
                raise UnsupportedCapabilityError('原生消息超出读取上限')
            info = json.loads(raw)
            if info.get('role') not in ('user', 'assistant'):
                continue
            texts = []
            for (raw_part,) in db.execute('SELECT data FROM part WHERE session_id=? AND message_id=? ORDER BY sequence,time_created,id',
                                         (value['nativeId'], ident)):
                if len(raw_part) > 32 * 1024 * 1024:
                    raise UnsupportedCapabilityError('原生消息超出读取上限')
                part = json.loads(raw_part)
                if part.get('type') == 'text' and not part.get('hidden') and not part.get('ignored') and not part.get('synthetic'):
                    texts.append(str(part.get('text', '')))
            if texts:
                entries.append(NativeHistoryEntry(entry_id=ident, role=info['role'], text='\n'.join(texts),
                    occurred_at=datetime.fromtimestamp(created / 1000, timezone.utc)))
    return NativeHistory(native_session_id=native_session_id, entries=tuple(entries), complete=False,
                         missing=('tool_details', 'attachments', 'permission_decisions'))
