import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import crush, zcode
from drivers.skill_execution.bridge_terminal import record, remember, run_terminal
from drivers.skill_execution.terminal_settings import immutable


def setup(tmp_path, engine):
    work = tmp_path / 'work'
    work.mkdir()
    spec = AcpAgentSpec(command=('bridge', '--state-dir', str(tmp_path/'sessions')), cwd=str(work),
        inherit_env=False, env={'HOME':str(tmp_path/'home'), 'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')})
    binding=AgentBinding.create(project='synthetic',backend=engine).model_copy(update={'runtime_config':{'workspace_root':str(work)}})
    conv=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='Test').model_copy(update={'native_session_id':str(uuid.uuid4())})
    directory=tmp_path/'sessions'/conv.native_session_id;directory.mkdir(parents=True)
    (directory/'session.json').write_text(json.dumps({'cwd':str(work),'native_id':'native-original'}))
    return spec,binding,conv,directory


@pytest.mark.parametrize('adapter', [crush,zcode])
def test_current_mcp_is_private_latest_and_empty_clears(adapter,tmp_path):
    spec,binding,conv,directory=setup(tmp_path,adapter.__name__.rsplit('.',1)[-1])
    old={'mcpServers':[{'name':'a','command':'tool','env':[{'name':'TOKEN','value':'private-fixture'}]}]}
    adapter.remember_session_options(spec,conv,old)
    selected=record(spec,conv,binding,'unused')
    assert selected['nativeId']=='native-original' and selected['mcpServers']==old['mcpServers']
    frozen=immutable(spec,conv,json.dumps(selected),suffix='.json')
    adapter.remember_session_options(spec,conv,{})
    assert record(spec,conv,binding,'unused')['mcpServers']==[]
    assert 'private-fixture' in frozen.read_text()
    assert frozen.stat().st_mode & 0o777 == 0o600
    assert (directory/'session.json').read_text()==json.dumps({'cwd':spec.cwd,'native_id':'native-original'})


def test_record_refuses_wrong_workspace_and_symlink(tmp_path):
    spec,binding,conv,directory=setup(tmp_path,'crush');remember(spec,conv,{})
    bad=binding.model_copy(update={'runtime_config':{'workspace_root':str(tmp_path)}})
    with pytest.raises(UnsupportedCapabilityError):record(spec,conv,bad,'crush-bridge')
    source=directory/'session.json';source.rename(directory/'original.json');source.symlink_to(directory/'original.json')
    with pytest.raises(UnsupportedCapabilityError):record(spec,conv,binding,'crush-bridge')


def test_crush_terminal_retains_scope_but_rejects_false_plan_and_host_auto(tmp_path):
    spec,binding,conv,directory=setup(tmp_path,'crush');crush.remember_session_options(spec,conv,{})
    plan=SimpleNamespace(root=tmp_path,spec=spec.with_env(KAUS_CRUSH_SKILL_SCOPE=str(tmp_path/'selected.json')))
    argv=crush.external_cli_command(plan,('/native/crush','--session',conv.native_session_id),conversation=conv,binding=binding,spec=spec)
    text=Path(argv[0]).read_text();assert 'KAUS_CRUSH_SKILL_SCOPE' in text
    value=json.loads(Path(argv[-1]).read_text());assert value['nativeId']=='native-original'
    for change in ({'execution_mode':'plan'},{'approval_mode':'host:auto'}):
        with pytest.raises(UnsupportedCapabilityError):crush.external_cli_command(plan,('/native/crush',),conversation=conv.model_copy(update=change),binding=binding,spec=spec)


def test_zcode_terminal_mcp_guard_and_unicode_paths(tmp_path):
    if not shutil.which('node'):pytest.skip('Node unavailable')
    root=tmp_path/'中文 路径';root.mkdir();spec,binding,conv,_=setup(root,'zcode')
    module=root/'zcode.cjs';module.write_text("const TUI_TITLE_GENERATION_CONFIG={}; function protocolMcpServersToRuntimeMcpConfig(x){return x}; const value={titleGeneration: TUI_TITLE_GENERATION_CONFIG,};console.log(JSON.stringify(value));")
    zcode.remember_session_options(spec,conv,{'mcpServers':[{'name':'chosen','command':'tool','env':[{'name':'TOKEN','value':'secret-fixture'}]}]})
    argv=zcode.external_cli_command(None,(str(module),),conversation=conv,binding=binding,spec=spec)
    payload=json.loads(Path(argv[-1]).read_text());assert payload['terminalMcpServers'][0]['isolation']=='session'
    assert 'secret-fixture' not in ' '.join(argv) and 'secret-fixture' not in Path(argv[0]).read_text()
    guard=next((root/'state/terminal-settings').rglob('*.cjs'))
    env={**os.environ,'KAUS_ZCODE_TERMINAL_SCOPE':argv[-1],'NODE_OPTIONS':'--require='+json.dumps(str(guard),ensure_ascii=False)}
    result=subprocess.run(['node',str(module)],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['mcp'][0]['name']=='chosen'
    original=module.read_text();module.write_text(original+'\n// upgrade')
    failed=subprocess.run(['node',str(module)],env=env,capture_output=True,text=True)
    assert failed.returncode!=0 and '重新验证' in failed.stderr


async def test_terminal_cancellation_waits_for_child(tmp_path):
    pid=tmp_path/'pid'
    task=asyncio.create_task(run_terminal((shutil.which('python3'),'-c',
        'import os,time,pathlib;pathlib.Path('+repr(str(pid))+').write_text(str(os.getpid()));time.sleep(60)'),cwd=str(tmp_path)))
    async with asyncio.timeout(5):
        while not pid.exists() or not pid.read_text():
            if task.done():await task
            await asyncio.sleep(.02)
    child=int(pid.read_text());task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with pytest.raises(ProcessLookupError):os.kill(child,0)


@pytest.mark.parametrize('engine',['crush','zcode'])
async def test_history_reads_original_id_public_text_only_stably(tmp_path,engine):
    spec,binding,conv,directory=setup(tmp_path,engine)
    if engine=='crush':
        (directory/'data').mkdir();db=sqlite3.connect(directory/'data/crush.db')
        db.execute('CREATE TABLE messages(id,session_id,role,parts,created_at)')
        parts=[{'type':'text','data':{'text':'visible'}},{'type':'reasoning','data':{'thinking':'private-thought'}},{'type':'text','data':{'text':'hidden-text','hidden':True}}]
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('m1','native-original','assistant',json.dumps(parts),1))
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('other','wrong-session','user',json.dumps(parts),1))
        adapter=crush
    else:
        db=sqlite3.connect(directory/'sessions.sqlite');db.execute('CREATE TABLE message(id,session_id,data,time_created,sequence)');db.execute('CREATE TABLE part(id,session_id,message_id,data,time_created,sequence)')
        db.execute('INSERT INTO message VALUES(?,?,?,?,?)',('m1','native-original','{"role":"assistant"}',1000,0))
        for index,part in enumerate([{'type':'text','text':'visible'},{'type':'reasoning','text':'private-thought'},{'type':'text','text':'hidden-text','synthetic':True}]):
            db.execute('INSERT INTO part VALUES(?,?,?,?,?,?)',(str(index),'native-original','m1',json.dumps(part),1000,index))
        adapter=zcode
    db.commit();db.close()
    first=await adapter.load_native_history(spec,binding,conv.native_session_id)
    second=await adapter.load_native_history(spec,binding,conv.native_session_id)
    assert first==second and len(first.entries)==1
    assert first.entries[0].entry_id=='m1' and first.entries[0].text=='visible'
    assert not first.complete and 'attachments' in first.missing


def test_zcode_missing_native_tui_fails_before_launch(tmp_path):
    spec,binding,conv,_=setup(tmp_path,'zcode');module=tmp_path/'zcode.cjs'
    module.write_text('const x={titleGeneration: TUI_TITLE_GENERATION_CONFIG,};import("@zcode/tui");')
    zcode.remember_session_options(spec,conv,{})
    with pytest.raises(UnsupportedCapabilityError,match='TUI'):
        zcode.external_cli_command(None,(str(module),),conversation=conv,binding=binding,spec=spec)


async def test_zcode_native_model_change_includes_registry_default_reasoning(tmp_path):
    from unittest.mock import AsyncMock
    from drivers.zcode_bridge.session import Session
    session=Session(SimpleNamespace(), 'id', tmp_path, str(tmp_path))
    session.native=SimpleNamespace(call=AsyncMock(return_value={}))
    session.snapshot={'settings':{'model':{'current':{'providerId':'p','modelId':'old'},'available':[{
        'ref':{'providerId':'p','modelId':'new'},'reasoning':{'defaultLevel':'medium','levels':[{'value':'medium'},{'value':'high'}]}}]}}}
    await session.configure('model','["p","new"]')
    method, params=session.native.call.call_args.args
    assert method=='session/setModel'
    assert params['model']=={'providerId':'p','modelId':'new','options':{'reasoningLevel':'medium'}}
    assert params['persistAsWorkspaceLastUsed'] is False
