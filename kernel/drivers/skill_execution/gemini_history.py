"""Read public messages from Gemini's native append-only chat records."""
from pathlib import Path
from uuid import UUID

from drivers.archive_sources.public_history import records, text, timestamp
from drivers.base import NativeHistory, NativeHistoryEntry


async def load_native_history(spec, binding, native_session_id):
    import asyncio
    try:
        return await asyncio.to_thread(_read_native_history, spec, native_session_id)
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError('无法读取 Gemini 原生记录，请重试') from None


def _read_native_history(spec, native_session_id):
    identity = str(UUID(native_session_id))
    env = spec.build_env()
    home = Path(env.get('GEMINI_CLI_HOME') or env.get('HOME') or Path.home()).expanduser().resolve()
    root = home / '.gemini' / 'tmp'
    candidates = sorted(root.glob('*/chats/session-*-' + identity[:8] + '.jsonl'))
    candidates += sorted(root.glob('*/chats/session-*-' + identity[:8] + '.json'))
    for path in candidates:
        stream = iter(records(path, root))
        first = next(stream, None)
        if first is None or first[1].get('sessionId') != identity:
            continue
        messages = {item['id']: item for item in first[1].get('messages', ())
            if isinstance(item, dict) and isinstance(item.get('id'), str)}
        for _, row in stream:
            if '$rewindTo' in row:
                ids = list(messages)
                index = ids.index(row['$rewindTo']) if row['$rewindTo'] in messages else 0
                for key in ids[index:]:
                    del messages[key]
            elif isinstance(row.get('$set'), dict) and isinstance(row['$set'].get('messages'), list):
                messages = {item['id']: item for item in row['$set']['messages']
                    if isinstance(item, dict) and isinstance(item.get('id'), str)}
            elif isinstance(row.get('id'), str) and row.get('type'):
                messages[row['id']] = row
        entries = []
        missing = set()
        for key, row in messages.items():
            if row.get('toolCalls'):
                missing.add('tool_records')
            if row.get('type') not in ('user', 'gemini'):
                continue
            content = row.get('displayContent') or row.get('content')
            if isinstance(content, list) and any(isinstance(part, dict) and not part.get('thought')
                    and not isinstance(part.get('text'), str) for part in content):
                missing.add('non_text_content')
            public = text(content)
            if not public:
                if content:
                    missing.add('non_text_content')
                continue
            # Gemini records internal context and local slash commands as user
            # rows, but excludes them when restoring its own public chat.
            if row['type'] == 'user' and public.lstrip().startswith(
                ('/', '?', '<session_context>', '<hook_context>')
            ):
                continue
            entries.append(NativeHistoryEntry(entry_id='native:' + key,
                role='assistant' if row['type'] == 'gemini' else 'user',
                text=public, occurred_at=timestamp(row.get('timestamp'))))
        return NativeHistory(native_session_id=identity, entries=tuple(entries),
            complete=not missing, missing=tuple(sorted(missing)))
    raise FileNotFoundError('native_history_unavailable')
