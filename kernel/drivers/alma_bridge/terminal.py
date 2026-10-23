"""Keep the native Alma TUI on the existing chat, through a private loopback proxy."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from websockets.asyncio.client import connect

from drivers.alma_bridge.bridge import local_url
from drivers.alma_bridge.runtime import public_messages, read_messages
from drivers.projection_store import atomic_write_text
from drivers.skill_execution.bridge_terminal import main, run_terminal


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The terminal supervisor owns termination and waits for native stop.
        yield


class TerminalProxy:
    def __init__(self, config, *, client=None):
        if config.get('mode') != 'chat':
            raise ValueError('Alma 终端暂不支持工具审批，请在 Kaus 中继续')
        self.config = config
        self.upstream = local_url(config['url'])
        self.ident = config['nativeId']
        self.path = '/api/threads/' + quote(self.ident, safe='')
        self.client = client or httpx.AsyncClient(base_url=self.upstream, timeout=20, trust_env=False, follow_redirects=False)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.token = secrets.token_urlsafe(24)
        self.native = None
        self.workspace = None
        self.models = set()
        self.active = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.socket = None
        self.started = False
        self.failed = False
        self.app.add_api_route('/' + self.token + '/{path:path}', self.request, methods=['GET', 'POST', 'PUT', 'DELETE'])
        self.app.add_api_websocket_route('/' + self.token + '/ws/threads', self.websocket)

    async def native_request(self, method, path, body=None):
        response = await self.client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    async def verify(self):
        record = json.loads(Path(self.config['recordPath']).read_text())
        if (record.get('threadId') != self.ident or record.get('url') != self.upstream
                or record.get('cwd') != self.config['cwd'] or record.get('mode', 'chat') != 'chat'):
            raise ValueError('Alma 会话配置已变化，请重新打开终端')
        self.native = await self.native_request('GET', self.path)
        if self.native.get('id') != self.ident or not self.native.get('workspaceId'):
            raise ValueError('Alma 会话不可用')
        self.workspace = await self.native_request('GET', '/api/workspaces/' + quote(self.native['workspaceId'], safe=''))
        if self.workspace.get('path') != self.config['cwd'] or self.workspace.get('remoteHostId'):
            raise ValueError('Alma 会话工作目录不一致')
        # Check public history before opening a writable terminal. This also
        # verifies the native pagination contract without creating a new thread.
        public_messages(await read_messages(self.client, self.ident))
        raw_models = await self.native_request('GET', '/api/models')
        self.model_rows = raw_models if isinstance(raw_models, list) else raw_models.get('data', [])
        self.models = {row.get('id') for row in self.model_rows if isinstance(row, dict)}
        model = self.config.get('model') or self.native.get('model')
        if model not in self.models:
            raise ValueError('Alma 当前模型不可用')
        self.config['model'] = model

    async def request(self, request: Request, path: str):
        path = '/' + path
        try:
            if request.method == 'GET':
                if path == '/api/settings':
                    data = {'chat': {'defaultModel': self.config['model']}}
                elif path == '/api/models':
                    data = {'data': self.model_rows}
                elif path == '/api/threads':
                    data = [self.native]
                elif path == self.path:
                    data = self.native
                elif path == self.path + '/messages':
                    data = public_messages(await read_messages(self.client, self.ident))
                else:
                    return JSONResponse({'error': '当前终端仅连接这段对话'}, status_code=404)
                return JSONResponse(data)
            # The native TUI should never create or delete a thread after the
            # entrypoint has selected the owned thread. Do not hide regressions.
            return JSONResponse({'error': '请在 Kaus 中管理对话'}, status_code=403)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'Alma 本地服务暂时不可用'}, status_code=502)

    async def generation(self, value):
        data = value.get('data', {})
        if not isinstance(data, dict) or data.get('threadId') != self.ident:
            raise ValueError('不能从此终端切换其他对话')
        kind = value.get('type')
        if kind == 'stop_generation':
            return {'type': kind, 'data': {'threadId': self.ident}}
        if kind != 'generate_response' or self.active:
            raise ValueError('请等待当前回复完成')
        model = data.get('model') or self.config['model']
        if model not in self.models:
            raise ValueError('Alma 当前模型不可用')
        user = data.get('userMessage')
        if not isinstance(user, dict) or user.get('role') != 'user' or not isinstance(user.get('parts'), list):
            raise ValueError('消息格式无效')
        if model != self.native.get('model'):
            native = await self.native_request('PUT', self.path, {'model': model})
            if native.get('model') != model:
                raise ValueError('Alma 未应用所选模型')
            self.native = native
        path = Path(self.config['recordPath'])
        record = json.loads(path.read_text())
        if record.get('threadId') != self.ident or record.get('mode', 'chat') != 'chat':
            raise ValueError('Alma 会话配置已变化，请重新打开终端')
        record['model'] = model
        atomic_write_text(path, json.dumps(record))
        path.chmod(0o600)
        self.config['model'] = model
        self.active = True
        self.idle.clear()
        return {'type': kind, 'data': {'threadId': self.ident, 'model': model, 'userMessage': user,
                                     'noTools': True, 'enabledMCPServerIds': []}}

    async def websocket(self, ws: WebSocket):
        await ws.accept()
        if self.socket is not None:
            await ws.close(code=1008)
            return
        upstream = None
        reader = None
        try:
            upstream = await connect(self.upstream.replace('http:', 'ws:', 1) + '/ws/threads',
                                     open_timeout=10, close_timeout=1, max_size=16 * 1024 * 1024, proxy=None)
            self.socket = upstream
            initial = json.loads(await asyncio.wait_for(upstream.recv(), 10))
            if initial.get('type') != 'generating_snapshot' or self.ident in initial.get('data', {}).get('ids', []):
                raise ValueError('Alma 当前对话仍在运行')
            await ws.send_json({'type': 'generating_snapshot', 'data': {'ids': []}})
            async def read():
                async for raw in upstream:
                    envelope = json.loads(raw)
                    data = envelope.get('data') or {}
                    own = data.get('threadId', data.get('id')) == self.ident
                    if own and envelope.get('type') == 'thread_generating':
                        self.active = bool(data.get('isGenerating'))
                        if not self.active:
                            self.idle.set()
                    if own:
                        with contextlib.suppress(Exception):
                            await ws.send_json(envelope)
                self.failed = True
                with contextlib.suppress(Exception):
                    await ws.close(code=1011)
            reader = asyncio.create_task(read())
            while True:
                value = await ws.receive_json()
                try:
                    await upstream.send(json.dumps(await self.generation(value)))
                except (ValueError, httpx.HTTPError) as exc:
                    message = str(exc) if isinstance(exc, ValueError) else 'Alma 本地服务暂时不可用'
                    await ws.send_json({'type': 'error', 'data': {'error': message}})
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
        finally:
            if upstream and self.active:
                try:
                    await upstream.send(json.dumps({'type': 'stop_generation', 'data': {'threadId': self.ident}}))
                    await asyncio.wait_for(self.idle.wait(), 8)
                except (Exception, asyncio.CancelledError):
                    self.failed = True
            if reader:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if upstream:
                try:
                    await asyncio.wait_for(upstream.close(), 2)
                except (Exception, asyncio.CancelledError):
                    self.failed = True
            self.socket = None

    async def close(self):
        await self.client.aclose()


async def run():
    config = json.loads(Path(sys.argv[1]).read_text())
    proxy = TerminalProxy(config)
    server_task = None
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind(('127.0.0.1', 0))
    server = _Server(uvicorn.Config(proxy.app, log_level='critical', access_log=False, lifespan='off'))
    code = 1
    try:
        await proxy.verify()
        server_task = asyncio.create_task(server.serve(sockets=[bound]))
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise ValueError('终端连接未能启动')
                await asyncio.sleep(.01)
        env = dict(os.environ)
        env['ALMA_API_URL'] = f'http://127.0.0.1:{bound.getsockname()[1]}/{proxy.token}'
        history = str(Path(config['recordPath']).with_suffix('.terminal-history.txt'))
        code = await run_terminal((config['node'], str(Path(__file__).with_name('tui_entry.mjs')),
                                  config['tui'], config['nativeId'], history), cwd=config['cwd'], env=env)
    except asyncio.CancelledError:
        # Finish native cancellation below before returning a signal exit. The
        # supervisor must distinguish a confirmed stop from a lost connection.
        code = 130
    finally:
        server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, 12)
            except asyncio.TimeoutError:
                proxy.failed = True
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        bound.close()
        await proxy.close()
    if proxy.failed:
        print('Alma 任务状态未确认，请在 Kaus 中重新连接。', file=sys.stderr)
        return 1
    return code


if __name__ == '__main__':
    try:
        raise SystemExit(main(run))
    except (ValueError, OSError, httpx.HTTPError):
        print('Alma 终端未能连接，请确认本地服务和对话状态。', file=sys.stderr)
        raise SystemExit(1)
