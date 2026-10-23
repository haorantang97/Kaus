"""Native Alma thread ownership and optional chat-only terminal handoff."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import quote
import uuid

import httpx

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from drivers.alma_bridge.bridge import local_url
from drivers.skill_execution.bridge_terminal import manifest
from drivers.skill_execution.terminal_settings import approval, selected

TUI_HASH = '9a24c27def8cd94de35cd660339e193dc18a6a7c015f3b01f26a571f369bcc8f'


def owned_record(spec, binding, ident):
    try:
        if str(uuid.UUID(ident)) != ident:
            raise ValueError
        args = spec.command
        root = (Path(args[args.index('--state-dir') + 1]) if '--state-dir' in args else
                Path(spec.build_env().get('HOME', str(Path.home()))) / '.local/share/kaus/alma-bridge')
        path = root.expanduser() / (ident + '.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError
        value = json.loads(path.read_text())
        cwd = (binding.runtime_config.get('workspace_root') if binding else None) or spec.cwd or os.getcwd()
        expected_url = args[args.index('--url') + 1] if '--url' in args else spec.build_env().get('ALMA_API_URL', 'http://127.0.0.1:23001')
        if value['cwd'] != str(Path(cwd).resolve()) or local_url(value['url']) != local_url(expected_url):
            raise ValueError
        if not isinstance(value['threadId'], str) or not value['threadId']:
            raise ValueError
        return path, value
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise UnsupportedCapabilityError('Alma 会话不可用，请重新载入对话') from None


def external_cli_command(plan, command, *, conversation, binding, spec):
    path, record = owned_record(spec, binding, conversation.native_session_id)
    permission = approval(conversation)
    if permission not in (None, 'read_only', 'chat') or record.get('mode', 'chat') != 'chat':
        raise UnsupportedCapabilityError('Alma 终端暂不支持工具审批，请在 Kaus 中继续')
    tui = Path(command[0]).resolve().with_name('tui.mjs')
    try:
        if hashlib.sha256(tui.read_bytes()).hexdigest() != TUI_HASH:
            raise ValueError
    except (OSError, ValueError):
        raise UnsupportedCapabilityError('当前 Alma 终端版本尚未适配') from None
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if not node:
        raise UnsupportedCapabilityError('Alma 终端需要 Node.js')
    model = selected(conversation, 'model_id', 'model') or record.get('model')
    value = {'recordPath': str(path.resolve()), 'nativeId': record['threadId'], 'cwd': record['cwd'],
             'url': record['url'], 'mode': 'chat', 'model': model, 'tui': str(tui), 'node': node}
    settings = manifest(spec, conversation, value)
    return (sys.executable, str(Path(__file__).with_name('terminal.py')), str(settings))


async def read_messages(client, native_id):
    rows, offset, seen = [], 0, set()
    for _ in range(500):
        response = await client.get('/api/threads/' + quote(native_id, safe='') + '/messages',
                                    params={'page': 1, 'limit': 200, 'offset': offset})
        response.raise_for_status()
        page = response.json()
        if isinstance(page, list):
            return page
        if not isinstance(page, dict) or not isinstance(page.get('messages'), list):
            raise ValueError('Alma message contract changed')
        for row in page['messages']:
            ident = row.get('id')
            if ident in seen:
                continue
            seen.add(ident)
            rows.append(row)
        if not page.get('hasMore'):
            return rows
        new_offset = page.get('nextOffset')
        if not isinstance(new_offset, int) or new_offset <= offset:
            raise ValueError('Alma message pagination did not advance')
        offset = new_offset
    raise ValueError('Alma history is too large')


def public_messages(rows):
    result = []
    for row in rows:
        message = row.get('message', row)
        if isinstance(message, str):
            message = json.loads(message)
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
            continue
        parts = message.get('parts', [])
        if not parts and isinstance(message.get('content'), str):
            parts = [{'type': 'text', 'text': message['content']}]
        parts = [part for part in parts if isinstance(part, dict) and part.get('type') == 'text'
                 and isinstance(part.get('text'), str) and not part.get('hidden')]
        if parts:
            result.append({'id': row['id'], 'createdAt': row.get('createdAt'),
                           'message': {'role': message['role'], 'parts': parts}})
    return result


async def load_native_history(spec, binding, native_session_id):
    _, record = owned_record(spec, binding, native_session_id)
    try:
        async with httpx.AsyncClient(base_url=record['url'], timeout=20, trust_env=False, follow_redirects=False) as client:
            native = await client.get('/api/threads/' + quote(record['threadId'], safe=''))
            native.raise_for_status()
            if native.json().get('id') != record['threadId']:
                raise ValueError
            rows = public_messages(await read_messages(client, record['threadId']))
        entries = []
        for row in rows:
            created = row.get('createdAt')
            stamp = datetime.fromisoformat(created.replace('Z', '+00:00')) if isinstance(created, str) else datetime.fromtimestamp(0, timezone.utc)
            entries.append(NativeHistoryEntry(entry_id=row['id'], role=row['message']['role'],
                text='\n'.join(part['text'] for part in row['message']['parts']), occurred_at=stamp))
        return NativeHistory(native_session_id=native_session_id, entries=tuple(entries), complete=False,
                             missing=('tool_details', 'attachments', 'permission_decisions'))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise UnsupportedCapabilityError('Alma 历史暂时无法读取，请确认本地服务正在运行') from None
