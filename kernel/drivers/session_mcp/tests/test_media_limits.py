"""Native Pi/Command Code media and failure boundaries using synthetic MCPs."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from drivers.session_mcp.broker import Broker, MAX_BYTES, emit

HERE = Path(__file__).parent
EXTENSION = HERE.parent / "extension.mjs"
BROKER = HERE.parent / "broker.py"


async def run_native(tmp_path, kind, server, body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "schema": 1, "python": sys.executable, "broker": str(BROKER), "servers": [server],
    }))
    script = """
import extension from EXTENSION;
const kind = KIND;
const tools = [];
let shutdown;
const api = {on(name, fn) { if(name === 'session_shutdown') shutdown = fn; }};
api[kind === 'pi' ? 'registerTool' : 'addTool'] = tool => tools.push(tool);
await extension(api);
const call = async (nativeName, input, signal = new AbortController().signal) => {
  const tool = tools.find(item => (item.name ?? item.schema.name).includes('__' + nativeName + '_'));
  const result = kind === 'pi' ? await tool.execute('test', input, signal) : await tool.run({input, signal});
  return result.content;
};
try {
BODY
} finally { await shutdown(); }
""".replace("EXTENSION", json.dumps(EXTENSION.as_uri())).replace("KIND", json.dumps(kind)).replace("BODY", body)
    process = await asyncio.create_subprocess_exec(
        node, "--input-type=module", "-e", script,
        env={**os.environ, "KAUS_SESSION_MCP_CONFIG": str(config)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 25)
        assert process.returncode == 0, err.decode()
        return json.loads(out)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
async def test_image_near_limit_and_oversize_error_preserve_following_results(tmp_path, kind):
    server = tmp_path / "media_server.py"
    server.write_text("""
import json,sys
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request:
  continue
 method=request['method']
 if method=='initialize':
  result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'media-fixture','version':'1'}}
 elif method=='tools/list':
  result={'tools':[{'name':'image','inputSchema':{'type':'object','properties':{'size':{'type':'integer'}}}},
                   {'name':'echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}}}}]}
 elif method=='tools/call':
  args=request['params'].get('arguments',{})
  result={'content':[{'type':'image','mimeType':'image/png','data':'A'*args['size']}] if request['params']['name']=='image'
          else [{'type':'text','text':args['text']}]}
 else:
  result={}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
""")
    result = await run_native(tmp_path, kind, {
        "name": "media", "command": sys.executable, "args": [str(server)], "env": [],
    }, f"""
const before = await call('echo', {{text:'before'}});
const image = (await call('image', {{size:{MAX_BYTES - 1024}}}))[0];
let overflow;
try {{ await call('image', {{size:{MAX_BYTES}}}); }} catch (error) {{ overflow = error.message; }}
const after = await call('echo', {{text:'after'}});
console.log(JSON.stringify({{before:before[0].text, length:(image.data ?? image.source.data).length,
  mime:image.mimeType ?? image.source.media_type, overflow, after:after[0].text}}));
""")
    assert result == {
        "before": "before", "length": MAX_BYTES - 1024, "mime": "image/png",
        "overflow": "MCP result exceeds limit", "after": "after",
    }


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
async def test_native_abort_cancels_sdk_call_then_next_tool_still_returns(tmp_path, kind):
    log = tmp_path / "server.jsonl"
    result = await run_native(tmp_path, kind, {
        "name": "fixture", "command": sys.executable,
        "args": [str(HERE / "sdk_fixture.py"), "stdio", str(log)], "env": [],
    }, """
const {readFileSync} = await import('node:fs');
const controller = new AbortController();
const pending = call('slow', {}, controller.signal).then(() => 'unexpected', error => error.message);
while (!readFileSync(LOG, 'utf8').includes('slow-start')) await new Promise(resolve => setTimeout(resolve, 10));
controller.abort();
const cancelled = await pending;
const after = await call('echo', {text:'after-cancel'});
while (!readFileSync(LOG, 'utf8').includes('slow-cancelled')) await new Promise(resolve => setTimeout(resolve, 10));
console.log(JSON.stringify({cancelled, after:after[0].text}));
""".replace("LOG", json.dumps(str(log))))
    assert result == {"cancelled": "MCP call cancelled", "after": "after-cancel"}


async def test_tool_timeout_budget_and_following_call_keep_same_client():
    calls = []
    client = SimpleNamespace()

    async def call_tool(name, arguments, *, read_timeout_seconds):
        calls.append((name, read_timeout_seconds))
        if name == "slow":
            raise TimeoutError("synthetic upstream timeout")
        return SimpleNamespace(model_dump=lambda **kwargs: {"content": [{"type": "text", "text": "after-timeout"}]})

    client.call_tool = call_tool
    broker = Broker({"servers": []})
    broker.tools = {"slow": (client, "slow"), "echo": (client, "echo")}
    with pytest.raises(TimeoutError):
        await broker.call("slow", {})
    assert (await broker.call("echo", {}))["content"][0]["text"] == "after-timeout"
    assert calls == [("slow", 60), ("echo", 60)]


def test_exact_wire_byte_limit_and_error_retain_request_identity(capsys):
    # The cap covers serialized JSON bytes, including the envelope, not raw image bytes.
    empty = {"id": "image-turn", "result": {"content": [{"type": "image", "mimeType": "image/png", "data": ""}]}}
    overhead = len(json.dumps(empty, ensure_ascii=False).encode())
    empty["result"]["content"][0]["data"] = "A" * (MAX_BYTES - overhead)
    emit(empty)
    assert len(capsys.readouterr().out.rstrip("\n").encode()) == MAX_BYTES
    empty["result"]["content"][0]["data"] += "A"
    emit(empty)
    assert json.loads(capsys.readouterr().out) == {"id": "image-turn", "error": "MCP result exceeds limit"}


async def test_actual_sdk_timeout_returns_error_and_next_tool_works(tmp_path):
    from drivers.session_mcp.tests.test_broker import frame, send, finish, until_event
    from drivers.session_mcp.broker import public_tool_name

    log = tmp_path / "timeout.jsonl"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": 1, "servers": [{
        "name": "fixture", "command": sys.executable,
        "args": [str(HERE / "sdk_fixture.py"), "stdio", str(log)], "env": [],
    }]}))
    # Keep the real SDK timeout/cancellation path but accelerate its clock budget.
    launcher = tmp_path / "timeout_broker.py"
    launcher.write_text("""
import asyncio
from drivers.session_mcp import broker
original = broker.Client.call_tool
async def accelerated(self, name, arguments, *, read_timeout_seconds):
    assert read_timeout_seconds == 60
    return await original(self, name, arguments, read_timeout_seconds=0.1)
broker.Client.call_tool = accelerated
raise SystemExit(asyncio.run(broker.main()))
""")
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(launcher), str(config),
        env={**os.environ, "PYTHONPATH": str(HERE.parents[2])},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert (await frame(process))["ready"]
        await send(process, {"id": "slow", "name": public_tool_name("fixture", "slow"), "arguments": {}})
        assert await frame(process) == {"id": "slow", "error": "MCP tool call failed"}
        await until_event(log, "slow-cancelled")
        await send(process, {"id": "next", "name": public_tool_name("fixture", "echo"),
                             "arguments": {"text": "after-timeout"}})
        assert (await frame(process))["result"]["content"][0]["text"] == "after-timeout"
    finally:
        await finish(process)
