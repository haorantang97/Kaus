"""Project-scoped native history addition, using synthetic files only."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'kernel'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.native_archives import build_native_archive_router
from app.api.session_auth import SessionAuthPolicy
from app.persistence.memory import in_memory_repository_set
from app.projects.models import Project, Backend, AgentBinding
from app.conversations.models import Conversation
from native_archives import NativeArchiveService, NativeArchiveStore
from test_native_archives import codex, jsonl


def fixture(tmp_path):
    repos=in_memory_repository_set()
    project=Project.create(slug='work',display_name='Work')
    other=Project.create(slug='other',display_name='Other')
    asyncio.run(repos.projects.save(project));asyncio.run(repos.projects.save(other))
    root=tmp_path/'native'
    file=jsonl(root/'session-native.jsonl',codex())
    store=NativeArchiveStore(tmp_path/'archive')
    service=NativeArchiveService(store,repos,[{'id':'codex','label':'Codex','path':str(root)}, {'id':'claude-code','label':'Claude Code','path':str(tmp_path/'absent')}])
    app=FastAPI();app.include_router(build_native_archive_router(service,auth_policy=SessionAuthPolicy(lambda:'fixture-token')))
    client=TestClient(app,base_url='http://127.0.0.1:8877',headers={'Authorization':'Bearer fixture-token'})
    return repos,project,other,file,store,service,client


def test_one_action_add_stays_reference_and_appears_in_project_without_mutating_native(tmp_path):
    repos,project,other,file,store,service,client=fixture(tmp_path)
    original=file.read_bytes()
    assert client.get('/api/native-archives/choices').json()=={'sources':[{'id':'codex','label':'Codex'}]}
    params={'source':'codex','projectId':project.id}
    candidates=client.get('/api/native-archives/candidates',params=params).json()['sessions']
    assert len(candidates)==1 and not candidates[0]['added']
    assert str(tmp_path) not in json.dumps(candidates)
    result=client.post('/api/native-archives/add',json={'sourceIds':[candidates[0]['sourceId']],'projectId':project.id})
    assert result.status_code==200,result.text
    again=client.post('/api/native-archives/add',json={'sourceIds':[candidates[0]['sourceId']],'projectId':project.id})
    assert again.json()==result.json()
    page=client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()
    assert len(page['items'])==1 and page['items'][0]['storage']=='reference'
    assert str(tmp_path) not in json.dumps(page)
    assert client.get('/api/native-archives/project-items',params={'projectId':other.id}).json()['items']==[]
    identity=page['items'][0]['id']
    assert [m['text'] for m in client.get('/api/native-archives/'+identity,params={'projectId':project.id}).json()['messages']]==['question','answer']
    assert client.get('/api/native-archives/'+identity,params={'projectId':other.id}).status_code==404
    assert client.get('/api/native-archives/candidates',params=params).json()['sessions'][0]['added']
    assert file.read_bytes()==original
    assert not [p for p in store.objects.rglob('*') if p.is_file()]
    assert asyncio.run(repos.conversations.list_for_project(project.id))==()
    file.unlink()
    assert client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items']==[]


def test_already_managed_native_identity_is_not_readded_even_from_other_project(tmp_path):
    repos,project,other,file,store,service,client=fixture(tmp_path)
    async def create():
        backend=Backend.create(key='codex',display_name='Codex',driver_kind='mock')
        await repos.backends.save(backend)
        binding=AgentBinding.create(project=other,backend='codex');await repos.bindings.save(binding)
        await repos.conversations.save(Conversation.create(project_id=other.id,agent_binding_id=binding.id,title='Managed',native_session_id='session-native'))
    asyncio.run(create())
    candidates=client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions']
    assert candidates[0]['added']
    result=client.post('/api/native-archives/add',json={'sourceIds':[candidates[0]['sourceId']],'projectId':project.id})
    assert result.json()=={'archiveIds':[],'count':0}
    assert not store.list()


def test_choices_do_not_scan_and_invalid_project_never_reads_sources(tmp_path,monkeypatch):
    _,project,_,_,_,service,client=fixture(tmp_path)
    def unexpected(*args,**kwargs): raise AssertionError('must not scan')
    monkeypatch.setattr(service,'scan',unexpected)
    assert client.get('/api/native-archives/choices').status_code==200
    assert client.get('/api/native-archives/project-items').status_code==200
    assert client.get('/api/native-archives/candidates',params={'source':'codex','projectId':'project:missing'}).status_code==404
    assert client.post('/api/native-archives/add',json={'sourceIds':['unscanned'],'projectId':project.id}).status_code==400


def test_remove_only_unlinks_one_project_and_can_be_added_again(tmp_path):
    _,project,other,file,store,service,client=fixture(tmp_path)
    original=file.read_bytes()
    candidate=client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions'][0]
    request={'sourceIds':[candidate['sourceId']],'projectId':project.id}
    identity=client.post('/api/native-archives/add',json=request).json()['archiveIds'][0]
    client.post('/api/native-archives/add',json={**request,'projectId':other.id})
    result=client.post(f'/api/native-archives/{identity}/unlink',json={'projectId':project.id})
    assert result.status_code==200
    assert client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items']==[]
    assert len(client.get('/api/native-archives/project-items',params={'projectId':other.id}).json()['items'])==1
    assert client.get('/api/native-archives/'+identity,params={'projectId':other.id}).json()['messages']
    assert client.get('/api/native-archives/'+identity,params={'projectId':project.id}).status_code==404
    assert file.read_bytes()==original
    assert client.post('/api/native-archives/add',json=request).status_code==200
    assert len(client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items'])==1
