"""Original client for Crush's public workspace Skill catalog.

Native discovery is frozen per workspace. Inspect in an uninitialized workspace,
then create the real workspace with its private exclusion configuration. Verify
the resulting catalog before agent initialization and before each turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import tempfile
import uuid

from .protocol import BridgeError
from .transport import NativeHTTP


def write_config(directory, paths, disabled):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'crush.json'
    document = json.loads(path.read_text()) if path.exists() else {}
    options = document.setdefault('options', {})
    options['skills_paths'] = list(dict.fromkeys(str(Path(p).parent) for p in paths))
    options['disabled_skills'] = disabled
    temporary = directory / 'crush.skill-scope.tmp'
    temporary.write_text(json.dumps(document))
    temporary.chmod(0o600)
    temporary.replace(path)


async def inspect(command, cwd, env, paths, *, include_builtin_content=False):
    with tempfile.TemporaryDirectory(prefix='kaus-crush-scope-', dir='/tmp') as temporary:
        root = Path(temporary)
        write_config(root / 'data', paths, [])
        socket = str(root / 'agent.sock')
        process = await asyncio.create_subprocess_exec(command, 'server', '--host', 'unix://' + socket,
            env=env, cwd=cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        client = NativeHTTP(socket)
        try:
            for _ in range(150):
                if process.returncode is not None:
                    raise BridgeError('Crush 原生技能目录启动失败')
                try:
                    await client.request('GET', '/v1/health', timeout=.3)
                    break
                except (OSError, http.client.HTTPException):
                    await asyncio.sleep(.1)
            else:
                raise BridgeError('Crush 原生技能目录检测超时')
            version = await client.request('GET', '/v1/version')
            if str(version.get('version', '')).lstrip('v') != '0.96.1':
                raise BridgeError('当前 Crush 版本的技能冻结行为尚未验证')
            workspace = await client.request('POST', '/v1/workspaces', {
                'path': str(cwd), 'data_dir': str(root / 'data'), 'client_id': str(uuid.uuid4()), 'yolo': False})
            rows = await client.request('GET', '/v1/workspaces/' + workspace['id'] + '/skills')
            if not isinstance(rows, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get('id'), str) or not isinstance(row.get('name'), str) for row in rows):
                raise BridgeError('Crush 原生技能目录格式不兼容')
            if include_builtin_content:
                for row in rows:
                    if row['id'].startswith('crush://'):
                        row['content'] = (await client.request('POST', '/v1/workspaces/' + workspace['id'] + '/skills/read',
                            {'skill_id': row['id']})).get('content')
            return rows
        finally:
            await client.aclose()
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()


def read_scope(command):
    path = os.environ.get('KAUS_CRUSH_SKILL_SCOPE')
    if not path:
        return None
    scope = json.loads(Path(path).read_text())
    native = Path(command).resolve()
    if str(native) != scope['executable'] or hashlib.sha256(native.read_bytes()).hexdigest() != scope['binaryHash']:
        raise BridgeError('Crush 版本已变化，需要重新应用项目技能')
    return scope


async def prepare(command, cwd, directory):
    scope = read_scope(command)
    if scope is None:
        return None
    rows = await inspect(command, cwd, dict(os.environ), scope['paths'])
    selected = set(scope['paths'])
    if not selected.issubset({row['id'] for row in rows}):
        raise BridgeError('Crush 未加载完整的项目技能')
    # Native Filter matches names. Selected copies carry stable unique names;
    # the subsequent exact path check rejects any same-name shadowing.
    disabled = [row['name'] for row in rows if row['id'] not in selected]
    write_config(directory / 'data', scope['paths'], disabled)
    return scope


async def validate(session):
    scope = getattr(session, 'skill_scope', None)
    if scope is None:
        return
    rows = await session.request('GET', session.base + '/skills')
    if not isinstance(rows, list) or {row.get('id') for row in rows} != set(scope['paths']):
        raise BridgeError('Crush 技能集合已变化，请重新打开此会话')
