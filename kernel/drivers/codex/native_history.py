"""Public messages from an exact native rollout, retaining append-only identity."""
from pathlib import Path
from uuid import UUID

from drivers.archive_sources.public_history import records, text, timestamp
from drivers.base import NativeHistory, NativeHistoryEntry


async def load_native_history(spec, binding, native_session_id):
    import asyncio
    try:
        return await asyncio.to_thread(_read_native_history, spec, native_session_id)
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError('无法读取 Codex 原生记录，请重试') from None


def _read_native_history(spec, native_session_id):
    identity = str(UUID(native_session_id))
    env = spec.build_env()
    root = Path(env.get('CODEX_HOME') or Path(env.get('HOME', str(Path.home()))) / '.codex').expanduser().resolve()
    candidates = [path for directory in ('sessions', 'archived_sessions')
        for path in (root / directory).rglob('*' + identity + '.jsonl')]
    for path in sorted(candidates):
        rows = iter(records(path, root))
        first = next(rows, None)
        if first is None or first[1].get('type') != 'session_meta' or first[1].get('payload', {}).get('id') != identity:
            continue
        entries = []
        modern_turns = set()
        current_turn = None
        missing = set()
        for offset, row in rows:
            payload = row.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            # Native UI events carry the actual public user input. Response
            # items also contain injected workspace instructions and must not
            # be mistaken for messages the user typed.
            if row.get('type') != 'event_msg':
                if row.get('type') == 'response_item' and payload.get('type') in (
                        'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                    missing.add('tool_records')
                continue
            if payload.get('type') == 'task_started':
                current_turn = payload.get('turn_id')
            item = payload.get('item') if payload.get('type') == 'item_completed' else None
            modern = isinstance(item, dict)
            if modern:
                role = {'UserMessage': 'user', 'AgentMessage': 'assistant'}.get(item.get('type'))
                turn = payload.get('turn_id') or current_turn
                if role is not None:
                    modern_turns.add(turn)
                content = item.get('content')
                if role is not None and isinstance(content, list) and any(
                        isinstance(part, dict) and part.get('type') not in ('text', 'Text')
                        for part in content):
                    missing.add('non_text_content')
                # Native AgentMessage uses PascalCase Text; UserMessage uses
                # the public UserInput text schema.
                public = ''.join(part.get('text', '') for part in content or ()
                    if isinstance(part, dict) and part.get('type') in ('text', 'Text')
                    and isinstance(part.get('text'), str))
            else:
                role = {'user_message': 'user', 'agent_message': 'assistant'}.get(payload.get('type'))
                turn = current_turn
                public = text(payload.get('message'))
                if payload.get('images') or payload.get('local_images'):
                    missing.add('non_text_content')
            if role is None:
                continue
            if not public:
                if modern or payload.get('images') or payload.get('local_images'):
                    missing.add('non_text_content')
                continue
            # The append-only native record offset remains stable when later
            # turns are appended. Repeated identical messages stay distinct.
            entry_id = (item.get('id') if modern else payload.get('id')) or f'{identity}:{offset}'
            entries.append((turn, modern, NativeHistoryEntry(entry_id='native:' + str(entry_id), role=role,
                text=public, occurred_at=timestamp(row.get('timestamp')))))
        # Some native versions emit both compatibility events and completed
        # items. Keep their stable item IDs without duplicating a turn; retain
        # older turns that only have legacy events after a native upgrade.
        public_entries = tuple(entry for turn, modern, entry in entries
            if modern or turn not in modern_turns)
        return NativeHistory(native_session_id=identity, entries=public_entries,
            complete=not missing, missing=tuple(sorted(missing)))
    raise FileNotFoundError('native_history_unavailable')
