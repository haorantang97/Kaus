"""Local opt-in hooks reuse known formats without launching agents or services."""
import asyncio
import importlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote
import uuid

import pytest

from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.base import UnsupportedCapabilityError
from runtime.native_storage import NativeTextStorage

ENGINES=('pi','commandcode','copilot','qwen','omp','goose','grok','crush','zcode')


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')


def synthetic_source(tmp_path, engine):
    home=tmp_path/'home';home.mkdir()
    work=tmp_path/'work';work.mkdir()
    ident=str(uuid.uuid4());native=ident
    spec=AcpAgentSpec(command=('must-never-launch',),cwd=str(work),inherit_env=False,
        env={'HOME':str(home),'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state'),
             'GOOSE_PATH_ROOT':str(home/'goose')})
    binding=AgentBinding.create(project='fixture',backend=engine,runtime_config={'workspace_root':str(work)})
    if engine in {'pi','omp','commandcode'}:
        if engine=='commandcode':
            bridge=tmp_path/'bridge';bridge.mkdir();native=str(uuid.uuid4())
            (bridge/(ident+'.json')).write_text(json.dumps({'version':1,'id':ident,'cwd':str(work),
                'nativeId':native,'model':'synthetic','effort':'low','mode':'plan','history':[]}))
            spec=spec.model_copy(update={'command':('must-never-launch','--state-dir',str(bridge))})
            path=home/'.commandcode/projects/project'/(native+'.jsonl')
        else:
            folder='--'+str(work).lstrip('/').replace('/','-')+'--'
            path=home/('.pi' if engine=='pi' else '.omp')/'agent/sessions'/folder/('time_'+ident+'.jsonl')
        rows=[{'type':'session','version':3,'id':native,'cwd':str(work)}]
        rows.extend({'type':'message','id':str(i),'parentId':str(i-1) if i else None,
            'message':{'role':role,'content':[{'type':'text','text':text}]}}
            for i,(role,text) in enumerate([('user','question'),('assistant','answer')]))
        if engine=='omp':rows.insert(0,{'type':'title','title':'Synthetic title'})
        write_jsonl(path,rows)
    elif engine=='copilot':
        rows=[{'type':'session.start','id':'start','parentId':None,
            'data':{'sessionId':ident,'context':{'cwd':str(work)}}},
            {'type':'user.message','id':'user','parentId':'start','data':{'content':'question'}},
            {'type':'assistant.message','id':'assistant','parentId':'user','data':{'content':'answer'}}]
        write_jsonl(home/'.copilot/session-state'/ident/'events.jsonl',rows)
    elif engine=='qwen':
        rows=[{'sessionId':ident,'cwd':str(work),'uuid':str(i),'parentUuid':str(i-1) if i else None,
            'type':role,'provenance':provenance,'message':{'parts':[{'text':text}]}}
            for i,(role,provenance,text) in enumerate([('user','real_user','question'),('assistant','assistant_output','answer')])]
        write_jsonl(home/'.qwen/projects/project/chats'/(ident+'.jsonl'),rows)
    elif engine=='goose':
        path=home/'goose/data/sessions/sessions.db';path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id TEXT,working_dir TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,message_id TEXT,session_id TEXT,role TEXT,content_json TEXT,created_timestamp INTEGER,metadata_json TEXT);')
            db.execute('INSERT INTO sessions VALUES (?,?)',(ident,str(work)))
            for i,(role,text) in enumerate([('user','question'),('assistant','answer')]):
                db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',(i,str(i),ident,role,
                    json.dumps([{'type':'text','text':text}]),1700000000,'{"userVisible":true}'))
    elif engine=='grok':
        root=home/'.grok/sessions'/quote(str(work),safe='')/ident;root.mkdir(parents=True)
        (root/'summary.json').write_text(json.dumps({'info':{'id':ident,'cwd':str(work)}}))
        rows=[{'method':'session/update','params':{'sessionId':ident,
            '_meta':{'eventId':str(i),'agentTimestampMs':i},
            'update':{'sessionUpdate':kind,'content':{'type':'text','text':text},'_meta':{'promptIndex':0}}}}
            for i,(kind,text) in enumerate([('user_message_chunk','question'),('agent_message_chunk','answer')])]
        write_jsonl(root/'updates.jsonl',rows)
    else:
        bridge=tmp_path/'bridge';directory=bridge/ident;directory.mkdir(parents=True)
        (directory/'session.json').write_text(json.dumps({'cwd':str(work),'native_id':'native-original'}))
        spec=spec.model_copy(update={'command':('must-never-launch','--state-dir',str(bridge))})
        if engine=='crush':
            path=directory/'data/crush.db';path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE messages(id,session_id,role,parts,created_at)')
                for i,(role,text) in enumerate([('user','question'),('assistant','answer')]):
                    db.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(str(i),'native-original',role,
                        json.dumps([{'type':'text','data':{'text':text}}]),i))
        else:
            with sqlite3.connect(directory/'sessions.sqlite') as db:
                db.executescript('CREATE TABLE message(id,session_id,data,time_created,sequence); CREATE TABLE part(id,session_id,message_id,data,time_created,sequence);')
                for i,(role,text) in enumerate([('user','question'),('assistant','answer')]):
                    db.execute('INSERT INTO message VALUES (?,?,?,?,?)',(str(i),'native-original',json.dumps({'role':role}),1000+i,i))
                    db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)',(str(i),'native-original',str(i),json.dumps({'type':'text','text':text}),1000+i,i))
    return spec,binding,ident


def forbid_external_calls(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('local history must not launch processes, models or network requests')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',forbidden)
    monkeypatch.setattr(asyncio,'create_subprocess_shell',forbidden)
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)


@pytest.mark.parametrize('engine',ENGINES)
async def test_explicit_local_hook_dispatch_reads_synthetic_native_files_only(tmp_path,engine,monkeypatch):
    spec,binding,ident=synthetic_source(tmp_path,engine)
    adapter=importlib.import_module('drivers.skill_execution.'+engine)
    driver=SimpleNamespace(agent_spec=spec,_assert_binding=lambda _:None,_runtime_adapter=lambda:adapter,
        load_native_history=AsyncMock(side_effect=AssertionError('general history dispatch is forbidden')))
    before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    forbid_external_calls(monkeypatch)
    first=await AcpDriver.load_local_native_history(driver,binding,ident)
    second=await AcpDriver.load_local_native_history(driver,binding,ident)
    assert first==second and first.native_session_id==ident
    assert [entry.text for entry in first.entries]==['question','answer']
    assert len({entry.entry_id for entry in first.entries})==2
    assert before=={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    driver.load_native_history.assert_not_called()
    if engine in {'crush','zcode'}:
        assert not first.complete and 'attachments' in first.missing
        repo=SimpleNamespace(get_native_storage=AsyncMock(return_value={'fromSequence':0,'mode':'kaus','status':'pending_verification'}),
            list_after=AsyncMock(return_value=()),save_native_storage=AsyncMock(),
            replace_with_native_references=AsyncMock(side_effect=AssertionError('incomplete history must remain owned')))
        storage=NativeTextStorage(repo)
        storage.loader=AsyncMock(return_value=first)
        assert await storage.compact('synthetic-conversation',ident)==0
        repo.replace_with_native_references.assert_not_called()
        assert repo.save_native_storage.call_args.args[1]['status']=='fallback'
    else:
        assert first.complete and not first.missing


@pytest.mark.parametrize('engine',['antigravity','cursor','deepseek','dsh'])
async def test_adapters_without_verified_local_reader_do_not_fall_back_to_service(engine):
    adapter=importlib.import_module('drivers.skill_execution.'+engine)
    assert not hasattr(adapter,'load_local_native_history')
    driver=SimpleNamespace(agent_spec=object(),_assert_binding=lambda _:None,_runtime_adapter=lambda:adapter,
        load_native_history=AsyncMock(side_effect=AssertionError('no remote fallback')))
    with pytest.raises(UnsupportedCapabilityError,match='local_native_history_unsupported'):
        await AcpDriver.load_local_native_history(driver,object(),'synthetic-session')
    driver.load_native_history.assert_not_called()
