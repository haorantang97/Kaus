"""Verify ACP-added MCP servers using the running native process's SDK.

The native ACP implementations ignore registration errors. Their plugin SDK
exposes the actual process-local MCP state, including dynamically added servers.
Only names and normalized states cross this short-lived private control channel.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import state_root

CONTROL_ENV = "KAUS_NATIVE_MCP_CONTROL"
PLUGIN_ENV = "KAUS_NATIVE_MCP_PLUGIN"
STATUS_TIMEOUT = 8.0


def _names(metadata):
    servers = metadata.get("mcpServers", [])
    if not isinstance(servers, list):
        raise UnsupportedCapabilityError("MCP 配置格式无效")
    names = [server.get("name") if isinstance(server, dict) else None for server in servers]
    if any(not isinstance(name, str) or not name or len(name) > 1024 for name in names):
        raise UnsupportedCapabilityError("MCP 名称无效")
    if len(names) != len(set(names)):
        raise UnsupportedCapabilityError("MCP 名称重复")
    return names


async def prepare(spec, metadata, *, prefix):
    names = _names(metadata)
    env = spec.build_env()
    old_plugin = env.get(PLUGIN_ENV)
    if not names and not old_plugin:
        return spec
    key = prefix + "_CONFIG_CONTENT"
    try:
        config = json.loads(env.get(key) or "{}")
        if not isinstance(config, dict) or not isinstance(config.get("plugin", []), list):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise UnsupportedCapabilityError("原生 MCP 启动配置无效") from exc
    plugins = [value for value in config.get("plugin", []) if value != old_plugin]
    if not names:
        config["plugin"] = plugins
        return spec.with_env(**{key: json.dumps(config), CONTROL_ENV: "", PLUGIN_ENV: ""})
    root = state_root(spec) / "native-mcp-status"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise UnsupportedCapabilityError("MCP 状态目录无效")
    source = Path(__file__).with_suffix(".mjs").read_text()
    plugin = root / ("status-" + hashlib.sha256(source.encode()).hexdigest()[:24] + ".mjs")
    if plugin.exists():
        if plugin.is_symlink() or plugin.read_text() != source:
            raise UnsupportedCapabilityError("MCP 状态插件校验失败")
    else:
        atomic_write_text(plugin, source)
    # A fresh directory per native process prevents concurrent sessions and cold
    # starts from accepting another runtime's receipt. Never remove the old one
    # here: a separately running conversation may still be using it.
    # Create it only after session/new or resume returns. Failed initialization
    # therefore leaves no per-runtime directory needing a host cleanup hook.
    control = root / ("run-" + secrets.token_hex(24))
    config["plugin"] = [*plugins, plugin.as_uri()]
    return spec.with_env(**{key: json.dumps(config), CONTROL_ENV: str(control), PLUGIN_ENV: plugin.as_uri()})


def _read_receipt(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
            raise ValueError("invalid receipt")
        with os.fdopen(fd, "r") as handle:
            fd = None
            return json.load(handle)
    finally:
        if fd is not None:
            os.close(fd)


def _label(name):
    return "".join(char for char in name if char.isprintable())[:120]


async def verify(spec, metadata, session_id):
    names = _names(metadata)
    if not names:
        return
    raw = spec.build_env().get(CONTROL_ENV)
    if not raw:
        raise UnsupportedCapabilityError("无法核验原生 MCP 状态")
    control = Path(raw)
    try:
        control.mkdir(mode=0o700)
    except OSError as exc:
        raise UnsupportedCapabilityError("MCP 状态目录无效") from exc
    nonce = secrets.token_hex(24)
    request = {"schema": 1, "nonce": nonce, "sessionId": session_id, "names": names}
    try:
        atomic_write_text(control / "request.json", json.dumps(request))
        (control / "request.json").chmod(0o600)
        deadline = asyncio.get_running_loop().time() + STATUS_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            try:
                receipt = _read_receipt(control / "response.json")
            except (OSError, ValueError, TypeError) as exc:
                raise UnsupportedCapabilityError("原生 MCP 状态回执无效") from exc
            if receipt is not None:
                if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                        or receipt.get("nonce") != nonce or receipt.get("sessionId") != session_id):
                    raise UnsupportedCapabilityError("原生 MCP 状态回执不匹配")
                states = receipt.get("states")
                if (not isinstance(states, dict) or set(states) != set(names)
                        or any(not isinstance(value, str) for value in states.values())):
                    raise UnsupportedCapabilityError("原生 MCP 状态不完整")
                failed = [name for name in names if states[name] != "connected"]
                if failed:
                    auth = all(states[name] in {"needs_auth", "needs_client_registration"} for name in failed)
                    title = "MCP 需要授权：" if auth else "MCP 连接失败："
                    raise UnsupportedCapabilityError(title + "、".join(_label(name) for name in failed[:3]))
                return
            await asyncio.sleep(0.05)
        raise UnsupportedCapabilityError("原生 MCP 状态校验超时")
    finally:
        shutil.rmtree(control, ignore_errors=True)
