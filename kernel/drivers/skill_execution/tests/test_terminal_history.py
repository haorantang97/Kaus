"""Public transcript readers: identity, active branch and bounded native input."""
import json
import os
from pathlib import Path
import uuid

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import commandcode, pi, copilot, qwen, omp, goose, grok
from drivers.skill_execution.history_io import read_bytes, tree_history
from test_selection import setup


def record(ident, cwd, rows=()):
    return ('\n'.join(json.dumps(row) for row in [
        {'type': 'session', 'version': 3, 'id': ident, 'cwd': str(cwd)}, *rows]) + '\n').encode()


def message(ident, parent, role, text):
    return {'type': 'message', 'id': ident, 'parentId': parent,
            'timestamp': '2026-10-16T00:00:00Z',
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}


def test_active_branch_and_public_text_only(tmp_path):
    ident = str(uuid.uuid4())
    rows = [message('a', None, 'user', 'first'), message('b', 'a', 'assistant', 'old branch'),
            message('c', 'a', 'assistant', 'active branch'),
            message('d', 'c', 'toolResult', 'private tool'), message('e', 'd', 'assistant', 'public')]
    rows[-1]['message']['content'].append({'type': 'thinking', 'thinking': 'hidden'})
    history = tree_history(record(ident, tmp_path, rows), ident=ident, cwd=str(tmp_path))
    assert [(e.entry_id, e.text) for e in history.entries] == [
        (ident + ':a', 'first'), (ident + ':c', 'active branch'), (ident + ':e', 'public')]
    assert not history.complete and history.missing == ('non_text_native_entries',)
    assert all(e.occurred_at is not None for e in history.entries)


@pytest.mark.parametrize('kind', ['wrong-id', 'wrong-cwd', 'unknown-version', 'duplicate-id', 'cycle', 'missing-parent'])
def test_tree_identity_and_chain_fail_closed(tmp_path, kind):
    ident = str(uuid.uuid4()); rows = [message('a', None, 'user', 'public')]
    raw = record(ident, tmp_path, rows)
    if kind == 'wrong-id': ident = str(uuid.uuid4())
    elif kind == 'wrong-cwd': tmp_path = tmp_path / 'other'
    elif kind == 'unknown-version': raw = raw.replace(b'"version": 3', b'"version": 9')
    elif kind == 'duplicate-id': raw += json.dumps(rows[0]).encode() + b'\n'
    elif kind == 'cycle': rows[0]['parentId'] = 'a'; raw = record(ident, tmp_path, rows)
    else: rows[0]['parentId'] = 'absent'; raw = record(ident, tmp_path, rows)
    with pytest.raises(ValueError): tree_history(raw, ident=ident, cwd=str(tmp_path))


@pytest.mark.parametrize('kind', ['link', 'parent-link', 'fifo', 'oversize'])
def test_transcript_file_is_never_followed_or_blocking(tmp_path, kind, monkeypatch):
    root = tmp_path / 'root'; root.mkdir(); target = tmp_path / 'outside'; target.mkdir()
    (target / 'log').write_text('private')
    if kind == 'link': (root / 'log').symlink_to(target / 'log'); relative = Path('log')
    elif kind == 'parent-link': (root / 'dir').symlink_to(target); relative = Path('dir/log')
    elif kind == 'fifo': os.mkfifo(root / 'log'); relative = Path('log')
    else:
        (root / 'log').write_text('large'); relative = Path('log')
        monkeypatch.setattr('drivers.skill_execution.history_io.MAX_BYTES', 2)
    with pytest.raises((OSError, ValueError)): read_bytes(root, relative)


@pytest.mark.parametrize('engine', ['pi', 'commandcode'])
async def test_exact_native_history_is_stable_and_read_only(tmp_path, engine, monkeypatch):
    spec, binding = setup(tmp_path, engine)
    spec = spec.with_env(HOME=str(tmp_path / 'home'))
    ident = str(uuid.uuid4()); native = ident
    if engine == 'pi':
        root = tmp_path / 'home/.pi/agent'
        safe = '--' + str(tmp_path).lstrip('/').replace('/', '-') + '--'
        target = root / 'sessions' / safe / ('timestamp_' + ident + '.jsonl')
    else:
        directory = tmp_path / 'bridge'; directory.mkdir()
        native = str(uuid.uuid4())
        data = {'version': 1, 'id': ident, 'cwd': str(tmp_path), 'nativeId': native,
                'model': 'model', 'effort': 'low', 'mode': 'plan', 'history': []}
        (directory / (ident + '.json')).write_text(json.dumps(data))
        spec = spec.model_copy(update={'command': ('bridge', '--state-dir', str(directory))})
        target = tmp_path / 'home/.commandcode/projects/any-unicode-slug' / (native + '.jsonl')
    target.parent.mkdir(parents=True)
    raw = record(native, tmp_path, [message('1', None, 'user', 'terminal question'), message('2', '1', 'assistant', 'terminal answer')])
    target.write_bytes(raw)
    adapter = {'pi': pi, 'commandcode': commandcode}[engine]
    first = await adapter.load_native_history(spec, binding, ident)
    second = await adapter.load_native_history(spec, binding, ident)
    assert first == second and first.native_session_id == ident
    assert [e.text for e in first.entries] == ['terminal question', 'terminal answer']
    assert target.read_bytes() == raw
    target.write_bytes(record(native, tmp_path / 'wrong', []))
    with pytest.raises(RuntimeError, match='历史暂不可读'):
        await adapter.load_native_history(spec, binding, ident)
    target.write_bytes(raw)
    with monkeypatch.context() as capped:
        capped.setattr('drivers.skill_execution.history_io.MAX_BYTES', 1)
        with pytest.raises(RuntimeError, match='历史暂不可读'):
            await adapter.load_native_history(spec, binding, ident)
    assert await adapter.load_native_history(spec, binding, ident) == first


@pytest.mark.parametrize('engine', ['copilot', 'qwen', 'omp', 'goose', 'grok'])
async def test_other_native_readers_public_scope_and_stable_ids(tmp_path, engine, monkeypatch):
    import sqlite3
    from urllib.parse import quote
    spec, binding = setup(tmp_path, engine)
    home = tmp_path / 'home'; home.mkdir()
    spec = spec.with_env(HOME=str(home), GOOSE_PATH_ROOT=str(home / 'goose'))
    ident = str(uuid.uuid4())
    if engine == 'copilot':
        root = home / '.copilot/session-state' / ident; root.mkdir(parents=True)
        rows = [{'type': 'session.start', 'id': '0', 'parentId': None, 'data': {'sessionId': ident, 'context': {'cwd': str(tmp_path)}}},
                {'type': 'user.message', 'id': '1', 'parentId': '0', 'data': {'content': 'question', 'transformedContent': 'hidden'}},
                {'type': 'assistant.message', 'id': '2', 'parentId': '1', 'data': {'content': 'answer'}},
                {'type': 'system.message', 'id': '3', 'parentId': '2', 'data': {'content': 'hidden'}}]
        (root / 'events.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    elif engine == 'qwen':
        root = home / '.qwen/projects/work/chats'; root.mkdir(parents=True)
        rows = [{'sessionId': ident, 'cwd': str(tmp_path), 'uuid': str(i), 'parentUuid': str(i-1) if i else None,
                 'type': role, 'provenance': provenance, 'message': {'parts': [{'text': text}]}}
                for i, (role, provenance, text) in enumerate([('user', 'real_user', 'question'), ('assistant', 'assistant_output', 'answer'), ('user', 'synthetic', 'hidden')])]
        (root / (ident + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in rows))
    elif engine == 'omp':
        root = home / '.omp/agent/sessions' / ('--' + str(tmp_path).lstrip('/').replace('/', '-') + '--'); root.mkdir(parents=True)
        (root / ('time_' + ident + '.jsonl')).write_bytes(b'{"type":"title","title":"hidden"}\n' + record(ident, tmp_path, [message('1', None, 'user', 'question'), message('2', '1', 'assistant', 'answer')]))
    elif engine == 'goose':
        root = home / 'goose/data/sessions'; root.mkdir(parents=True)
        db = sqlite3.connect(root / 'sessions.db')
        db.execute('CREATE TABLE sessions(id TEXT,working_dir TEXT)'); db.execute('INSERT INTO sessions VALUES(?,?)', (ident, str(tmp_path)))
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,message_id TEXT,session_id TEXT,role TEXT,content_json TEXT,created_timestamp INTEGER,metadata_json TEXT)')
        for i, (role, text, visible) in enumerate([('user', 'question', True), ('assistant', 'answer', True), ('user', 'hidden', False)]):
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (i, 'repeated-model-id', ident, role, json.dumps([{'type': 'text', 'text': text}]), 1700000000, json.dumps({'userVisible': visible})))
        db.commit(); db.close()
    else:
        root = home / '.grok/sessions' / quote(str(tmp_path), safe='') / ident; root.mkdir(parents=True)
        (root / 'summary.json').write_text(json.dumps({'info': {'id': ident, 'cwd': str(tmp_path)}}))
        rows = [{'timestamp': 1700000000, 'method': 'session/update', 'params': {'sessionId': ident,
                 '_meta': {'eventId': 'reset-counter-1', 'agentTimestampMs': i}, 'update': {'sessionUpdate': role, 'content': {'type': 'text', 'text': text}, '_meta': {'promptIndex': 0}}}}
                for i, (role, text) in enumerate([('user_message_chunk', 'question'), ('agent_message_chunk', 'answer')])]
        (root / 'updates.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    adapter = globals()[engine]
    first = await adapter.load_native_history(spec, binding, ident)
    second = await adapter.load_native_history(spec, binding, ident)
    assert first == second
    assert [e.text for e in first.entries] == ['question', 'answer']
    assert len({e.entry_id for e in first.entries}) == 2
    wrong = binding.model_copy(update={'runtime_config': {'workspace_root': str(tmp_path / 'other')}})
    with pytest.raises(RuntimeError, match='历史暂不可读'):
        await adapter.load_native_history(spec, wrong, ident)
    with monkeypatch.context() as capped:
        capped.setattr('drivers.skill_execution.history_io.MAX_BYTES', 1)
        with pytest.raises(RuntimeError, match='历史暂不可读'):
            await adapter.load_native_history(spec, binding, ident)
    assert await adapter.load_native_history(spec, binding, ident) == first


async def test_grok_rewind_filters_old_branch_and_host_turns(tmp_path):
    from urllib.parse import quote
    spec, binding = setup(tmp_path, 'grok'); spec = spec.with_env(GROK_HOME=str(tmp_path / 'grok'))
    ident = str(uuid.uuid4()); folder = tmp_path / 'grok/sessions' / quote(str(tmp_path), safe='') / ident
    folder.mkdir(parents=True); (folder / 'summary.json').write_text(json.dumps({'info': {'id': ident, 'cwd': str(tmp_path)}}))
    def event(index, kind, text, prompt=None, host=False):
        return {'timestamp': 1700000000, 'method': 'session/update', 'params': {'sessionId': ident,
            '_meta': {'eventId': 'counter-reset', 'agentTimestampMs': index}, 'update': {'sessionUpdate': kind,
            'content': {'type': 'text', 'text': text}, '_meta': {'promptIndex': prompt, 'hostTurn': host}}}}
    rows = [event(1, 'user_message_chunk', 'first', 0), event(2, 'agent_message_chunk', 'answer'),
            event(3, 'user_message_chunk', 'dead', 1), event(4, 'agent_message_chunk', 'dead answer'),
            {'method': '_x.ai/session/update', 'params': {'sessionId': ident, 'update': {'sessionUpdate': 'rewind_marker', 'target_prompt_index': 1}}},
            event(5, 'user_message_chunk', 'replacement', 1), event(6, 'user_message_chunk', 'hidden', host=True),
            event(7, 'agent_message_chunk', 'new answer')]
    (folder / 'updates.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    result = await grok.load_native_history(spec, binding, ident)
    assert [e.text for e in result.entries] == ['first', 'answer', 'replacement', 'new answer']
    assert len({e.entry_id for e in result.entries}) == 4


async def test_goose_reads_committed_wal_without_changing_original_database(tmp_path):
    import hashlib
    import sqlite3
    spec, binding = setup(tmp_path, 'goose'); spec = spec.with_env(GOOSE_PATH_ROOT=str(tmp_path / 'goose'))
    folder = tmp_path / 'goose/data/sessions'; folder.mkdir(parents=True)
    db = sqlite3.connect(folder / 'sessions.db'); db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE sessions(id TEXT,working_dir TEXT)')
    db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,message_id TEXT,session_id TEXT,role TEXT,content_json TEXT,created_timestamp INTEGER,metadata_json TEXT)')
    db.execute('INSERT INTO sessions VALUES(?,?)', ('exact-id', str(tmp_path)))
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (1, 'native-message', 'exact-id', 'assistant', '[{"type":"text","text":"committed WAL"}]', 1700000000, '{"userVisible":true}'))
    db.commit()
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    try:
        result = await goose.load_native_history(spec, binding, 'exact-id')
        assert [e.text for e in result.entries] == ['committed WAL']
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()} == before
    finally:
        db.close()
