import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app.conversations.models import Conversation
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import gemini, gemini_terminal
from test_selection import setup


CORE = '''
export const MCPServerStatus={CONNECTED:'connected'};
export class Config {
  constructor(){this.servers={ambient:{command:'ambient'},same:{command:'old'}};}
  getSessionId(){return 'native-id';}
  getModel(){return process.argv[process.argv.indexOf('--model')+1];}
  getApprovalMode() {const mode=process.argv[process.argv.indexOf('--approval-mode')+1];return mode==='auto_edit'?'autoEdit':mode;}
  getMcpServers(){return this.servers;}
  setMcpServers(mcpServers) {this.servers=mcpServers;}
  async initialize() {this.started=(this.started||0)+1;}
  async waitForMcpInit() {}
  getMcpClientManager() {return {discoveryPromise:Promise.resolve(),getMcpServers:()=>this.servers,
    clients:new Map(Object.entries(this.servers).map(([name,value])=>[name,{getServerName:()=>name,getStatus:()=>value.command==='bad'?'error':'connected'}])),stop:async()=>{}};}
}
'''


def fixture(tmp_path, monkeypatch, **updates):
    spec, binding = setup(tmp_path, 'gemini')
    conv = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture').evolve(
        native_session_id='native-id', model_id='synthetic-model', approval_mode='ask', **updates)
    package = tmp_path / 'package';package.mkdir();(package/'package.json').write_text('{"type":"module"}')
    core = package/'core.mjs';core.write_text(CORE)
    entry = package/'native.mjs';entry.write_text("import {Config} from './core.mjs';const c=new Config();await Promise.all([c.initialize(),c.initialize()]);console.log(JSON.stringify({servers:c.getMcpServers(),started:c.started,mode:c.getApprovalMode()}));")
    node = shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    monkeypatch.setattr(gemini_terminal, 'locate', lambda *args:package)
    monkeypatch.setattr(gemini, 'loader_module', lambda *args:core)
    return spec,binding,conv,(node,str(entry)),core


async def run(command):
    child=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(child.communicate(),10)
    return child.returncode,out.decode(),err.decode()


async def test_terminal_replaces_same_server_preserves_native_and_clears_previous_projection(tmp_path,monkeypatch):
    spec,binding,conv,command,core=fixture(tmp_path,monkeypatch)
    secret='synthetic-secret'
    gemini.remember_session_options(spec,conv,{'mcpServers':[{'name':'same','command':'new','args':['with spaces'],'env':[{'name':'TOKEN','value':secret}]}]})
    first=gemini.external_cli_command(None,command,conversation=conv,binding=binding,spec=spec)
    assert secret not in ' '.join(first)+Path(first[0]).read_text()
    code,out,err=await run(first);assert code==0,err
    result=json.loads(out);assert result['started']==1 and result['servers']['ambient']['command']=='ambient'
    assert result['servers']['same']['command']=='new' and result['servers']['same']['env']=={'TOKEN':secret}
    conv=conv.evolve(approval_mode='auto')
    gemini.remember_session_options(spec,conv,{})
    second=gemini.external_cli_command(None,command,conversation=conv,binding=binding,spec=spec)
    code,out,err=await run(second);assert code==0,err
    assert json.loads(out)['servers']['same']['command']=='old' and json.loads(out)['mode']=='autoEdit'
    code,out,err=await run(first);assert code==0 and json.loads(out)['servers']['same']['command']=='new'


async def test_terminal_rejects_bad_mcp_and_changed_native_module(tmp_path,monkeypatch):
    spec,binding,conv,command,core=fixture(tmp_path,monkeypatch)
    gemini.remember_session_options(spec,conv,{'mcpServers':[{'name':'broken','command':'bad'}]})
    launch=gemini.external_cli_command(None,command,conversation=conv,binding=binding,spec=spec)
    code,out,err=await run(launch);assert code!=0 and 'MCP 连接失败：broken' in err
    core.write_text(CORE+'\n// changed after launch preparation')
    code,out,err=await run(launch);assert code!=0 and '重新验证' in err


def test_skill_plan_guard_is_carried_and_unsupported_permissions_are_not_widened(tmp_path,monkeypatch):
    spec,binding,conv,command,core=fixture(tmp_path,monkeypatch)
    gemini.remember_session_options(spec,conv,{})
    plan=SimpleNamespace(spec=spec.with_env(NODE_OPTIONS='--import=file:///skill-guard.mjs',KAUS_GEMINI_SKILL_SCOPE='/synthetic/scope'))
    launch=gemini.external_cli_command(plan,command,conversation=conv,binding=binding,spec=spec)
    script=Path(launch[0]).read_text();assert '--import=file:///skill-guard.mjs' in script and 'KAUS_GEMINI_SKILL_SCOPE=/synthetic/scope' in script
    for mode in ('host:auto','read_only'):
        with pytest.raises(UnsupportedCapabilityError):
            gemini.external_cli_command(plan,command,conversation=conv.evolve(approval_mode=mode),binding=binding,spec=spec)
    with pytest.raises(UnsupportedCapabilityError,match='推理档位'):
        gemini.external_cli_command(plan,command,conversation=conv.evolve(reasoning_mode='high'),binding=binding,spec=spec)


async def test_exact_empty_session_is_resumable_without_exposing_other_empty_or_subagent_sessions(tmp_path,monkeypatch):
    from uuid import uuid4
    spec,binding,conv,command,core=fixture(tmp_path,monkeypatch)
    identity=str(uuid4());conv=conv.evolve(native_session_id=identity)
    core.write_text(CORE.replace("'native-id'",repr(identity)))
    package=core.parent;bundle=package/'bundle';bundle.mkdir()
    (bundle/'gemini.js').write_text("await import('./gemini-test.js');")
    (bundle/'gemini-test.js').write_text("import {SessionSelector} from './selector.js';")
    selector=bundle/'selector.js';selector.write_text('''
export class SessionSelector {}
export function visible(content){if (!content.hasResumableContent) {return false;} return true;}
''')
    entry=Path(command[1])
    entry.write_text(entry.read_text()+f'''
const {{visible}}=await import('./bundle/selector.js');
console.log(JSON.stringify([
 {{sessionId:{json.dumps(identity)},kind:'main',messageCount:1,hasResumableContent:false}},
 {{sessionId:'another-session',kind:'main',messageCount:0,hasResumableContent:false}},
 {{sessionId:{json.dumps(identity)},kind:'subagent',messageCount:0,hasResumableContent:false}},
 {{sessionId:'another-session',kind:'main',hasResumableContent:true}}
].map(visible)));
''')
    gemini.remember_session_options(spec,conv,{})
    launch=gemini.external_cli_command(None,command,conversation=conv,binding=binding,spec=spec)
    code,out,err=await run(launch);assert code==0,err
    assert json.loads(out.splitlines()[-1])==[True,False,False,True]
    selector.write_text(selector.read_text()+'\n// upgrade')
    code,out,err=await run(launch)
    assert code!=0 and '空会话恢复接口不兼容' in err
