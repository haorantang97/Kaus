"""Verified text references to local native history, with exact replay identity.

The opt-in watermark protects every record that predates this policy. Only
completed, byte-for-byte matched text is externalized. Host state, rejected
messages and unsupported rich content remain in Kaus. Native files are read
only; their cleanup and retention remain the engine's responsibility.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from typing import Any

from drivers.base import UnsupportedCapabilityError


def digest(text: str) -> str:
    return sha256(text.encode('utf-8')).hexdigest()


def with_text(row, value: str, *, entry_index: int | None = None):
    event = row.envelope.event
    if entry_index is not None:
        entries = list(event.data['entries'])
        entries[entry_index] = {**entries[entry_index], 'text': value}
        event = event.model_copy(update={'data': {**event.data, 'entries': entries}})
    elif event.type == 'extension.event':
        event = event.model_copy(update={'data': {**event.data, 'text': value}})
    else:
        event = event.model_copy(update={'text': value})
    return row.model_copy(update={'envelope': row.envelope.model_copy(update={'event': event})})


def user_message(row) -> bool:
    event = row.envelope.event
    return event.type == 'extension.event' and event.namespace == 'kaus' and event.name == 'user.message'


def reconciled_history(row) -> bool:
    event = row.envelope.event
    return event.type == 'extension.event' and event.namespace == 'kaus' and event.name == 'history.reconciled'


def references(row):
    ref = row.native_reference
    return ref.get('parts', (ref,)) if ref else ()


class NativeTextStorage:
    def __init__(self, repository):
        self.repository = repository
        self.loader = None

    async def begin(self, conversation_id: str, native_session_id: str | None) -> None:
        if not hasattr(self.repository, 'get_native_storage'):
            return
        if await self.repository.get_native_storage(conversation_id) is not None:
            return
        latest = await self.repository.latest_sequence(conversation_id)
        await self.repository.save_native_storage(conversation_id, {
            'fromSequence': 0 if latest is None else latest + 1,
            'nativeSessionId': native_session_id,
            'mode': 'kaus', 'status': 'pending_verification', 'missing': [],
        })

    async def status(self, conversation_id: str) -> dict[str, Any]:
        if not hasattr(self.repository, 'get_native_storage'):
            return {'mode': 'kaus', 'status': 'local', 'missing': []}
        return await self.repository.get_native_storage(conversation_id) or {
            'mode': 'kaus', 'status': 'local', 'missing': [],
        }

    async def _state(self, conversation_id, state, **changes):
        updated = {**state, **changes}
        if updated != state:
            await self.repository.save_native_storage(conversation_id, updated)
        return updated

    async def compact(self, conversation_id: str, native_session_id: str | None) -> int:
        state = await self.status(conversation_id)
        if 'fromSequence' not in state or not native_session_id or self.loader is None:
            return 0
        rows = await self.repository.list_after(conversation_id)
        # Active turns and trailing user drafts are never released.
        terminal = max((r.sequence for r in rows if r.envelope.parent_run_id is None
            and (r.envelope.event.type in ('run.completed', 'run.failed', 'run.interrupted')
                 or (reconciled_history(r) and not r.envelope.event.data.get('pending')))), default=-1)
        candidates = [r for r in rows if state['fromSequence'] <= r.sequence <= terminal
                      and r.native_reference is None and r.envelope.parent_run_id is None]
        try:
            history = await self.loader(conversation_id, native_session_id)
        except UnsupportedCapabilityError:
            await self._state(conversation_id, state, status='fallback', missing=['local_source_unsupported'])
            return 0
        except Exception:
            await self._state(conversation_id, state, status='source_unavailable', missing=['native_source_unavailable'])
            return 0
        if not history.complete or history.missing or history.earlier_cursor:
            await self._state(conversation_id, state, status='fallback',
                missing=list(history.missing or ('incomplete_native_history',)))
            return 0
        if history.native_session_id != native_session_id:
            await self._state(conversation_id, state, status='fallback', missing=['native_session_mismatch'])
            return 0
        entries = defaultdict(list)
        seen_ids = set()
        for entry in history.entries:
            if entry.entry_id in seen_ids:
                await self._state(conversation_id, state, status='fallback', missing=['duplicate_native_identity'])
                return 0
            seen_ids.add(entry.entry_id)
            if entry.kind == 'message' and entry.role in ('user', 'assistant') and entry.text:
                entries[(entry.role, entry.text)].append(entry)
        used = {ref['entryId'] for row in rows for ref in references(row)
                if ref['nativeSessionId'] == native_session_id}
        groups = []
        # Host-originated messages without a matching native row stay local.
        for index, row in enumerate(candidates):
            if not user_message(row) or row.envelope.event.data.get('attachments') or row.envelope.event.data.get('attachmentCount'):
                continue
            later = candidates[index + 1:]
            boundary = next((i for i, other in enumerate(later) if user_message(other)), len(later))
            if any(other.envelope.event.type == 'extension.event' and other.envelope.event.namespace == 'kaus'
                   and other.envelope.event.name == 'user.message.failed' for other in later[:boundary]):
                continue
            if not row.envelope.event.data.get('restored') and not any(
                other.envelope.event.type == 'run.started' for other in later[:boundary]
            ):
                continue
            value = row.envelope.event.data.get('text')
            if isinstance(value, str) and value:
                groups.append(('user', value, [(row, 0, len(value))]))
        messages = defaultdict(list)
        for row in candidates:
            if row.envelope.event.type in ('message.delta', 'message.completed'):
                messages[row.envelope.event.message_id].append(row)
        for parts in messages.values():
            completed = [r for r in parts if r.envelope.event.type == 'message.completed']
            if len(completed) != 1 or parts[-1] != completed[0]:
                continue
            deltas = [r for r in parts if r.envelope.event.type == 'message.delta']
            streamed = ''.join(r.envelope.event.text for r in deltas)
            value = completed[0].envelope.event.text
            value = streamed if value is None else value
            if not value or (deltas and streamed != value):
                continue
            mapped = []
            offset = 0
            for row in deltas:
                size = len(row.envelope.event.text)
                if size:
                    mapped.append((row, offset, offset + size))
                offset += size
            if completed[0].envelope.event.text:
                mapped.append((completed[0], 0, len(value)))
            groups.append(('assistant', value, mapped))
        replacements = []
        unmatched = False
        for role, value, mapped in groups:
            matches = entries.get((role, value), [])
            # Text-only identity is safe only when unambiguous. Repeated equal
            # messages retain the cache until a driver supplies stronger anchors.
            if len(matches) != 1 or matches[0].entry_id in used:
                unmatched = True
                continue
            entry = matches[0]
            used.add(entry.entry_id)
            for row, start, end in mapped:
                reference = {'nativeSessionId': native_session_id, 'entryId': entry.entry_id,
                    'role': role, 'start': start, 'end': end, 'sha256': digest(value)}
                replacements.append(with_text(row, '').model_copy(update={'native_reference': reference}))
        by_id = {entry.entry_id: entry for entry in history.entries}
        for row in candidates:
            if not reconciled_history(row) or row.envelope.event.data.get('nativeSessionId') != native_session_id:
                continue
            parts = []
            replacement = row
            for index, item in enumerate(row.envelope.event.data.get('entries', ())):
                if not isinstance(item, dict) or item.get('kind', 'message') != 'message':
                    continue
                value = item.get('text')
                entry = by_id.get(item.get('entryId'))
                if not isinstance(value, str) or not value:
                    continue
                if entry is None or entry.kind != 'message' or entry.role not in ('user', 'assistant') or (
                    entry.role != item.get('role') or entry.text != value
                ):
                    unmatched = True
                    continue
                # Reconciliation already supplies the exact native ID, so equal
                # text from separate native messages is unambiguous here.
                parts.append({'nativeSessionId': native_session_id, 'entryId': entry.entry_id,
                    'role': entry.role, 'start': 0, 'end': len(value), 'sha256': digest(value),
                    'entryIndex': index})
                replacement = with_text(replacement, '', entry_index=index)
            if parts:
                replacements.append(replacement.model_copy(update={'native_reference': {'parts': parts}}))
        count = await self.repository.replace_with_native_references(replacements) if replacements else 0
        reference_count = count + sum(r.native_reference is not None for r in rows)
        # Non-text body types are deliberately retained, never represented as
        # an all-content native migration merely because text matched.
        replaced_ids = {r.event_id for r in replacements}
        retained_text = any(not r.native_reference and r.event_id not in replaced_ids and (
            (user_message(r) and r.envelope.event.data.get('text')) or
            (r.envelope.event.type in ('message.delta', 'message.completed') and r.envelope.event.text) or
            (reconciled_history(r) and any(isinstance(item, dict) and item.get('text')
                for item in r.envelope.event.data.get('entries', ())))
        ) for r in rows)
        unmatched = unmatched or retained_text
        rich = any((reconciled_history(r) and any(isinstance(item, dict) and (
                       item.get('kind', 'message') != 'message' or item.get('metadata')
                   ) for item in r.envelope.event.data.get('entries', ()))) or
                   r.envelope.event.type.startswith(('tool.', 'reasoning.', 'terminal.', 'file.', 'artifact.'))
                   or (user_message(r) and r.envelope.event.data.get('attachments')) for r in rows)
        coverage_status = 'hybrid' if unmatched or rich else ('native' if reference_count else 'fallback')
        coverage_missing = (['unmatched_text'] if unmatched else []) + (['host_rich_content'] if rich else [])
        await self._state(conversation_id, state, nativeSessionId=native_session_id,
            mode='native_text' if reference_count else 'kaus', status=coverage_status,
            missing=coverage_missing, coverageStatus=coverage_status, coverageMissing=coverage_missing)
        return count

    async def hydrate(self, rows):
        refs = [r for r in rows if r.native_reference]
        if not refs:
            return tuple(rows)
        sources = {}
        failures = defaultdict(set)
        for row in refs:
            for ref in references(row):
                key = (row.conversation_id, ref['nativeSessionId'])
                if key in sources:
                    continue
                try:
                    history = await self.loader(*key) if self.loader else None
                    if history is None or history.native_session_id != key[1]:
                        raise ValueError('native_source_unavailable')
                    source = {}
                    for entry in history.entries:
                        if entry.entry_id in source:
                            raise ValueError('duplicate_native_identity')
                        source[entry.entry_id] = entry
                    sources[key] = source
                except Exception:
                    sources[key] = {}
        result = []
        for row in rows:
            hydrated = row
            for ref in references(row):
                entry = sources[(row.conversation_id, ref['nativeSessionId'])].get(ref['entryId'])
                value = entry.text if entry is not None and entry.role == ref['role'] else None
                if value is None or digest(value) != ref['sha256']:
                    # Respect native removal or compaction. Never restore a stale
                    # local body or invent a new history event to fill the gap.
                    failures[row.conversation_id].add('native_content_unavailable')
                    hydrated = with_text(hydrated, '', entry_index=ref.get('entryIndex'))
                else:
                    hydrated = with_text(hydrated, value[ref['start']:ref['end']], entry_index=ref.get('entryIndex'))
            result.append(hydrated)
        for conversation_id in {r.conversation_id for r in refs}:
            state = await self.status(conversation_id)
            if failures[conversation_id]:
                await self._state(conversation_id, state, status='source_unavailable',
                                  missing=sorted(failures[conversation_id]))
            elif state.get('status') == 'source_unavailable':
                # A partial SSE range cannot establish that earlier missing
                # references recovered. Validate the whole reference set first.
                all_rows = await self.repository.list_after(conversation_id)
                all_refs = [ref for row in all_rows for ref in references(row)]
                recovered = all(
                    (entry := sources.get((conversation_id, ref['nativeSessionId']), {}).get(ref['entryId'])) is not None
                    and entry.role == ref['role'] and isinstance(entry.text, str)
                    and digest(entry.text) == ref['sha256'] for ref in all_refs
                )
                if recovered:
                    await self._state(conversation_id, state,
                        status=state.get('coverageStatus', 'native'),
                        missing=state.get('coverageMissing', []))
        return tuple(result)
