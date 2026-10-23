"""Run the native TUI against an owned local app-server with the same Skill roots.

No credentials are written. The private Unix socket, app-server and bootstrap
connection live only as long as this terminal; native history stays in CODEX_HOME.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile

from websockets.asyncio.client import unix_connect

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from drivers.terminals.managed_process import register_owned_child


async def request(connection, identity, method, params):
    await connection.send(json.dumps({'id': identity, 'method': method, 'params': params}))
    while True:
        response = json.loads(await asyncio.wait_for(connection.recv(), 20))
        if response.get('id') != identity:
            continue
        if 'error' in response:
            raise RuntimeError('原生终端未接受项目技能配置')
        return response.get('result', {})


async def configure(connection, root, cwd, thread_settings=None):
    await request(connection, 1, 'initialize', {'clientInfo': {'name': 'kaus-terminal', 'version': '1'},
                                                'capabilities': {'experimentalApi': True}})
    await connection.send('{"method":"initialized","params":{}}')
    await request(connection, 2, 'skills/extraRoots/set', {'extraRoots': [str(root / '.agents/skills')]})
    await verify(connection, root, cwd)
    if thread_settings:
        result = await request(connection, 4, 'thread/resume', thread_settings)
        for key in ('model', 'approvalPolicy', 'approvalsReviewer'):
            if key in thread_settings and result.get(key) != thread_settings[key]:
                raise RuntimeError('Codex 终端未接受当前会话设置')
        effort = (thread_settings.get('config') or {}).get('model_reasoning_effort')
        if effort and result.get('reasoningEffort') != effort:
            raise RuntimeError('Codex 终端未接受当前推理设置')


async def verify(connection, root, cwd):
    result = await request(connection, 3, 'skills/list', {'cwds': [cwd], 'forceReload': True})
    selected = {str(p.resolve()) for p in (root / '.agents/skills').glob('*/SKILL.md')}
    loaded = set()
    for row in result.get('data', []):
        if row.get('errors'):
            raise RuntimeError('原生终端技能清单不完整')
        for skill in row.get('skills', []):
            if skill.get('enabled'):
                loaded.add(str(Path(skill['path']).resolve()))
    if loaded != selected:
        raise RuntimeError('原生终端技能范围与对话不一致')


async def watch(connection, root):
    while True:
        await asyncio.sleep(.5)
        await verify(connection, root, os.getcwd())


async def stop_owned_process(process):
    if process is None:
        return
    # Launchers may spawn the native binary rather than exec it. Stopping only
    # the launcher leaves the app-server holding the session writer lease.
    def terminate_group(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may retain a group with only zombie members briefly.
            # Ignore EPERM only after verifying there is no live member.
            groups = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True)
            live = any(parts[0] == str(process.pid) and not parts[1].startswith('Z')
                       for row in groups.splitlines() if len(parts := row.split()) == 2)
            if sys.platform != 'darwin' or live:
                raise
    terminate_group(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        pass
    # The launcher can exit before its child. Reap the whole owned group.
    terminate_group(signal.SIGKILL)
    await process.wait()


async def cleanup_owned_processes(child, server, waiting):
    results = await asyncio.gather(
        stop_owned_process(child), stop_owned_process(server), return_exceptions=True)
    if waiting is not None:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def run(launcher, root, command, *, ready=None, thread_settings=None):
    # macOS Unix socket paths are short; TMPDIR often exceeds their limit.
    with tempfile.TemporaryDirectory(prefix='kaus-cli-', dir='/tmp') as directory:
        socket = str(Path(directory) / 'app.sock')
        endpoint = 'unix://' + socket
        server = await asyncio.create_subprocess_exec(launcher, 'app-server', '--listen', endpoint,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        child = None
        guard = None
        waiting = None
        try:
            register_owned_child(server.pid)
            for _ in range(200):
                if Path(socket).exists():
                    break
                if server.returncode is not None:
                    raise RuntimeError('原生终端服务启动失败')
                await asyncio.sleep(.05)
            async with unix_connect(socket, uri='ws://localhost', open_timeout=10) as connection:
                await configure(connection, Path(root), os.getcwd(), thread_settings)
                if ready:
                    return await ready(endpoint)
                child = await asyncio.create_subprocess_exec(launcher, '--remote', endpoint, *command,
                                                            start_new_session=True)
                register_owned_child(child.pid)
                def resize():
                    try:
                        os.killpg(child.pid, signal.SIGWINCH)
                    except ProcessLookupError:
                        pass
                asyncio.get_running_loop().add_signal_handler(signal.SIGWINCH, resize)
                guard = asyncio.create_task(watch(connection, Path(root)))
                waiting = asyncio.create_task(child.wait())
                done, _ = await asyncio.wait((guard, waiting), return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    guard.result()
                return await waiting
        finally:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGWINCH)
            async def cleanup():
                if guard is not None:
                    guard.cancel()
                    await asyncio.gather(guard, return_exceptions=True)
                await cleanup_owned_processes(child, server, waiting)
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise


async def run_cli(launcher, root, command, *, thread_settings=None):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    stopped_by = None

    def stop(signum):
        nonlocal stopped_by
        if stopped_by is None:
            stopped_by = signum
            task.cancel()

    signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    for signum in signals:
        loop.add_signal_handler(signum, stop, signum)
    try:
        return await run(launcher, root, command, thread_settings=thread_settings)
    except asyncio.CancelledError:
        if stopped_by is None:
            raise
        return 128 + stopped_by
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--launcher', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--thread-settings', type=json.loads)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    command = options.command[1:] if options.command[:1] == ['--'] else options.command
    try:
        raise SystemExit(asyncio.run(run_cli(options.launcher, options.root, command,
                                            thread_settings=options.thread_settings)))
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        parser.exit(1, str(exc) + '\n')
