"""Native attach TUI backed by a private, early Skill-command gate.

The agent owns execution and its original session database. This adapter only
checks command scope before native template expansion and restores Kaus's latest
per-session settings through the native API; it does not replace the TUI.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sys
from urllib.parse import parse_qsl
from starlette.requests import Request

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).parents[2]))

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution.bridge_terminal import options_path, main, run_terminal
from drivers.skill_execution.terminal_settings import approval, choice, immutable, selected
from drivers.terminals.managed_process import register_owned_child

VERSIONS = {'opencode': '1.18.32', 'kilo': '7.8.1'}


def remember(spec, conversation, metadata):
    from drivers.projection_store import atomic_write_text
    path = options_path(spec, conversation)
    if path.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录不可用')
    previous = json.loads(path.read_text()) if path.exists() else {}
    servers = metadata.get('mcpServers', [])
    names = set(previous.get('ownedMcpNames', [])) | {row['name'] for row in previous.get('mcpServers', [])} | {row['name'] for row in servers}
    atomic_write_text(path, json.dumps({'mcpServers': servers, 'ownedMcpNames': sorted(names)}))
    path.chmod(0o600)



def command(plan, native_command, *, conversation, binding, spec, engine):
    cwd = (binding.runtime_config.get('workspace_root') if binding else None) or spec.cwd
    ident = conversation.native_session_id
    if not cwd or not isinstance(ident, str) or not re.fullmatch(r'ses[A-Za-z0-9_-]{1,128}', ident):
        raise UnsupportedCapabilityError('原生会话不可用于终端续接')
    try:
        current = json.loads(options_path(spec, conversation).read_text())
        if not isinstance(current.get('mcpServers'), list):
            raise ValueError()
    except (OSError, ValueError):
        raise UnsupportedCapabilityError('终端配置尚未就绪，请重新打开会话') from None
    mode = choice(approval(conversation), {'ask': 'ask', 'host:ask': 'ask', 'bypass': 'allow', 'native:auto': 'allow'}, '审批方式')
    data = {'engine': engine, 'executable': native_command[0], 'sessionId': ident,
        'cwd': str(Path(cwd).resolve()), 'mcpServers': current['mcpServers'], 'ownedMcpNames': current.get('ownedMcpNames', []),
        'model': selected(conversation, 'model_id', 'model'),
        'variant': selected(conversation, 'reasoning_mode', 'reasoning', 'variant'),
        'agent': selected(conversation, 'execution_mode', 'mode'), 'approval': mode,
        'allowedSkills': None, 'scopeOverlay': {}, 'scopePlugins': []}
    if plan is not None and plan.root is not None:
        prefix = engine.upper() + '_CONFIG_CONTENT'
        projected = json.loads(plan.spec.build_env().get(prefix) or '{}')
        original = json.loads(spec.build_env().get(prefix) or '{}')
        data.update(allowedSkills=list(plan.names),
            scopeOverlay={'skills': projected.get('skills', {}), 'permission': {'skill': projected.get('permission', {}).get('skill', {})},
                **{key: {name: {'permission': {'skill': value.get('permission', {}).get('skill', {})}}
                    for name, value in projected.get(key, {}).items()} for key in ('agent', 'mode')}},
            scopePlugins=[value for value in projected.get('plugin', []) if value not in original.get('plugin', [])])
    # Prevent old same-named native MCP definitions from starting even briefly.
    if data['ownedMcpNames']:
        guard = 'const names=' + json.dumps(data['ownedMcpNames']) + ';export const KausMcpHandoff=async()=>({config:async config=>{config.mcp={...(config.mcp||{})};for(const name of names)config.mcp[name]={...(config.mcp[name]||{}),enabled:false}}});'
        data['scopePlugins'].append(immutable(spec, conversation, guard, suffix='.mjs').as_uri())
    manifest = immutable(spec, conversation, json.dumps(data, sort_keys=True), suffix='.json')
    return (sys.executable, str(Path(__file__).resolve()), str(manifest))


def merge(left, right):
    value = dict(left) if isinstance(left, dict) else {}
    for key, item in right.items():
        value[key] = merge(value.get(key), item) if isinstance(item, dict) else item
    return value


def environment(data, inherited):
    env = dict(inherited)
    prefix = data['engine'].upper()
    config = merge(json.loads(env.get(prefix + '_CONFIG_CONTENT') or '{}'), data['scopeOverlay'])
    config['plugin'] = list(dict.fromkeys([*config.get('plugin', []), *data['scopePlugins']]))
    for name in data.get('ownedMcpNames', []):
        config.setdefault('mcp', {})[name] = {**config.get('mcp', {}).get(name, {}), 'enabled': False}
    if data.get('model'):
        config['model'] = data['model']
    if data.get('agent'):
        config['default_agent'] = data['agent']
    env[prefix + '_CONFIG_CONTENT'] = json.dumps(config)
    # Native serve and attach share this one-shot credential. It never enters
    # command arguments, persistent manifests, logs, or the user's shell config.
    env[prefix + '_SERVER_PASSWORD'] = secrets.token_urlsafe(32)
    env[prefix + '_SERVER_USERNAME'] = data['engine']
    return env


def mcp_config(server):
    if server.get('type') in ('http', 'sse'):
        return {'type': 'remote', 'url': server['url'], 'headers': {
            row['name']: row['value'] for row in server.get('headers', [])}, 'enabled': True}
    if not isinstance(server.get('command'), str) or not server['command']:
        raise ValueError('invalid MCP command')
    return {'type': 'local', 'command': [server['command'], *server.get('args', [])],
        'environment': {row['name']: row['value'] for row in server.get('env', [])}, 'enabled': True}


class CommandGate:
    def __init__(self, client, allowed, session_id, inventory=None, selections=None):
        self.client = client
        self.allowed = None if allowed is None else set(allowed)
        self.session_id = session_id
        self.inventory = inventory
        self.selections = selections or {}
        self.restored = False
        self.sessions = {session_id}

    async def commands(self):
        response = await self.client.get('/command')
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, list):
            raise ValueError('invalid native command catalog')
        return value

    def admitted(self, row):
        return self.allowed is None or row.get('source') != 'skill' or row.get('name') in self.allowed or (
            row.get('name', '').endswith(':skill') and row['name'][:-6] in self.allowed)

    async def check(self, path, value):
        if self.allowed is None:
            return
        name = value.get('command') if path.endswith('/command') else None
        # V2 API can route slash text to the command executor. Protect it too.
        if path.endswith(('/prompt', '/prompt_async', '/message')):
            parts = value.get('parts') or value.get('prompt') or []
            text = parts if isinstance(parts, str) else ''.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')
            text = text.strip('\ufeff \t\r\n')
            if text.startswith('/'):
                name = re.split(r'[\s\ufeff]+', text[1:], maxsplit=1)[0]
        if not name:
            return
        rows = await self.commands()
        for row in rows:
            if row.get('name') == name:
                if not self.admitted(row):
                    raise PermissionError('技能不在当前项目范围内')
                return
        if name.endswith(':skill') and name[:-6] not in self.allowed:
            raise PermissionError('技能不在当前项目范围内')
        # A running native instance may cache /command. Fresh inventory is
        # needed only for unknown slash names, before forwarding any prompt.
        if self.inventory is not None:
            skills, config = await self.inventory()
            explicit = config.get('command', {}).get(name, {})
            if isinstance(explicit, dict) and isinstance(explicit.get('template'), str):
                return
            if any(row.get('name') == name for row in skills) and name not in self.allowed:
                raise PermissionError('技能不在当前项目范围内')

    def app(self):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse, StreamingResponse
        from starlette.background import BackgroundTask
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        async def belongs(ident):
            if ident in self.sessions:
                return True
            visited = set()
            cursor = ident
            for _ in range(16):
                if cursor in visited:
                    return False
                visited.add(cursor)
                reply = await self.client.get('/session/' + cursor)
                if reply.status_code != 200:
                    return False
                parent = reply.json().get('parentID')
                if parent in self.sessions:
                    self.sessions.update(visited)
                    return True
                if not isinstance(parent, str) or not re.fullmatch(r'ses[A-Za-z0-9_-]{1,128}', parent):
                    return False
                cursor = parent
            return False

        @app.api_route('/{path:path}', methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'])
        async def forward(request: Request, path: str):
            path = '/' + path
            match = re.match(r'^/(?:api/|experimental/)?session/(ses[^/]+)(?:/|$)', path)
            if match and not await belongs(match[1]):
                return JSONResponse({'name': 'InvalidRequestError', 'data': {'message': '请从 Kaus 打开对应会话'}}, status_code=403)
            mutating = request.method in ('POST', 'PATCH', 'PUT', 'DELETE')
            if mutating and (path in ('/session', '/api/session', '/global/config', '/config', '/config/overlay', '/experimental/control-plane/move-session') or path == '/mcp' or path.startswith('/mcp/')):
                return JSONResponse({'name': 'InvalidRequestError', 'data': {'message': '请在 Kaus 中修改配置'}}, status_code=403)
            if request.method == 'GET' and path in ('/session', '/api/session', '/experimental/session'):
                reply = await self.client.get(path)
                reply.raise_for_status()
                rows = reply.json()
                if isinstance(rows, list):
                    return JSONResponse([r for r in rows if r.get('id') in self.sessions])
                return JSONResponse({'data': [r for r in rows.get('data', []) if r.get('id') in self.sessions]})
            data = await request.body()
            if len(data) > 16 * 1024 * 1024:
                return JSONResponse({'name': 'InvalidRequestError', 'data': {'message': '请求过大'}}, status_code=413)
            try:
                if request.method == 'POST' and re.fullmatch(r'/(?:api/)?session/[^/]+/(command|prompt|prompt_async|message)', path):
                    await self.check(path, json.loads(data))
                if request.method == 'GET' and path in ('/command', '/api/command') and self.allowed is not None:
                    rows = await self.commands()
                    allowed = {row['name'] for row in rows if self.admitted(row)}
                    if path == '/api/command':
                        response = await self.client.get(path)
                        response.raise_for_status()
                        result = response.json()
                        return JSONResponse({**result, 'data': [row for row in result.get('data', []) if row.get('name') in allowed]})
                    return JSONResponse([row for row in rows if row.get('name') in allowed])
            except PermissionError as exc:
                return JSONResponse({'name': 'InvalidRequestError', 'data': {'message': str(exc)}}, status_code=403)
            except (ValueError, TypeError):
                return JSONResponse({'name': 'InvalidRequestError', 'data': {'message': '命令格式无效'}}, status_code=400)
            # Fixed upstream, never an arbitrary URL supplied by the TUI.
            # Native HTTP can switch workspaces using either query or headers.
            # Keep the one workspace selected by this terminal lease.
            query = [(key, value) for key, value in parse_qsl(request.url.query, keep_blank_values=True)
                if key not in ('directory', 'workspace', 'workspaceID')]
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ('host', 'authorization', 'connection', 'content-length', 'accept-encoding', 'x-opencode-directory', 'x-opencode-workspace', 'x-kilo-directory', 'x-kilo-workspace')}
            upstream = await self.client.send(self.client.build_request(request.method, path, params=query, content=data, headers=headers), stream=True)
            if request.method == 'GET' and path == '/session/' + self.session_id + '/message' and not self.restored and self.selections:
                # Native attach restores composer options from its last user
                # envelope, ignoring the newer session selection. Project only
                # that transient envelope; the native transcript is untouched.
                payload = json.loads(await upstream.aread())
                await upstream.aclose()
                if isinstance(payload, list):
                    for row in reversed(payload):
                        info = row.get('info', {})
                        if info.get('role') != 'user':
                            continue
                        if self.selections.get('model'):
                            provider, _, model = self.selections['model'].partition('/')
                            info['model'] = {'providerID': provider, 'modelID': model}
                        if self.selections.get('variant') == 'default':
                            info.pop('variant', None)
                            if isinstance(info.get('model'), dict):
                                info['model'].pop('variant', None)
                        elif self.selections.get('variant'):
                            info['variant'] = self.selections['variant']
                            if isinstance(info.get('model'), dict):
                                info['model']['variant'] = self.selections['variant']
                        if self.selections.get('agent'):
                            info['agent'] = self.selections['agent']
                        break
                    self.restored = True
                return JSONResponse(payload, status_code=upstream.status_code)
            headers = {k: v for k, v in upstream.headers.items() if k.lower() not in ('connection', 'transfer-encoding', 'content-encoding', 'content-length')}
            return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code, headers=headers, background=BackgroundTask(upstream.aclose))
        return app


async def prepare(client, data):
    response = await client.get('/session/' + data['sessionId'])
    response.raise_for_status()
    session = response.json()
    if session.get('id') != data['sessionId'] or Path(session.get('directory', '')).resolve() != Path(data['cwd']).resolve():
        raise ValueError('native session identity mismatch')
    current = session.get('model') or {}
    if data.get('model'):
        provider, separator, model = data['model'].partition('/')
        if not separator:
            raise ValueError('native model requires provider')
        current = {'id': model, 'providerID': provider}
    if data.get('variant') == 'default':
        current.pop('variant', None)
    elif data.get('variant'):
        current['variant'] = data['variant']
    if current:
        response = await client.post('/api/session/' + data['sessionId'] + '/model', json={'model': current})
        response.raise_for_status()
    if data.get('agent'):
        response = await client.post('/api/session/' + data['sessionId'] + '/agent', json={'agent': data['agent']})
        response.raise_for_status()
    if data.get('approval'):
        rules = [{'permission': '*', 'pattern': '*', 'action': data['approval']}]
        if data.get('allowedSkills') is not None:
            rules += [{'permission': 'skill', 'pattern': '*', 'action': 'deny'}]
            rules += [{'permission': 'skill', 'pattern': name, 'action': 'allow'} for name in data['allowedSkills']]
        response = await client.patch('/session/' + data['sessionId'], json={'permission': rules})
        response.raise_for_status()
    names = set()
    for server in data['mcpServers']:
        if server['name'] in names:
            raise ValueError('duplicate MCP name')
        names.add(server['name'])
        response = await client.post('/mcp', json={'name': server['name'], 'config': mcp_config(server)})
        response.raise_for_status()
    if names:
        response = await client.get('/mcp')
        response.raise_for_status()
        states = response.json()
        if any(states.get(name, {}).get('status') != 'connected' for name in names):
            raise ValueError('native MCP connection failed')


async def serve(data, *, on_ready=None):
    import httpx
    import uvicorn
    env = environment(data, os.environ)
    prefix = data['engine'].upper()
    version = await asyncio.create_subprocess_exec(data['executable'], '--version', env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await asyncio.wait_for(version.communicate(), 15)
    if version.returncode or output.decode().strip() != VERSIONS[data['engine']]:
        raise ValueError('native terminal version requires verification')
    # Native serve does not accept inherited sockets. Bind a loopback ephemeral
    # port, then require its authenticated health endpoint before attaching.
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    server = await asyncio.create_subprocess_exec(data['executable'], 'serve', '--hostname', '127.0.0.1', '--port', str(port),
        env=env, cwd=data['cwd'], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    proxy_task = None
    try:
        register_owned_child(server.pid)
        async with httpx.AsyncClient(base_url='http://127.0.0.1:' + str(port), timeout=httpx.Timeout(60, read=None),
            auth=(data['engine'], env[prefix + '_SERVER_PASSWORD']), params={'directory': data['cwd']}, trust_env=False) as client:
            for _ in range(200):
                if server.returncode is not None:
                    raise ValueError('native server exited')
                try:
                    response = await client.get('/global/health', timeout=.5)
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(.1)
            else:
                raise ValueError('native server did not start')
            await prepare(client, data)
            async def inventory():
                from drivers.acp.client import AcpAgentSpec
                from drivers.skill_execution.opencode import inspect
                spec = AcpAgentSpec(command=(data['executable'],), env=env, inherit_env=False, cwd=data['cwd'])
                return (await inspect(data['executable'], spec, data['cwd'], 'skill'),
                        await inspect(data['executable'], spec, data['cwd'], 'config'))
            gate = CommandGate(client, data['allowedSkills'], data['sessionId'], inventory, data)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0)); listener.listen(128)
            proxy_port = listener.getsockname()[1]
            app = gate.app()
            # Authenticate the proxy too; a hostile web page must not be able
            # to bypass the native server's one-shot credential.
            import base64
            expected = 'Basic ' + base64.b64encode((data['engine'] + ':' + env[prefix + '_SERVER_PASSWORD']).encode()).decode()
            @app.middleware('http')
            async def authenticate(request, call_next):
                from fastapi.responses import Response
                if not secrets.compare_digest(request.headers.get('authorization', ''), expected):
                    return Response(status_code=401)
                return await call_next(request)
            class LocalServer(uvicorn.Server):
                @contextlib.contextmanager
                def capture_signals(self):
                    # The terminal supervisor owns SIGTERM/SIGHUP and must
                    # close the native process tree before releasing its lease.
                    yield
            proxy = LocalServer(uvicorn.Config(app, log_level='critical', access_log=False, lifespan='off', timeout_graceful_shutdown=2))
            proxy_task = asyncio.create_task(proxy.serve(sockets=[listener]))
            for _ in range(100):
                if proxy.started:
                    break
                await asyncio.sleep(.02)
            else:
                listener.close()
                raise ValueError('native terminal proxy did not start')
            if on_ready is not None:
                await on_ready('http://127.0.0.1:' + str(proxy_port), data['engine'], env[prefix + '_SERVER_PASSWORD'])
            try:
                return await run_terminal((data['executable'], 'attach', 'http://127.0.0.1:' + str(proxy_port),
                    '--dir', data['cwd'], '--session', data['sessionId']), cwd=data['cwd'], env=env)
            finally:
                proxy.should_exit = True
                await asyncio.wait_for(proxy_task, 10)
                listener.close()
    finally:
        if proxy_task and not proxy_task.done():
            proxy_task.cancel()
            await asyncio.gather(proxy_task, return_exceptions=True)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(server.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(server.wait(), 8)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(server.pid, signal.SIGKILL)
            await server.wait()


if __name__ == '__main__':
    async def run():
        import httpx
        try:
            data = json.loads(Path(sys.argv[1]).read_text())
            return await serve(data)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError, UnsupportedCapabilityError):
            print('终端配置无法恢复，请回到 Kaus 重试。', file=sys.stderr)
            return 1
    raise SystemExit(main(run))
