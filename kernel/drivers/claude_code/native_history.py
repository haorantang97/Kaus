"""Exact, read-only Claude Code project history for plain public messages.

Unsupported blocks and compacted/branched transcripts are reported as partial;
this reader never promotes a text projection to complete rich history.
"""
from pathlib import Path
from uuid import UUID

from drivers.archive_sources.public_history import records, timestamp
from drivers.base import NativeHistory, NativeHistoryEntry


async def load_native_history(spec, binding, native_session_id):
    import asyncio
    return await asyncio.to_thread(_read, spec, native_session_id)


def _read(spec, native_session_id):
    identity = str(UUID(native_session_id))
    env = spec.build_env()
    root = Path(env.get('CLAUDE_CONFIG_DIR') or Path(env.get('HOME') or Path.home()) / '.claude').expanduser().resolve()
    candidates = sorted((root / 'projects').glob('*/' + identity + '.jsonl'))
    # More than one exact ledger is ambiguous; never silently choose newest.
    if len(candidates) != 1:
        raise FileNotFoundError('native_history_unavailable_or_ambiguous')
    entries = []
    missing = set()
    identities = set()
    public_parent_children = {}
    confirmed = False
    for _, row in records(candidates[0], root):
        session = row.get('sessionId')
        if session is not None and session != identity:
            raise ValueError('native_history_identity_mismatch')
        confirmed = confirmed or session == identity
        if row.get('isCompactSummary') or row.get('subtype') == 'compact_boundary':
            missing.add('compacted_segments')
        role = row.get('type')
        if role not in ('user', 'assistant'):
            continue
        if row.get('isSidechain'):
            missing.add('sidechain_history')
            continue
        message = row.get('message')
        if not isinstance(message, dict) or message.get('role', role) != role:
            missing.add('unrecognized_message')
            continue
        entry_id = row.get('uuid')
        if not isinstance(entry_id, str) or not entry_id or entry_id in identities:
            missing.add('unstable_native_identity')
            continue
        identities.add(entry_id)
        parent = row.get('parentUuid')
        if parent:
            if parent in public_parent_children:
                missing.add('branched_history')
            public_parent_children[parent] = entry_id
        content = message.get('content')
        if isinstance(content, str):
            value = content
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get('type') != 'text' or not isinstance(part.get('text'), str):
                    missing.add('non_text_content')
            value = ''.join(part['text'] for part in content if isinstance(part, dict)
                and part.get('type') == 'text' and isinstance(part.get('text'), str))
        else:
            missing.add('unrecognized_message')
            continue
        if value:
            entries.append(NativeHistoryEntry(entry_id='native:' + entry_id, role=role,
                text=value, occurred_at=timestamp(row.get('timestamp'))))
    if not confirmed:
        raise ValueError('native_history_identity_unverified')
    return NativeHistory(native_session_id=identity, entries=tuple(entries),
        complete=not missing, missing=tuple(sorted(missing)))
