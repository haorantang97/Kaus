"""Gemini runtime guard tests use an executable synthetic native interface."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import gemini, gemini_runtime


CORE = '''
export const MCPServerStatus={CONNECTED:'connected'};
export class ChatRecordingService {
  constructor(config){this.context={config};}
  async initialize(resumed){this.context.config.writes.push(resumed?'resume':'new');}
}
'''
ACP = '''
import {ChatRecordingService} from './core.js';
const startupProfiler={flush(){}};
function startAutoMemoryIfEnabled(){}
export class Manager {
  async newSessionConfig(id,cwd,servers){
    const config={id,writes:[],stopped:false};
    const clients=new Map(servers.map(s=>[s.name,{getServerName:()=>s.name,getStatus:()=>s.status??'connected'}]));
    config.getMcpClientManager=()=>({clients,discoveryPromise:Promise.resolve(),stop:async()=>{config.stopped=true}});
    config.initialize=async()=>{await new ChatRecordingService(config).initialize();};
    config.getGeminiClient=()=>({startChat:async()=>config});
    this.configs??=[];this.configs.push(config);return config;
  }
  async newSession({cwd,mcpServers},authDetails){
    const config=await this.newSessionConfig('new',cwd,mcpServers);
    await config.initialize();
    startupProfiler.flush(config);
    startAutoMemoryIfEnabled(config);
    const geminiClient = config.getGeminiClient();
    return geminiClient.startChat();
  }
  async loadSession({ sessionId, cwd, mcpServers }, authDetails) {
    const config = await this.initializeSessionConfig(
      sessionId,
      cwd,
      mcpServers,
      authDetails
    );
    if(config.writes.length)throw Error('history overwritten before read');
    await config.initialize();
    if(config.writes.length)throw Error('history overwritten before resume');
    await new ChatRecordingService(config).initialize({history:'original'});
    await new ChatRecordingService(config).initialize();
    return config;
  }
  async initializeSessionConfig(sessionId, cwd, mcpServers, authDetails) {
    const config = await this.newSessionConfig(sessionId, cwd, mcpServers);
    await config.initialize();
    startupProfiler.flush(config);
    startAutoMemoryIfEnabled(config);
    return config;
  }
}
'''
RUN = '''
const {Manager}=await import('./gemini-runtime.js');
const manager=new Manager();
const [a,b,n]=await Promise.all([
 manager.loadSession({sessionId:'a',cwd:'/',mcpServers:[]},{}),
 manager.loadSession({sessionId:'b',cwd:'/',mcpServers:[{name:'valid'}]},{}),
 manager.newSession({cwd:'/',mcpServers:[]},{})
]);
let failures=0;
for(const method of ['newSession','loadSession']){
 try{await manager[method]({sessionId:'fail',cwd:'/',mcpServers:[{name:'broken',status:'disconnected'}]},{});}
 catch(e){if(!e.message.includes('broken'))throw e;failures++;}
}
console.log(JSON.stringify({a:a.writes,b:b.writes,n:n.writes,failures,stopped:manager.configs.slice(-2).every(c=>c.stopped)}));
'''


def fixture(tmp_path, monkeypatch):
    package=tmp_path/'package';bundle=package/'bundle';bundle.mkdir(parents=True)
    (package/'package.json').write_text('{"type":"module"}')
    core=bundle/'core.js';core.write_text(CORE)
    acp=bundle/'gemini-runtime.js';acp.write_text(ACP)
    entry=bundle/'gemini.js';entry.write_text(RUN)
    monkeypatch.setattr(gemini_runtime,'locate',lambda *args:package)
    monkeypatch.setattr(gemini,'loader_module',lambda *args:core)
    node=shutil.which('node')
    if not node:pytest.skip('Node runtime unavailable')
    return AcpAgentSpec(command=(node,str(entry)),inherit_env=False,
        env={'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')}),package,core,acp


async def run(spec):
    process=await asyncio.create_subprocess_exec(*spec.command,env=spec.build_env(),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(process.communicate(),15)
    return process.returncode,out.decode(),err.decode()


async def test_runtime_resumes_without_initial_history_write_and_checks_same_manager_mcp(tmp_path,monkeypatch):
    spec,package,core,acp=fixture(tmp_path,monkeypatch)
    before={p:p.read_bytes() for p in package.rglob('*') if p.is_file()}
    prepared=await gemini.prepare_runtime_spec(spec,None,{})
    again=await gemini.prepare_runtime_spec(prepared,None,{})
    assert again==prepared
    code,out,err=await run(prepared)
    assert code==0,err
    result=json.loads(out)
    assert result=={'a':['resume','new'],'b':['resume','new'],'n':['new'],'failures':2,'stopped':True}
    assert {p:p.read_bytes() for p in before}==before
    assert spec.env=={'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')}


async def test_runtime_rejects_changed_module_after_preparation(tmp_path,monkeypatch):
    spec,package,core,acp=fixture(tmp_path,monkeypatch)
    prepared=await gemini.prepare_runtime_spec(spec,None,{})
    acp.write_text(acp.read_text()+'\n// external update\n')
    code,out,err=await run(prepared)
    assert code!=0 and '重新验证' in err


async def test_unknown_runtime_interface_rejected_before_spawning(tmp_path,monkeypatch):
    spec,package,core,acp=fixture(tmp_path,monkeypatch)
    acp.write_text('export default {}')
    with pytest.raises(UnsupportedCapabilityError,match='重新适配'):
        await gemini.prepare_runtime_spec(spec,None,{})
    assert not (tmp_path/'state').exists()
