"""Owned terminal handoff records for adapters with private native session stores."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import uuid

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import state_root
from .terminal_settings import immutable


def options_path(spec, conversation):
    key = str(conversation.id) + '\0' + str(conversation.agent_binding_id)
    directory = state_root(spec) / 'terminal-options' / hashlib.sha256(key.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录不可用')
    return directory / 'current.json'


def remember(spec, conversation, metadata):
    value = {'mcpServers': (metadata or {}).get('mcpServers', [])}
    path = options_path(spec, conversation)
    if path.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录不可用')
    atomic_write_text(path, json.dumps(value, sort_keys=True))
    path.chmod(0o600)


def record(spec, conversation, binding, storage_name):
    value = session_record(spec, binding, conversation.native_session_id, storage_name)
    try:
        options = json.loads(options_path(spec, conversation).read_text())
        if not isinstance(options.get('mcpServers'), list):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise UnsupportedCapabilityError('终端配置不可用，请重新载入对话') from None
    return {**value, 'mcpServers': options['mcpServers']}


def session_record(spec, binding, ident, storage_name):
    try:
        if str(uuid.UUID(ident)) != ident:
            raise ValueError
        args = spec.command
        root = (Path(args[args.index('--state-dir') + 1]) if '--state-dir' in args else
                Path(spec.build_env().get('HOME', str(Path.home()))) / '.local/share/kaus' / storage_name)
        root = root.expanduser().resolve()
        directory = root / ident
        metadata = directory / 'session.json'
        if directory.is_symlink() or metadata.is_symlink():
            raise ValueError
        session = json.loads(metadata.read_text())
        cwd = (binding.runtime_config.get('workspace_root') if binding else None) or spec.cwd or os.getcwd()
        if session['cwd'] != str(Path(cwd).resolve()) or not isinstance(session['native_id'], str) or not session['native_id']:
            raise ValueError
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise UnsupportedCapabilityError('终端会话或配置不可用，请重新载入对话') from None
    return {'directory': str(directory), 'bridgeId': ident, 'nativeId': session['native_id'],
            'cwd': session['cwd']}


def manifest(spec, conversation, value):
    return immutable(spec, conversation, json.dumps(value, sort_keys=True), suffix='.json')


async def run_terminal(command, *, cwd, env=None):
    """Keep the foreground TUI in this PTY; reap it before closing its server."""
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env)
    try:
        return await process.wait()
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def main(run):
    async def guarded():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        return await run()
    try:
        return asyncio.run(guarded())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
