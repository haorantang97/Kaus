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
    # 「撤销」 puts the same archive back into the project.
    assert client.post(f'/api/native-archives/{identity}/link',json={'projectId':project.id}).json()=={'linked':True}
    assert [item['id'] for item in client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items']]==[identity]
    assert client.post('/api/native-archives/add',json=request).status_code==200
    assert len(client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items'])==1


class _Storage:
    async def begin(self, conversation_id, native_id): self.begun=(conversation_id,native_id)


class _Host:
    """Records what the importer writes; no runtime, no model calls."""
    def __init__(self):
        from types import SimpleNamespace
        self.event_store=SimpleNamespace(native_storage=_Storage());self.events=[]
    async def normalize_native_history(self, conversation_id, history): return history
    async def emit_conversation_event(self, conversation, event): self.events.append((conversation.id,event))
    async def compact_native_text(self, conversation): return 0


class _Driver:
    driver_kind='native'
    def __init__(self): self.reads=[]
    async def load_native_history(self, binding, native_id):
        from drivers.base import NativeHistory, NativeHistoryEntry
        self.reads.append(native_id)
        return NativeHistory(native_session_id=native_id,entries=(
            NativeHistoryEntry(entry_id='e1',role='user',text='question'),
            NativeHistoryEntry(entry_id='e2',role='assistant',text='answer')))


class _Registry:
    def __init__(self, driver): self.driver=driver
    def try_get(self, backend_id): return self.driver if backend_id=='backend:codex' else None
    def get(self, backend_id): return self.driver


def importing_fixture(tmp_path, connected=True):
    from app.conversations.native_import import NativeConversationImporter
    repos,project,other,file,store,service,client=fixture(tmp_path)
    driver=_Driver();host=_Host()
    if connected: asyncio.run(repos.backends.save(Backend.create(key='codex',display_name='Codex',driver_kind='native')))
    service.importer=NativeConversationImporter(repos,host,_Registry(driver))
    return repos,project,store,client,host,driver


def test_add_imports_a_connected_engine_conversation_as_an_ordinary_conversation(tmp_path):
    repos,project,store,client,host,driver=importing_fixture(tmp_path)
    source=client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions'][0]
    result=client.post('/api/native-archives/add',json={'sourceIds':[source['sourceId']],'projectId':project.id}).json()
    assert result['archiveIds']==[] and result['count']==1 and len(result['conversationIds'])==1
    conversation=asyncio.run(repos.conversations.get(result['conversationIds'][0]))
    binding=asyncio.run(repos.bindings.get(conversation.agent_binding_id))
    # Bound to the original native session in this project, titled like the native one.
    assert conversation.native_session_id=='session-native' and binding.backend_id=='backend:codex' and binding.project_id==project.id
    # No title index: the first question stands in for the file name.
    assert conversation.title=='question'
    (owner,event),=host.events
    assert owner==conversation.id and event.name=='history.reconciled'
    assert event.data['imported'] is True and event.data['lastEntryId']=='e2' and event.data['nativeSessionId']=='session-native'
    assert [entry['text'] for entry in event.data['entries']]==['question','answer']
    assert host.event_store.native_storage.begun==(conversation.id,'session-native')
    # No read-only record is left, and adding again does not duplicate.
    assert client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items']==[]
    assert client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions'][0]['added']
    again=client.post('/api/native-archives/add',json={'sourceIds':[source['sourceId']],'projectId':project.id}).json()
    assert again=={'archiveIds':[],'count':0}
    assert len(asyncio.run(repos.conversations.list_for_project(project.id)))==1


def test_unconnected_engine_keeps_the_read_only_record_and_adopt_reports_it(tmp_path):
    repos,project,store,client,host,driver=importing_fixture(tmp_path,connected=False)
    source=client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions'][0]
    result=client.post('/api/native-archives/add',json={'sourceIds':[source['sourceId']],'projectId':project.id}).json()
    assert result['conversationIds']==[] and len(result['archiveIds'])==1
    identity=client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items'][0]['id']
    refused=client.post(f'/api/native-archives/{identity}/adopt',json={'projectId':project.id})
    assert refused.status_code==409 and refused.json()['error']['code']=='archive_adopt_unavailable'
    assert asyncio.run(repos.conversations.list_for_project(project.id))==()


def test_adopt_turns_an_earlier_record_into_a_conversation_once(tmp_path):
    repos,project,store,client,host,driver=importing_fixture(tmp_path,connected=False)
    source=client.get('/api/native-archives/candidates',params={'source':'codex','projectId':project.id}).json()['sessions'][0]
    client.post('/api/native-archives/add',json={'sourceIds':[source['sourceId']],'projectId':project.id})
    identity=client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items'][0]['id']
    asyncio.run(repos.backends.save(Backend.create(key='codex',display_name='Codex',driver_kind='native')))
    first=client.post(f'/api/native-archives/{identity}/adopt',json={'projectId':project.id}).json()
    second=client.post(f'/api/native-archives/{identity}/adopt',json={'projectId':project.id}).json()
    assert first==second and first['conversationId']
    assert len(host.events)==1 and driver.reads==['session-native']
    assert client.get('/api/native-archives/project-items',params={'projectId':project.id}).json()['items']==[]
