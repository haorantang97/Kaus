"""Actual SDK transports, cancellation and child lifecycle; no model accounts."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from drivers.session_mcp.broker import public_tool_name

HERE = Path(__file__).parent
BROKER = HERE.parent / 'broker.py'


async def frame(process):
    line = await asyncio.wait_for(process.stdout.readline(), 15)
    assert line, 'broker exited before responding'
    return json.loads(line)


async def send(process, value):
    process.stdin.write(json.dumps(value).encode() + b'\n')
    await process.stdin.drain()


async def finish(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def until_event(path, wanted):
    async with asyncio.timeout(8):
        while wanted not in [row['event'] for row in events(path)]:
            await asyncio.sleep(.03)


@asynccontextmanager
async def running(tmp_path, kind):
    log = tmp_path / 'server.jsonl'
    native = None
    if kind == 'stdio':
        entry = {'name': 'fixture', 'command': sys.executable,
            'args': [str(HERE / 'sdk_fixture.py'), 'stdio', str(log)], 'env': []}
    else:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            port = sock.getsockname()[1]
            native = await asyncio.create_subprocess_exec(sys.executable, str(HERE / 'sdk_fixture.py'),
                kind, str(log), str(sock.fileno()), pass_fds=(sock.fileno(),),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await until_event(log, 'start')
        entry = {'name': 'fixture', 'type': kind, 'url': f'http://127.0.0.1:{port}/' + ('sse' if kind == 'sse' else 'mcp'),
            'headers': [{'name': 'x-kaus-fixture', 'value': 'synthetic-header'}]}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema': 1, 'servers': [entry]}))
    process = await asyncio.create_subprocess_exec(sys.executable, str(BROKER), str(config),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        ready = await frame(process)
        assert ready.get('ready') is True, ready
        yield process, ready, log
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 8)
            finally:
                await finish(process)
        if native is not None:
            await finish(native)


@pytest.mark.parametrize('kind', ['stdio', 'http', 'sse'])
async def test_actual_sdk_transport_returns_tool_result_and_closes(tmp_path, kind):
    async with running(tmp_path, kind) as (process, ready, log):
        name = public_tool_name('fixture', 'echo')
        assert name in [tool['name'] for tool in ready['tools']]
        await send(process, {'id': 'one', 'name': name, 'arguments': {'text': 'from-' + kind}})
        result = await frame(process)
        assert result['result']['content'][0]['text'] == 'from-' + kind
        assert result['result'].get('isError') is False
    if kind == 'stdio':
        pid = events(log)[0]['pid']
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize('kind', ['stdio', 'http', 'sse'])
async def test_cancel_stops_native_tool_and_preserves_connection(tmp_path, kind):
    async with running(tmp_path, kind) as (process, ready, log):
        await send(process, {'id': 'slow', 'name': public_tool_name('fixture', 'slow'), 'arguments': {}})
        await until_event(log, 'slow-start')
        await send(process, {'cancel': 'slow'})
        assert (await frame(process)) == {'id': 'slow', 'error': 'MCP call cancelled'}
        await until_event(log, 'slow-cancelled')
        await send(process, {'id': 'after', 'name': public_tool_name('fixture', 'echo'), 'arguments': {'text': 'after'}})
        assert (await frame(process))['result']['content'][0]['text'] == 'after'


async def test_failed_handshake_is_bounded_private_and_closes_process(tmp_path):
    server = tmp_path / 'failed.py'
    marker = tmp_path / 'pid'
    server.write_text('import sys,json,os\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\n'
        'for line in sys.stdin:\n'
        ' r=json.loads(line)\n'
        ' if "id" in r: print(json.dumps({"jsonrpc":"2.0","id":r["id"],"error":{"code":-32000,"message":"synthetic-secret"}}),flush=True)\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema': 1, 'servers': [{'name': 'bad', 'command': sys.executable,
        'args': [str(server), str(marker)], 'env': []}]}))
    process = await asyncio.create_subprocess_exec(sys.executable, str(BROKER), str(config),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 78
        assert json.loads(stdout) == {'ready': False, 'error': 'MCP connection failed'}
        assert b'synthetic-secret' not in stdout + stderr
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    finally:
        await finish(process)
