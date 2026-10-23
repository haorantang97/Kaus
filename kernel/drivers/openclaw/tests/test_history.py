import json
import sqlite3
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec, AcpTransportError
from drivers.openclaw.history import native_route, public_entries


def test_route_reads_only_exact_acp_id_in_the_selected_workspace(tmp_path):
    state = tmp_path/'state'; state.mkdir(); database = state/'openclaw.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('create table acp_replay_sessions(session_id text, session_key text, cwd text)')
        db.executemany('insert into acp_replay_sessions values(?,?,?)', [('ours','agent:owned:ours',str(tmp_path/'work')), ('other','agent:other:private',str(tmp_path/'other'))])
    spec = AcpAgentSpec(command=('openclaw','acp'), env={'OPENCLAW_STATE_DIR':str(tmp_path)}, inherit_env=False)
    binding = SimpleNamespace(runtime_config={'workspace_root':str(tmp_path/'work')})
    assert native_route(spec,binding,'ours') == 'agent:owned:ours'
    for value in ('other', "' OR 1=1 --", 'missing'):
        with pytest.raises(AcpTransportError):native_route(spec,binding,value)
    saved=tmp_path/'saved.sqlite';database.rename(saved);database.symlink_to(saved)
    with pytest.raises(AcpTransportError):native_route(spec,binding,'ours')


def test_public_history_uses_native_ids_and_excludes_hidden_blocks():
    source={'sessionId':'native','messages':[
        {'role':'assistant','timestamp':1234,'content':[{'type':'thinking','thinking':'PRIVATE'}, {'type':'text','text':'Visible'}], '__openclaw':{'id':'a','private':'SECRET'}},
        {'role':'user','content':'Visible user','__openclaw':{'id':'u'}},
        {'role':'system','content':'PRIVATE','__openclaw':{'id':'s'}},
        {'role':'assistant','content':'No stable identity'},
    ]}
    entries,missing=public_entries(source,'native')
    assert [e.entry_id for e in entries] == ['native:a','native:u']
    assert [e.text for e in entries] == ['Visible','Visible user']
    assert missing == ('部分消息缺少原生编号',)
    assert all(e.metadata == {} for e in entries)
    assert 'PRIVATE' not in json.dumps([e.model_dump(mode='json') for e in entries])
    with pytest.raises(AcpTransportError):public_entries(source,'reset-native')


@pytest.mark.parametrize('failure', [None, 'cursor', 'changed', 'session', 'transport'])
async def test_gateway_history_reads_every_page_or_preserves_retryable_failure(monkeypatch, failure):
    from drivers.openclaw import history, driver
    messages = [{'role':'assistant', 'content':f'public-{i}', '__openclaw':{'id':str(i)}}
                for i in range(1250)]
    calls, closed = [], []
    class Gateway:
        def __init__(self, spec): pass
        async def close(self): closed.append(True)
        async def call(self, method, params):
            if method == 'sessions.describe': return {'session':{'sessionId':'native'}}
            calls.append(params)
            offset = params['offset']
            if offset and failure == 'transport': raise AcpTransportError('暂时不可读')
            page = messages[250:] if offset == 0 else messages[:251]
            return {'sessionId':'other' if offset and failure == 'session' else 'native',
                'messages':page, 'totalMessages':1251 if offset and failure == 'changed' else 1250,
                'hasMore':offset == 0, 'nextOffset':0 if failure == 'cursor' else 1000}
    monkeypatch.setattr(history, 'native_route', lambda *args: 'agent:owned:ours')
    monkeypatch.setattr(driver, 'OpenClawGateway', Gateway)
    if failure:
        with pytest.raises(AcpTransportError): await history.load_history(None, None, 'acp-id')
    else:
        result = await history.load_history(None, None, 'acp-id')
        assert result.complete and not result.missing
        assert [entry.text for entry in result.entries] == [f'public-{i}' for i in range(1250)]
        assert [row['offset'] for row in calls] == [0, 1000]
        assert all(row['sessionKey'] == 'agent:owned:ours' for row in calls)
    assert closed == [True]


def test_public_history_reports_native_truncation_attachments_and_tool_gaps():
    entries, missing = public_entries({'sessionId':'native', 'messages':[
        {'role':'assistant', 'content':'Shortened', '__openclaw':{'id':'a','truncated':True}},
        {'role':'user', 'content':[{'type':'image', 'data':'PRIVATE_BINARY'}], '__openclaw':{'id':'u'}},
        {'role':'toolResult', 'content':'Tool output', '__openclaw':{'id':'t'}},
    ]}, 'native')
    assert [entry.text for entry in entries] == ['Shortened']
    assert missing == ('原生历史包含截断内容', '原生附件未回填', '工具记录未回填')
