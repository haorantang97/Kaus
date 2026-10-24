"""Opt-in native Cursor codec, single-store bridge and MCP parser acceptance.

Uses the installed binary's own codecs with synthetic data. No login, network,
model request or daily Cursor history is used. This is not cloud acceptance.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'kernel'))
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.skill_execution import cursor, cursor_terminal

WRITE = r'''
const crypto=require('node:crypto');
const input=JSON.parse(process.argv[1]);
globalThis.fetch=()=>{throw Error('Network disabled')};
(async()=>{
 const {Blob,Store,context,require:r}=await require(input.loader)(input);
 const proto=r('../proto/dist/generated/agent/v1/agent_pb.js');
 const db=await Blob.initAndLoad(input.database),store=new Store(db,db);
 await store.resetFromDb(context);
 const initial=store.getId();if(initial&&initial!==input.id)throw Error('Wrong native identity');
 store.setMetadata('agentId',input.id);store.setMetadata('name','Synthetic bridge acceptance');
 store.setMetadata('createdAt',Date.now());
 async function save(value){const data=value.toBinary(),id=new Uint8Array(crypto.createHash('sha256').update(data).digest());await db.setBlob(context,id,data);return id}
 const user=await save(new proto.UserMessage({text:input.text,messageId:'message-'+input.step}));
 const hidden=await save(new proto.ConversationStep({message:{case:'thinkingMessage',value:{text:'PRIVATE_REASONING'}}}));
 const reply=await save(new proto.ConversationStep({message:{case:'assistantMessage',value:{text:'Reply '+input.text}}}));
 const turn=await save(new proto.ConversationTurnStructure({turn:{case:'agentConversationTurn',value:{userMessage:user,steps:[hidden,reply],requestId:'request-'+input.step}}}));
 const state=store.getConversationStateStructure();state.turns.push(turn);
 await store.handleCheckpoint(context,state);await store.dispose();
})().catch(e=>{console.error(e);process.exitCode=1});
'''

MCP = r'''
const fs=require('node:fs'),path=require('node:path'),Module=require('node:module'),assert=require('node:assert');
const input=JSON.parse(process.argv[1]),index=path.join(input.package,'index.js'),original=Module.prototype._compile;
// The native parser hook sees the original, hashed code before this test-only
// entry suppression. CLI authentication/telemetry are never booted.
Module.prototype._compile=function(code,file){if(file===index){
 const anchor='var __webpack_exports__=__webpack_require__("./src/main.tsx")';
 assert.equal(code.split(anchor).length,2);code=code.replace(anchor,'globalThis.fixtureRequire=__webpack_require__')}
 return original.call(this,code,file)};
const scope={modules:Object.fromEntries(Object.entries(input.modules).map(([k,v])=>[path.join(input.package,k),v])),paths:null};
process.env.KAUS_CURSOR_TERMINAL_MCP=JSON.stringify({path:input.file,servers:{selected:{command:'fixture',env:{TOKEN:'synthetic-secret'}}}});
require(input.guard).install(scope);require(index);
(async()=>{
 const read=globalThis.fixtureRequire('../mcp-agent-exec/dist/index.js').h1;
 assert.deepStrictEqual(await read(input.file),{mcpServers:{selected:{command:'fixture',env:{TOKEN:'synthetic-secret'}}}});
 fs.writeFileSync(input.file,JSON.stringify({mcpServers:{personal:{command:'preserved'}}}));
 assert.deepStrictEqual(Object.keys((await read(input.file)).mcpServers).sort(),['personal','selected']);
 assert(!fs.readFileSync(input.file,'utf8').includes('synthetic-secret'));
 console.log('verified');
})().catch(e=>{console.error(e);process.exitCode=1});
'''


async def run(args):
    root = args.output.resolve()/('cursor-store-'+uuid.uuid4().hex[:10])
    work = root/'work'; work.mkdir(parents=True,mode=0o700)
    spec = AcpAgentSpec(command=(args.native_cli,'acp'),cwd=str(work),inherit_env=False,
        env={'PATH':os.environ['PATH'],'HOME':str(root/'home'),'CURSOR_CONFIG_DIR':str(root/'config'),
             'CURSOR_DATA_DIR':str(root/'data')})
    package = cursor.package_path(spec)
    if not package: raise RuntimeError('The installed Cursor build needs verification')
    sid = str(uuid.uuid4())
    binding = AgentBinding.create(project='fixture',backend='cursor',runtime_config={'workspace_root':str(work)})
    native, terminal, _ = cursor_terminal.session_paths(spec,sid,str(work))
    native.mkdir(parents=True);(native/'meta.json').write_text(json.dumps({'schemaVersion':1,'cwd':str(work)}))
    data={'package':str(package),'modules':cursor.MODULE_HASHES,'loader':str(Path(cursor.__file__).with_name('cursor_native.cjs')),'id':sid}
    async def write(database,step,text):
        command=(str(package/'node'),'-e',WRITE,json.dumps({**data,'database':str(database),'step':step,'text':text}))
        result=await asyncio.to_thread(subprocess.run,command,env=spec.build_env(),text=True,capture_output=True,timeout=20)
        assert result.returncode==0,result.stderr
    await write(native/'store.db',1,'Kaus first')
    before=await cursor_terminal.load_native_history(spec,binding,sid)
    cursor_terminal.bridge_store(spec,sid,str(work))
    await write(terminal/'store.db',2,'CLI second remembers Kaus first')
    returned=await cursor_terminal.load_native_history(spec,binding,sid)
    assert [e.text for e in returned.entries]==['Kaus first','Reply Kaus first','CLI second remembers Kaus first','Reply CLI second remembers Kaus first']
    assert returned.entries[:2]==before.entries
    await write(native/'store.db',3,'Kaus third remembers CLI second')
    final=await cursor_terminal.load_native_history(spec,binding,sid)
    assert len(final.entries)==6 and len({e.entry_id for e in final.entries})==6
    assert (terminal/'store.db').stat().st_ino==(native/'store.db').stat().st_ino
    mcp={**data,'file':str(work/'mcp.json'),'guard':str(Path(cursor.__file__).with_name('cursor_guard.cjs'))}
    result=await asyncio.to_thread(subprocess.run,(str(package/'node'),'-e',MCP,json.dumps(mcp)),env=spec.build_env(),text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    report={'passed':True,'kind':'native storage and parser, no model','nativeVersion':'2026.09.18',
        'sameDatabase':True,'threeSurfaceWrites':True,'publicHistoryEntries':len(final.entries),
        'stableMessageIds':True,'reasoningExcluded':True,'nativeMcpParser':True,'nativeSessionId':sid}
    (root/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--native-cli',default='cursor-agent');asyncio.run(run(p.parse_args()))
