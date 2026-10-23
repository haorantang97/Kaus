"""Real stdio MCP roundtrip through the desktop view, with synthetic tools."""
import base64
import json
import sys
from pathlib import Path

from drivers.computer_use.cua_proxy import DESKTOP_TOOLS


def test_canonical_desktop_view_cannot_dispatch_browser_or_setup_actions():
    assert {'get_window_state', 'click', 'type_text'} <= DESKTOP_TOOLS
    assert not any(name.startswith('browser_') for name in DESKTOP_TOOLS)
    assert not {'run_script', 'run_steps', 'page', 'check_permissions', 'set_config',
                'install_extension', 'install_ffmpeg', 'start_recording'} & DESKTOP_TOOLS


async def test_proxy_filters_catalog_and_calls_and_preserves_image_result(tmp_path):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    from drivers.session_mcp.stdio_compat import stdio_client
    fake = tmp_path / 'cua-driver'
    log = tmp_path / 'calls.jsonl'
    # Valid 1x1 PNG; synthetic evidence, no screen capture.
    png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhAAAAABJRU5ErkJggg=='
    fake.write_text('#!' + sys.executable + '\n' + '''import json, sys
for line in sys.stdin:
 r=json.loads(line);m=r.get('method')
 if 'id' not in r: continue
 if m=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
 elif m=='tools/list': result={'tools':[{'name':n,'inputSchema':{'type':'object'}} for n in ['get_window_state','click','type_text','browser_click','run_script','check_permissions']]}
 elif m=='tools/call':
  with open(''' + repr(str(log)) + ''','a') as f: f.write(json.dumps(r['params'])+'\\n')
  result={'content':[{'type':'image','data':''' + repr(png) + ''','mimeType':'image/png'}]}
 elif m=='ping': result={}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    bootstrap = tmp_path / 'proxy.py'
    kernel = str(Path(__file__).resolve().parents[3])
    bootstrap.write_text('import asyncio,sys\nsys.path.insert(0,' + repr(kernel) + ')\n'
        'from pathlib import Path\nfrom drivers.computer_use import installer\n'
        'async def host(**kwargs): return Path("/synthetic/socket")\n'
        'installer.ensure_runtime_host=host\nfrom drivers.computer_use.cua_proxy import serve, _arm_retirement\n'
        'from contextlib import asynccontextmanager\n'
        '@asynccontextmanager\nasync def operation(*args): yield\n'
        'asyncio.run(serve(' + repr(str(fake)) + ', operation_factory=operation))\n')
    transport = stdio_client(StdioServerParameters(command=sys.executable, args=[str(bootstrap)]))
    async with Client(transport, read_timeout_seconds=5) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {'get_window_state', 'click', 'type_text'}
        result = await client.call_tool('get_window_state', {'pid': 42})
        assert not result.is_error
        assert result.content[0].data == png
        assert result.content[0].mime_type == 'image/png'
        for name in ('browser_click', 'run_script', 'check_permissions'):
            denied = await client.call_tool(name, {'prompt': True})
            assert denied.is_error
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [{'name': 'get_window_state', 'arguments': {'pid': 42}}]


async def test_cancelled_rpc_retires_proxy_and_keeps_uncertain_receipt(tmp_path):
    import asyncio
    fake = tmp_path / 'cua-driver'
    marker = tmp_path / 'unconfirmed'
    fake.write_text('#!' + sys.executable + '\n' + '''import json,sys,time
for line in sys.stdin:
 r=json.loads(line); m=r.get('method')
 if 'id' not in r: continue
 if m=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
 elif m=='tools/list': result={'tools':[{'name':'click','inputSchema':{'type':'object'}}]}
 elif m=='tools/call': time.sleep(60); continue
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    kernel = str(Path(__file__).resolve().parents[3])
    bootstrap = tmp_path / 'proxy.py'
    bootstrap.write_text('import asyncio,sys\nsys.path.insert(0,' + repr(kernel) + ')\n'
        'from pathlib import Path\nfrom drivers.computer_use import installer\n'
        'async def host(**kwargs): return Path("/synthetic/socket")\n'
        'installer.ensure_runtime_host=host\nfrom drivers.computer_use.cua_proxy import serve, _arm_retirement\n'
        'class Operation:\n'
        ' def __init__(self,*args): self.dispatched=True\n'
        ' async def __aenter__(self): Path(' + repr(str(marker)) + ').touch()\n'
        ' async def __aexit__(self,typ,*args):\n'
        '  if typ is None: Path(' + repr(str(marker)) + ').unlink()\n'
        'asyncio.run(serve(' + repr(str(fake)) + ',operation_factory=Operation,on_uncertain=_arm_retirement))\n')
    process = await asyncio.create_subprocess_exec(sys.executable, str(bootstrap),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def send(value):
        process.stdin.write((json.dumps({'jsonrpc':'2.0', **value}) + '\n').encode())
        await process.stdin.drain()
    try:
        await send({'id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05',
            'capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        response = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert response['id'] == 1
        await send({'method':'notifications/initialized'})
        await send({'id':2,'method':'tools/call','params':{'name':'click','arguments':{}}})
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        await send({'method':'notifications/cancelled','params':{'requestId':2,'reason':'test'}})
        await asyncio.wait_for(process.wait(), 8)
        assert marker.exists()
        assert process.returncode in (0, 70)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_live_proxy_reconnects_after_daemon_generation_changes_without_replaying_calls(tmp_path):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    from drivers.session_mcp.stdio_compat import stdio_client
    generation = tmp_path / 'generation'
    generation.write_text('1')
    calls = tmp_path / 'calls'
    fake = tmp_path / 'cua-driver'
    fake.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
generation=Path(''' + repr(str(generation)) + ''').read_text()
for line in sys.stdin:
 r=json.loads(line);m=r.get('method')
 if 'id' not in r: continue
 if m=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
 elif m=='tools/list': result={'tools':[{'name':'click','inputSchema':{'type':'object'}}]}
 elif m=='tools/call':
  with open(''' + repr(str(calls)) + ''','a') as f: f.write(generation+'\\n')
  result={'content':[{'type':'text','text':generation}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    fake.chmod(0o700)
    bootstrap = tmp_path / 'proxy.py'
    kernel = str(Path(__file__).resolve().parents[3])
    bootstrap.write_text('import asyncio,os,sys\nsys.path.insert(0,' + repr(kernel) + ')\n'
        'from pathlib import Path\nfrom drivers.computer_use import installer,cua_guard,lease\n'
        'async def host(**kwargs): return Path("/synthetic/socket")\n'
        'installer.ensure_runtime_host=host\n'
        'lease.default_lease_root=lambda: Path(' + repr(str(tmp_path / 'locks')) + ')\n'
        'cua_guard._peer_identity=lambda _: {"pid":999999,"uid":os.getuid(),"started":Path(' + repr(str(generation)) + ').read_text()}\n'
        'from drivers.computer_use.cua_proxy import serve\n'
        'asyncio.run(serve(' + repr(str(fake)) + '))\n')
    transport = stdio_client(StdioServerParameters(command=sys.executable, args=[str(bootstrap)]))
    async with Client(transport, read_timeout_seconds=8) as client:
        await client.list_tools()
        assert (await client.call_tool('click', {})).content[0].text == '1'
        generation.write_text('2')
        assert (await client.call_tool('click', {})).content[0].text == '2'
        assert (await client.call_tool('click', {})).content[0].text == '2'
    assert calls.read_text().splitlines() == ['1', '2', '2']
