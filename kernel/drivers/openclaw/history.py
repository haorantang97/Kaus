"""Read a known ACP session's public Gateway transcript without inference."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from drivers.base import NativeHistory, NativeHistoryEntry
from drivers.acp.client import AcpTransportError

HISTORY_PAGE_SIZE = 1000


def native_route(spec, binding, session_id):
    env = spec.build_env()
    home = Path(env.get('OPENCLAW_HOME') or env.get('HOME') or '~').expanduser()
    root = Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw').expanduser()
    database = root / 'state' / 'openclaw.sqlite'
    if database.is_symlink() or not database.is_file():
        raise AcpTransportError('原生会话索引不可读')
    workspace = binding.runtime_config.get('workspace_root') or spec.cwd
    if not workspace:
        raise AcpTransportError('缺少原生会话工作目录')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT session_key, cwd FROM acp_replay_sessions WHERE session_id=?', (session_id,)).fetchone()
    if not row or Path(row[1]).resolve() != Path(workspace).resolve() or not isinstance(row[0], str):
        raise AcpTransportError('原生会话与工作目录不匹配')
    return row[0]


def public_entries(result, expected_id):
    if result.get('sessionId') != expected_id or not isinstance(result.get('messages'), list):
        raise AcpTransportError('原生会话历史已变化')
    entries = []
    missing = []
    for message in result['messages']:
        if not isinstance(message, dict):
            continue
        if message.get('role') in ('tool', 'toolResult'):
            missing.append('工具记录未回填')
        if message.get('role') not in ('user','assistant'):
            continue
        metadata = message.get('__openclaw') if isinstance(message.get('__openclaw'), dict) else {}
        if metadata.get('truncated') is True:
            missing.append('原生历史包含截断内容')
        identity = metadata.get('id')
        identity = identity or message.get('id')
        content = message.get('content')
        if isinstance(content, list):
            if any(isinstance(item, dict) and item.get('type') in
                   ('image', 'image_url', 'file', 'audio', 'resource', 'resource_link') for item in content):
                missing.append('原生附件未回填')
            if any(isinstance(item, dict) and item.get('type') in
                   ('toolCall', 'tool_use', 'tool_result') for item in content):
                missing.append('工具记录未回填')
        text = content if isinstance(content, str) else ''.join(
            item.get('text', '') for item in content or []
            if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)) if isinstance(content, list) else ''
        if not text:
            continue
        if not isinstance(identity, str) or not identity:
            missing.append('部分消息缺少原生编号')
            continue
        timestamp = message.get('timestamp')
        occurred = None
        try:
            if isinstance(timestamp, (int,float)):
                occurred = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            elif isinstance(timestamp, str):
                occurred = datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        except (ValueError, OverflowError, OSError):
            pass
        entries.append(NativeHistoryEntry(entry_id=expected_id+':'+identity, role=message['role'], text=text, occurred_at=occurred))
    return tuple(entries), tuple(dict.fromkeys(missing))


async def load_history(spec, binding, native_session_id):
    from .driver import OpenClawGateway
    key = await asyncio.to_thread(native_route, spec, binding, native_session_id)
    gateway = OpenClawGateway(spec)
    try:
        described = await gateway.call('sessions.describe', {'key':key})
        row = described.get('session')
        expected = row.get('sessionId') if isinstance(row, dict) else None
        if not isinstance(expected, str):
            raise AcpTransportError('原生会话不存在')
        pages, missing = [], []
        offset, total = 0, None
        while True:
            result = await gateway.call('chat.history', {
                'sessionKey':key,
                'limit':HISTORY_PAGE_SIZE, 'offset':offset, 'maxChars':500000})
            entries, gaps = public_entries(result, expected)
            pages.append(entries)
            missing.extend(gaps)
            count = result.get('totalMessages')
            if isinstance(count, int):
                if total is not None and total != count:
                    raise AcpTransportError('原生历史在读取期间发生变化，请重试')
                total = count
            if result.get('hasMore') is not True:
                break
            next_offset = result.get('nextOffset')
            if not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset <= offset:
                raise AcpTransportError('原生历史分页游标无效')
            offset = next_offset
        # Gateway offsets walk backwards from the newest record. Preserve the
        # native chronological order and collapse page-boundary overlap by ID.
        ordered = {}
        for page in reversed(pages):
            for entry in page:
                ordered[entry.entry_id] = entry
        missing = tuple(dict.fromkeys(missing))
        return NativeHistory(native_session_id=native_session_id,
            entries=tuple(ordered.values()), complete=not missing, missing=missing)
    finally:
        await gateway.close()
