"""Map Cursor's two native surfaces to one exact native store."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import UUID

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from .native_catalog import query


def config_root(spec):
    env = spec.build_env()
    root = env.get('CURSOR_CONFIG_DIR')
    if not root:
        root = str(Path(env['XDG_CONFIG_HOME'])/'cursor') if env.get('XDG_CONFIG_HOME') else str(Path(env.get('HOME') or Path.home())/'.cursor')
    return Path(root).expanduser().resolve()


def session_paths(spec, sid, cwd):
    sid = str(UUID(sid))
    # Native path.resolve preserves a workspace symlink's spelling.
    workspace = os.path.abspath(cwd)
    root = config_root(spec)
    native = root/'acp-sessions'/sid
    terminal = root/'chats'/hashlib.md5(workspace.encode(), usedforsecurity=False).hexdigest()/sid
    return native, terminal, workspace


def checked_store(spec, sid, cwd):
    native, terminal, workspace = session_paths(spec,sid,cwd)
    meta = native/'meta.json'
    if not meta.is_file() or os.path.abspath(json.loads(meta.read_text()).get('cwd','')) != workspace:
        raise UnsupportedCapabilityError('Cursor 会话与当前工作区不一致')
    database = native/'store.db'
    if not database.is_file():
        raise UnsupportedCapabilityError('Cursor 原生会话尚未保存')
    return native, terminal, database


def bridge_store(spec, sid, cwd):
    native, terminal, database = checked_store(spec,sid,cwd)
    terminal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A directory link includes SQLite's WAL/SHM files. Linking only store.db
    # would split a transaction across two directories and risk lost updates.
    try:
        terminal.symlink_to(native.resolve(), target_is_directory=True)
    except FileExistsError:
        if not terminal.is_symlink() or terminal.resolve()!=native.resolve():
            raise UnsupportedCapabilityError('Cursor 已存在另一份同名终端记录') from None
    return database


async def load_native_history(spec, binding, sid):
    from .cursor import MODULE_HASHES, package_path
    package = package_path(spec)
    if not package:
        raise UnsupportedCapabilityError('当前 Cursor 版本需要适配')
    try:
        _, _, database = checked_store(spec,sid,binding.runtime_config.get('workspace_root') or spec.cwd)
        # Native decoder opens a database read/write. Give it a consistent,
        # disposable SQLite snapshot; the live native store remains read-only.
        with tempfile.TemporaryDirectory(prefix='kaus-cursor-read-') as folder:
            snapshot = Path(folder)/'store.db'
            def backup():
                with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2) as src:
                    with sqlite3.connect(snapshot) as dst: src.backup(dst)
            await asyncio.to_thread(backup)
            payload = {'package':str(package),'modules':MODULE_HASHES,'database':str(snapshot),'id':sid}
            data = json.loads(await query(spec, (str(package/'node'),str(Path(__file__).with_name('cursor_history.cjs')),json.dumps(payload))))
        return NativeHistory(native_session_id=sid, entries=tuple(NativeHistoryEntry(**row) for row in data['entries']),
                             complete=not data['missing'],missing=tuple(data['missing']))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise RuntimeError('无法读取 Cursor 原生记录，请重试') from None


async def prepare_terminal_launch(command, spec, metadata, *, conversation, binding):
    from .cursor import MODULE_HASHES, immutable, package_path
    from .common import state_root
    package = package_path(spec)
    if not package or any(hashlib.sha256((package/name).read_bytes()).hexdigest()!=digest for name,digest in MODULE_HASHES.items()):
        raise UnsupportedCapabilityError('当前 Cursor 终端版本需要适配')
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    bridge_store(spec, conversation.native_session_id, cwd)
    servers = {}
    for server in metadata.get('mcpServers',[]):
        if not server.get('type'):
            servers[server['name']] = {'command':server['command'],'args':server.get('args',[]),
                'env':{row['name']:row['value'] for row in server.get('env',[])}}
        elif server['type']=='http':
            servers[server['name']] = {'url':server['url'],
                'headers':{row['name']:row['value'] for row in server.get('headers',[])}}
        else: raise UnsupportedCapabilityError('Cursor 终端尚未支持此 MCP 连接类型')
    if servers:
        # Reuse the Skill guard to avoid stacking two native compile hooks.
        if not spec.env.get('KAUS_CURSOR_SKILL_SCOPE'):
            root=state_root(spec)/'cursor-terminal'; root.mkdir(parents=True,exist_ok=True,mode=0o700)
            source=Path(__file__).with_name('cursor_guard.cjs').read_text()
            guard=root/('guard-'+hashlib.sha256(source.encode()).hexdigest()[:24]+'.cjs');immutable(guard,source)
            data=json.dumps({'modules':{str(package/name):digest for name,digest in MODULE_HASHES.items()},'paths':None},sort_keys=True)
            manifest=root/('scope-'+hashlib.sha256(data.encode()).hexdigest()[:24]+'.json');immutable(manifest,data)
            spec=spec.with_env(KAUS_CURSOR_SKILL_SCOPE=str(manifest),
                NODE_OPTIONS=(spec.build_env().get('NODE_OPTIONS','')+' --require='+json.dumps(str(guard),ensure_ascii=False)).strip())
        spec=spec.with_env(KAUS_CURSOR_TERMINAL_MCP=json.dumps({'path':str(Path(cwd)/'.cursor/mcp.json'),'servers':servers}))
    return command, spec
