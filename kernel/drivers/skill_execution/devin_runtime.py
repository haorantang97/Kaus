"""Confirm project MCP startup in the owning Devin ACP process.

Devin 3000.11.3 accepts session/new even when an MCP server fails to
initialize. Its native connection lifecycle is reported on the ACP output
extension. Observe that lifecycle around new/load; never run a second MCP
client or treat successful session creation as successful MCP startup.

This is a runtime adapter only. Devin has no verified exclusive Skill loader
contract, so it must not advertise project Skill projection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from dataclasses import dataclass, field
from typing import Callable
from weakref import WeakKeyDictionary

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import state_root

STATUS_TIMEOUT = 12.0
CONFIG_TIMEOUT = 8.0


def _private_cache(spec, conversation):
    owner = str(getattr(conversation, 'id', 'probe')) + '\0' + str(getattr(conversation, 'agent_binding_id', ''))
    root = state_root(spec)
    cache = root / 'devin-mcp-cache' / hashlib.sha256(owner.encode()).hexdigest()
    descriptions = cache / 'devin/cli/mcp/descriptions.json'
    try:
        for path in (root, *reversed(descriptions.parents)):
            if path == root or root in path.parents:
                if path.is_symlink():
                    raise ValueError
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if descriptions.is_symlink():
            raise ValueError
        if descriptions.exists():
            if descriptions.stat().st_size > 4 * 1024 * 1024:
                raise ValueError
            value = json.loads(descriptions.read_text())
            if not isinstance(value, dict):
                raise ValueError
            # Devin otherwise treats a cached name as a lazily connected server,
            # including after its command changes. In our own cache only, evict
            # Kaus names so the owning process verifies the current connection.
            remaining = {key: item for key, item in value.items() if not key.startswith('kaus_')}
            if remaining != value:
                atomic_write_text(descriptions, json.dumps(remaining))
    except (OSError, ValueError, TypeError):
        raise UnsupportedCapabilityError('Devin 的项目 MCP 缓存不可用，请重新连接') from None
    return spec.with_env(XDG_CACHE_HOME=str(cache))


def _names(metadata):
    servers = metadata.get("mcpServers", [])
    if not isinstance(servers, list):
        raise UnsupportedCapabilityError("MCP 配置无效")
    names = [server.get("name") if isinstance(server, dict) else None for server in servers]
    if any(not isinstance(name, str) or not name or len(name) > 1024
           or any(not char.isprintable() for char in name) for name in names):
        raise UnsupportedCapabilityError("MCP 名称无效")
    if len(names) != len(set(names)):
        raise UnsupportedCapabilityError("MCP 名称重复")
    return names


async def _native_name_exists(spec, cwd, name):
    command = tuple(spec.command)
    if "acp" not in command:
        raise UnsupportedCapabilityError("无法核验 Devin 的 MCP 配置")
    command = (*command[:command.index("acp")], "mcp", "get", name)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=cwd,
            env=spec.build_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix")
        async with asyncio.timeout(CONFIG_TIMEOUT):
            stdout, stderr, code = await asyncio.gather(process.stdout.read(65537),
                process.stderr.read(65537), process.wait())
        if len(stdout) > 65536 or len(stderr) > 65536:
            raise ValueError
        if code == 0:
            return True
        if code == 1 and stderr.decode("utf-8").strip() == f"Error: Server '{name}' not found":
            return False
        raise ValueError
    except (OSError, ValueError, UnicodeError, TimeoutError):
        raise UnsupportedCapabilityError("无法核验 Devin 的 MCP 配置，请检查引擎状态") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def prepare_runtime_spec(spec, conversation, metadata):
    names = _names(metadata)
    if not names:
        return spec
    aliases = metadata.get("_kausDevinMcpAliases")
    if not isinstance(aliases, dict) or set(aliases) != set(names):
        owner = str(getattr(conversation, "id", "probe"))
        aliases, servers = {}, []
        for server in metadata["mcpServers"]:
            name = server["name"]
            scoped = "kaus_" + hashlib.sha256((owner + "\0" + name).encode()).hexdigest()[:32]
            aliases[scoped] = name
            servers.append({**server, "name": scoped})
        metadata["mcpServers"] = servers
        metadata["_kausDevinMcpAliases"] = aliases
        names = list(aliases)
    # ACP appends servers, but a native same-name server wins. Stable private
    # names avoid replacing the user's native configuration. Verify those names
    # through the native resolver too; a collision must never select a different
    # command or environment silently.
    limit = asyncio.Semaphore(4)
    async def check(name):
        async with limit:
            if await _native_name_exists(spec, metadata.get("_kausWorkspaceRoot") or spec.cwd, name):
                raise UnsupportedCapabilityError("Devin 的项目 MCP 连接名称冲突：" + aliases[name][:60])
    tasks = [asyncio.create_task(check(name)) for name in names]
    try:
        async with asyncio.timeout(CONFIG_TIMEOUT):
            await asyncio.gather(*tasks)
    except TimeoutError:
        raise UnsupportedCapabilityError("Devin 的 MCP 配置检查超时，请重试") from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return _private_cache(spec, conversation)


@dataclass
class _Startup:
    names: tuple[str, ...]
    previous: Callable | None
    states: dict[str, str] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def observe(self, method, params):
        if self.previous is not None:
            self.previous(method, params)
        if method != "_cognition.ai/output" or not isinstance(params, dict):
            return
        channel, message = params.get("channel"), params.get("message")
        # In the verified native build these process-level lifecycle events
        # precede the session. Agent messages and MCP stderr are separate streams.
        if not isinstance(message, str) or params.get("sessionId") != "":
            return
        for name in self.names:
            if channel != f"MCP: {name}":
                continue
            # Match the native lifecycle, not arbitrary mentions of a server.
            if (message == f"Connecting to MCP server '{name}'"
                    and params.get("level") == "info"):
                self.states[name] = "connecting"
            elif (message == f"MCP server '{name}' connected successfully"
                  and self.states.get(name) == "connecting"
                  and params.get("level") == "info"):
                self.states[name] = "connected"
            elif message.startswith(f"MCP server '{name}' connection failed:"):
                self.states[name] = "failed"
            else:
                continue
            self.changed.set()


_startups: WeakKeyDictionary = WeakKeyDictionary()


async def before_runtime_session(connection, spec, conversation, metadata):
    names = _names(metadata)
    if not names:
        return metadata
    startup = _Startup(tuple(names), connection.on_notification)
    _startups[connection] = startup
    connection.on_notification = startup.observe
    return metadata


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    names = _names(metadata)
    if not names:
        return
    startup = _startups.pop(connection, None)
    if startup is None:
        raise UnsupportedCapabilityError("无法确认 Devin 的 MCP 连接状态")
    try:
        async with asyncio.timeout(STATUS_TIMEOUT):
            while True:
                failed = [name for name in names if startup.states.get(name) == "failed"]
                if failed:
                    aliases = metadata.get("_kausDevinMcpAliases", {})
                    raise UnsupportedCapabilityError("MCP 连接失败：" + "、".join(aliases.get(name, name)[:60] for name in failed[:3]))
                if all(startup.states.get(name) == "connected" for name in names):
                    return
                startup.changed.clear()
                await startup.changed.wait()
    except TimeoutError:
        raise UnsupportedCapabilityError("Devin 未确认 MCP 连接，请检查服务配置") from None
    finally:
        # The host installs its normal runtime handler after this hook. Restore
        # the previous observer on failures/cancellation as well.
        if connection.on_notification == startup.observe:
            connection.on_notification = startup.previous
