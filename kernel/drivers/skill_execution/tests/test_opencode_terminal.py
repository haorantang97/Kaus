import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from drivers.skill_execution.opencode_family_terminal import CommandGate, environment, prepare, command, remember
from drivers.skill_execution import opencode, kilo
from drivers.base import UnsupportedCapabilityError
from app.conversations.models import Conversation
from test_selection import setup, effective


async def test_command_gate_checks_before_native_request_and_keeps_non_skill_commands():
    sent=[]
    def upstream(request):
        sent.append((request.method,request.url.path))
        if request.url.path=='/command':
            return httpx.Response(200,json=[{'name':'chosen','source':'skill'}, {'name':'blocked','source':'skill'}, {'name':'ordinary','source':'command'}])
        return httpx.Response(200,json={'ok':True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),base_url='http://native') as native:
        gate=CommandGate(native,['chosen'],'ses_fixture')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate.app()),base_url='http://gate') as client:
            menu=(await client.get('/command')).json()
            assert [r['name'] for r in menu]==['chosen','ordinary']
            for path,body in [('/session/ses_fixture/command',{'command':'blocked','arguments':''}),('/api/session/ses_fixture/prompt',{'prompt':'/blocked'}),('/session/ses_fixture/prompt_async',{'parts':[{'type':'text','text':'/blocked'}]})]:
                assert (await client.post(path,json=body)).status_code==403
            assert not any(m=='POST' for m,p in sent)
            for name in ['chosen','ordinary']:
                assert (await client.post('/session/ses_fixture/command',json={'command':name})).status_code==200


async def test_dynamic_unknown_skill_inventory_and_empty_selection():
    native=SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200,json=[],request=httpx.Request('GET','http://n/command'))))
    inventory=AsyncMock(return_value=([{'name':'late'}],{}))
    gate=CommandGate(native,[],'ses_fixture',inventory)
    with pytest.raises(PermissionError):await gate.check('/session/ses_fixture/message',{'parts':[{'type':'text','text':'/late'}]})
    with pytest.raises(PermissionError):await gate.check('/session/ses_fixture/command',{'command':'late:skill'})
    await gate.check('/session/ses_fixture/message',{'parts':[{'type':'text','text':'ordinary task'}]})
    assert inventory.await_count==1


async def test_gate_keeps_bound_workspace_and_cannot_restart_old_mcp():
    seen=[]
    def upstream(request):
        seen.append(request)
        return httpx.Response(200,json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),base_url='http://native',params={'directory':'/selected'}) as native:
        gate=CommandGate(native,[],'ses_fixture')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate.app()),base_url='http://gate') as client:
            assert (await client.get('/session/ses_fixture?directory=/other&limit=5',headers={'x-opencode-directory':'/other'})).status_code==200
            assert seen[-1].url.params['directory']=='/selected' and seen[-1].url.params['limit']=='5'
            assert 'x-opencode-directory' not in seen[-1].headers
            for path in ['/mcp', '/mcp/old/connect', '/experimental/control-plane/move-session']:
                assert (await client.post(path,json={})).status_code==403


def test_environment_preserves_provider_auth_but_reapplies_scope():
    data={'engine':'opencode','scopeOverlay':{'skills':{'paths':['chosen']},'permission':{'skill':{'*':'deny','chosen':'allow'}}},'scopePlugins':['scope.mjs'],'model':'p/m','agent':'plan'}
    env=environment(data,{'OPENCODE_CONFIG_CONTENT':json.dumps({'provider':{'p':{'apiKey':'secret'}},'plugin':['user.mjs'],'permission':{'edit':'ask'}}),'HOME':'/native'})
    config=json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert config['provider']['p']['apiKey']=='secret'
    assert config['permission']['edit']=='ask' and config['permission']['skill']['*']=='deny'
    assert config['plugin']==['user.mjs','scope.mjs'] and env['HOME']=='/native'


@pytest.mark.parametrize('adapter',[opencode,kilo])
def test_launch_contains_native_identity_current_options_and_no_credentials_in_argv(tmp_path,adapter):
    engine=adapter.__name__.rsplit('.',1)[-1]
    spec,binding=setup(tmp_path,engine)
    spec=spec.model_copy(update={'cwd':str(tmp_path)})
    conv=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture').model_copy(update={'native_session_id':'ses_fixture','model_id':'p/m','reasoning_mode':'low','execution_mode':'plan'})
    remember(spec,conv,{'mcpServers':[{'name':'fixture','command':'tool','env':[{'name':'TOKEN','value':'synthetic-secret-token-123'}]}]})
    result=adapter.external_cli_command(None,('/native','-s','ses_fixture'),conversation=conv,binding=binding,spec=spec)
    assert 'synthetic-secret-token-123' not in ' '.join(result)
    payload=json.loads(open(result[-1]).read())
    assert payload['sessionId']=='ses_fixture' and payload['model']=='p/m' and payload['variant']=='low' and payload['agent']=='plan'
    assert payload['mcpServers'][0]['env'][0]['value']=='synthetic-secret-token-123'
    remember(spec,conv,{})
    result=adapter.external_cli_command(None,('/native','-s','ses_fixture'),conversation=conv,binding=binding,spec=spec)
    assert json.loads(open(result[-1]).read())['mcpServers']==[]


async def test_prepare_rejects_identity_and_failed_mcp_before_attach(tmp_path):
    requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path.startswith('/session/'):
            return httpx.Response(200,json={'id':'ses_fixture','directory':str(tmp_path)})
        if request.url.path=='/mcp':return httpx.Response(200,json={'tool':{'status':'failed'}})
        return httpx.Response(204)
    data={'sessionId':'ses_fixture','cwd':str(tmp_path),'model':'p/m','variant':'low','agent':'plan','approval':'ask','allowedSkills':['chosen'],'mcpServers':[{'name':'tool','command':'fake'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://native') as client:
        with pytest.raises(ValueError,match='MCP'):await prepare(client,data)
    model=next(json.loads(r.content) for r in requests if r.url.path.endswith('/model'))
    assert model['model']=={'providerID':'p','id':'m','variant':'low'}
    patch=next(json.loads(r.content) for r in requests if r.method=='PATCH')
    assert patch['permission'][-1]=={'permission':'skill','pattern':'chosen','action':'allow'}


async def test_default_effort_clears_previous_native_variant(tmp_path):
    requests=[]
    def handle(request):
        requests.append(request)
        if request.method=='GET':return httpx.Response(200,json={'id':'ses_fixture','directory':str(tmp_path),'model':{'providerID':'p','id':'m','variant':'high'}})
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://native') as client:
        await prepare(client,{'sessionId':'ses_fixture','cwd':str(tmp_path),'variant':'default','mcpServers':[]})
    model=next(json.loads(r.content) for r in requests if r.url.path.endswith('/model'))
    assert model['model']=={'providerID':'p','id':'m'}


async def test_proxy_conversation_scope_and_composer_restore_do_not_rewrite_native_history(tmp_path):
    original=[{'info':{'id':'u','role':'user','model':{'providerID':'old','modelID':'old'},'agent':'old'},'parts':[{'type':'text','text':'unchanged user'}]},
              {'info':{'id':'a','role':'assistant','modelID':'old'},'parts':[{'type':'text','text':'unchanged assistant'}]}]
    def handle(request):
        if request.url.path.endswith('/message'):return httpx.Response(200,json=original)
        if request.url.path=='/session/ses_child':return httpx.Response(200,json={'parentID':'ses_fixture'})
        if request.url.path=='/session/ses_other':return httpx.Response(200,json={'parentID':None})
        return httpx.Response(200,json={'id':'ses_fixture'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://native') as native:
        gate=CommandGate(native,[],'ses_fixture',selections={'model':'new/model','agent':'plan','variant':'low'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate.app()),base_url='http://gate') as client:
            assert (await client.get('/session/ses_other/message')).status_code==403
            assert (await client.get('/session/ses_child')).status_code==200
            projected=(await client.get('/session/ses_fixture/message')).json()
            assert projected[0]['info']['model']['modelID']=='model'
            assert projected[0]['parts']==original[0]['parts'] and projected[1]==original[1]
            assert (await client.get('/session/ses_fixture/message')).json()==original
            assert (await client.patch('/global/config',json={})).status_code==403
    assert original[0]['info']['model']['modelID']=='old'


def test_mcp_removal_disables_every_owned_name_before_native_start(tmp_path):
    spec,binding=setup(tmp_path,'opencode')
    conv=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture').model_copy(update={'native_session_id':'ses_fixture'})
    remember(spec,conv,{'mcpServers':[{'name':'old','command':'old-command'}]})
    remember(spec,conv,{'mcpServers':[{'name':'new','command':'new-command'}]})
    remember(spec,conv,{})
    result=opencode.external_cli_command(None,('/native','-s','ses_fixture'),conversation=conv,binding=binding,spec=spec)
    data=json.loads(open(result[-1]).read())
    assert data['ownedMcpNames']==['new','old'] and data['mcpServers']==[]
    env=environment(data,{'OPENCODE_CONFIG_CONTENT':json.dumps({'mcp':{'old':{'type':'local','command':['old-command'],'enabled':True},'personal':{'enabled':True}}})})
    mcp=json.loads(env['OPENCODE_CONFIG_CONTENT'])['mcp']
    assert mcp['old']['enabled'] is False and mcp['new']['enabled'] is False and mcp['personal']['enabled'] is True
    assert data['scopePlugins']


def test_read_native_sqlite_keeps_original_bytes_and_checks_workspace(tmp_path):
    import sqlite3
    from drivers.skill_execution.opencode_history import read
    spec,binding=setup(tmp_path,'opencode');spec=spec.with_env(HOME=str(tmp_path))
    root=tmp_path/'.local/share/opencode';root.mkdir(parents=True);db=root/'opencode.db'
    conn=sqlite3.connect(db)
    conn.executescript('CREATE TABLE session(id TEXT,directory TEXT);CREATE TABLE message(id TEXT,session_id TEXT,time_created INT,data TEXT);CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,time_created INT,data TEXT);')
    conn.execute('INSERT INTO session VALUES(?,?)',('ses_fixture',str(tmp_path)))
    conn.execute('INSERT INTO message VALUES(?,?,?,?)',('u','ses_fixture',1700000000000,json.dumps({'role':'user'})))
    conn.execute('INSERT INTO part VALUES(?,?,?,?,?)',('p','u','ses_fixture',1700000000000,json.dumps({'type':'text','text':'public task'})))
    conn.commit();conn.close();before=db.read_bytes()
    history=read(spec,binding,'ses_fixture',engine='opencode')
    assert history.entries[0].text=='public task' and history.entries[0].entry_id=='ses_fixture:u'
    assert db.read_bytes()==before
    binding=binding.model_copy(update={'runtime_config':{'workspace_root':str(tmp_path/'other')}})
    with pytest.raises(RuntimeError):read(spec,binding,'ses_fixture',engine='opencode')
