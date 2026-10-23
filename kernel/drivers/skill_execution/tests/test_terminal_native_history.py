import json
from pathlib import Path
from uuid import uuid4

import pytest

from drivers.skill_execution import codex, gemini
from test_selection import setup


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


async def test_codex_public_events_are_not_duplicated_by_response_items_or_internal_context(tmp_path):
    spec, binding = setup(tmp_path, 'codex')
    spec = spec.with_env(CODEX_HOME=str(tmp_path/'home'))
    sid = str(uuid4());path = tmp_path/'home/sessions/2026/01/01'/('rollout-'+sid+'.jsonl')
    rows = [
        {'type':'session_meta','payload':{'id':sid}},
        {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'PRIVATE_INSTRUCTIONS'}]}},
        {'type':'event_msg','payload':{'type':'user_message','message':'public question','images':['private-image']}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'public answer'}]}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'public answer'}},
        {'type':'event_msg','payload':{'type':'agent_reasoning','text':'PRIVATE_THOUGHTS'}},
    ]
    write(path,rows)
    first = await codex.load_native_history(spec,binding,sid)
    assert [(e.role,e.text) for e in first.entries]==[('user','public question'),('assistant','public answer')]
    assert 'PRIVATE' not in first.model_dump_json()
    assert not first.complete and 'non_text_content' in first.missing
    rows.append({'type':'event_msg','payload':{'type':'user_message','message':'public question'}})
    write(path,rows)
    second=await codex.load_native_history(spec,binding,sid)
    assert [e.entry_id for e in second.entries[:2]]==[e.entry_id for e in first.entries]
    assert len({e.entry_id for e in second.entries})==3


async def test_gemini_native_updates_rewind_and_replacement_use_original_ids(tmp_path):
    spec,binding=setup(tmp_path,'gemini');spec=spec.with_env(GEMINI_CLI_HOME=str(tmp_path/'home'))
    sid=str(uuid4());path=tmp_path/'home/.gemini/tmp/work/chats'/('session-date-'+sid[:8]+'.jsonl')
    rows=[{'sessionId':sid}, {'id':'a','type':'user','content':'question'},
        {'id':'b','type':'gemini','content':'partial','thoughts':[{'text':'PRIVATE'}]},
        {'id':'b','type':'gemini','content':'answer'},
        {'id':'context','type':'user','content':'<session_context>PRIVATE</session_context>'},
        {'id':'hook','type':'user','content':'  <hook_context>PRIVATE</hook_context>'},
        {'id':'command','type':'user','content':'/quit'},
        {'id':'help','type':'user','content':'?help'},
        {'id':'c','type':'user','content':'rewound'}, {'$rewindTo':'c'},
        {'id':'d','type':'gemini','content':[{'text':'PRIVATE','thought':True},{'text':'public'},
            {'inlineData':{'mimeType':'image/png','data':'PRIVATE_BINARY'}}]}]
    write(path,rows)
    history=await gemini.load_native_history(spec,binding,sid)
    assert [(e.entry_id,e.text) for e in history.entries]==[('native:a','question'),('native:b','answer'),('native:d','public')]
    assert 'PRIVATE' not in history.model_dump_json()
    assert not history.complete and 'non_text_content' in history.missing
    rows.append({'$set':{'messages':[{'id':'b','type':'gemini','content':'compacted visible'}]}});write(path,rows)
    assert [(e.entry_id,e.text) for e in (await gemini.load_native_history(spec,binding,sid)).entries]==[('native:b','compacted visible')]


async def test_codex_completed_items_keep_native_ids_and_skip_legacy_twins(tmp_path):
    spec, binding = setup(tmp_path, 'codex')
    spec = spec.with_env(CODEX_HOME=str(tmp_path/'home'))
    sid = str(uuid4());path = tmp_path/'home/sessions'/('rollout-'+sid+'.jsonl')
    write(path, [
        {'type':'session_meta','payload':{'id':sid}},
        {'type':'event_msg','payload':{'type':'task_started','turn_id':'old'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'older question'}},
        {'type':'event_msg','payload':{'type':'task_started','turn_id':'new'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'question'}},
        {'type':'event_msg','payload':{'type':'item_completed','turn_id':'new','item':{
            'type':'UserMessage','id':'user-native','content':[{'type':'text','text':'question'}]}}},
        {'type':'event_msg','payload':{'type':'item_completed','turn_id':'new','item':{
            'type':'Reasoning','id':'private','content':[{'type':'Text','text':'PRIVATE'}]}}},
        {'type':'event_msg','payload':{'type':'item_completed','turn_id':'new','item':{
            'type':'AgentMessage','id':'assistant-native','content':[{'type':'Text','text':'answer'}]}}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'answer'}},
    ])
    result = await codex.load_native_history(spec, binding, sid)
    assert [e.text for e in result.entries] == ['older question', 'question', 'answer']
    assert [e.entry_id for e in result.entries[1:]] == ['native:user-native', 'native:assistant-native']
    assert 'PRIVATE' not in result.model_dump_json()


async def test_gemini_legacy_json_and_wrong_session_prefix_are_not_confused(tmp_path):
    spec,binding=setup(tmp_path,'gemini');spec=spec.with_env(HOME=str(tmp_path/'home'))
    sid=str(uuid4());path=tmp_path/'home/.gemini/tmp/work/chats'/('session-date-'+sid[:8]+'.json')
    write(path,[{'sessionId':sid,'messages':[{'id':'a','type':'user','content':'legacy'}]}])
    assert (await gemini.load_native_history(spec,binding,sid)).entries[0].text=='legacy'
    write(path,[{'sessionId':str(uuid4()),'messages':[{'id':'wrong','type':'user','content':'foreign'}]}])
    with pytest.raises(RuntimeError,match='原生记录'):
        await gemini.load_native_history(spec,binding,sid)


@pytest.mark.parametrize('adapter,variable,directory',[
    (codex,'CODEX_HOME','sessions'),(gemini,'GEMINI_CLI_HOME','.gemini/tmp/work/chats')])
async def test_native_history_never_follows_session_symlink_outside_owned_home(tmp_path,adapter,variable,directory):
    spec,binding=setup(tmp_path);spec=spec.with_env(**{variable:str(tmp_path/'home')})
    sid=str(uuid4());root=tmp_path/'home'/directory;root.mkdir(parents=True)
    name='rollout-'+sid+'.jsonl' if adapter is codex else 'session-date-'+sid[:8]+'.jsonl'
    outside=tmp_path/'outside.jsonl';outside.write_text('{"private":"must not read"}\n');(root/name).symlink_to(outside)
    with pytest.raises(RuntimeError,match='原生记录'):
        await adapter.load_native_history(spec,binding,sid)


@pytest.mark.parametrize('adapter,variable,directory',[
    (codex,'CODEX_HOME','sessions'),(gemini,'GEMINI_CLI_HOME','.gemini/tmp/work/chats')])
async def test_history_missing_and_corrupt_are_retryable_but_exact_empty_is_valid(tmp_path,adapter,variable,directory):
    spec,binding=setup(tmp_path);spec=spec.with_env(**{variable:str(tmp_path/'home')})
    sid=str(uuid4());root=tmp_path/'home'/directory;root.mkdir(parents=True)
    name='rollout-'+sid+'.jsonl' if adapter is codex else 'session-date-'+sid[:8]+'.jsonl'
    with pytest.raises(RuntimeError, match='原生记录'):
        await adapter.load_native_history(spec,binding,sid)
    path=root/name;path.write_text('{invalid json}\n')
    with pytest.raises(RuntimeError, match='原生记录'):
        await adapter.load_native_history(spec,binding,sid)
    write(path,[{'type':'session_meta','payload':{'id':sid}} if adapter is codex else {'sessionId':sid}])
    history=await adapter.load_native_history(spec,binding,sid)
    assert not history.entries and history.complete
