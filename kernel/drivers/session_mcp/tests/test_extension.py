import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

EXTENSION = Path(__file__).parents[1] / 'extension.mjs'


@pytest.mark.parametrize('kind', ['pi', 'commandcode'])
async def test_native_tool_registration_keeps_schema_errors_and_image_content(tmp_path, kind):
    node=shutil.which('node')
    if not node: pytest.skip('Node is unavailable')
    broker=tmp_path/'broker.py'
    broker.write_text('''import sys,json
print(json.dumps({'ready':True,'tools':[{'name':'mcp__test','title':'Test','description':'Synthetic','inputSchema':{'type':'object'},'readOnly':False}]}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 print(json.dumps({'id':r['id'],'result':{'content':[{'type':'text','text':'marker'},{'type':'image','data':'eA==','mimeType':'image/png'}]}}),flush=True)
''')
    config=tmp_path/'mcp.json';config.write_text(json.dumps({'python':sys.executable,'broker':str(broker)}));config.chmod(0o600)
    script='''
import extension from EXTENSION;
let tool, shutdown;
const api={on:(name,fn)=>{if(name==='session_shutdown')shutdown=fn;}};
api[KIND==='pi'?'registerTool':'addTool']=t=>{tool=t;};
await extension(api);
const result=KIND==='pi'?await tool.execute('id',{},new AbortController().signal):await tool.run({input:{},signal:new AbortController().signal});
await shutdown();
console.log(JSON.stringify({schema:tool.schema??tool.parameters,readOnly:tool.readOnly,result}));
'''.replace('EXTENSION',json.dumps(EXTENSION.as_uri())).replace('KIND',json.dumps(kind))
    process=await asyncio.create_subprocess_exec(node,'--input-type=module','-e',script,env={**os.environ,'KAUS_SESSION_MCP_CONFIG':str(config)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(process.communicate(),20)
    assert process.returncode==0,err.decode()
    result=json.loads(out)
    titles=Path(str(config)+'.titles.json')
    assert json.loads(titles.read_text())=={'schema':1,'titles':{'mcp__test':'Test'}}
    assert titles.stat().st_mode & 0o777 == 0o600
    image=result['result']['content'][1]
    if kind=='commandcode':
        assert result['schema']['input_schema']['required']==[]
        assert result['readOnly'] is False
        assert image=={'type':'image','source':{'type':'base64','media_type':'image/png','data':'eA=='}}
    else:assert image=={'type':'image','mimeType':'image/png','data':'eA=='}
    assert result['result']['content'][0]['text']=='marker'


async def test_shutdown_returns_after_broker_was_terminated_by_signal(tmp_path):
    node=shutil.which('node')
    if not node: pytest.skip('Node is unavailable')
    broker=tmp_path/'broker.py'
    broker.write_text('''import os,sys,json,time,signal
print(json.dumps({'ready':True,'tools':[]}),flush=True)
time.sleep(.1)
os.kill(os.getpid(),signal.SIGTERM)
''')
    config=tmp_path/'mcp.json';config.write_text(json.dumps({'python':sys.executable,'broker':str(broker)}))
    script='''
import extension from EXTENSION;
let shutdown;
await extension({addTool(){},on(name,fn){if(name==='session_shutdown')shutdown=fn;}});
await new Promise(resolve=>setTimeout(resolve,250));
await shutdown();
console.log('closed');
'''.replace('EXTENSION',json.dumps(EXTENSION.as_uri()))
    process=await asyncio.create_subprocess_exec(node,'--input-type=module','-e',script,
        env={**os.environ,'KAUS_SESSION_MCP_CONFIG':str(config)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err=await asyncio.wait_for(process.communicate(),5)
        assert process.returncode==0,err.decode()
        assert out.decode().strip()=='closed'
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
