"""Recover remote Card reservations through configured drivers, never saved auth."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import weakref

from . import lease as lease_module

_resolvers: list = []
_monitors: dict[str, asyncio.Task] = {}
POLL_SECONDS = 1.0


def register_resolver(resolver):
    # Hosts own driver lifetimes; recovery must not keep discarded hosts alive.
    _resolvers[:] = [item for item in _resolvers if item() is not None]
    _resolvers.append(weakref.WeakMethod(resolver))


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 16384:
                return None
            value = json.load(stream)
        if (not isinstance(value, dict) or value.get("kind") != "card"
                or not isinstance(value.get("launchId"), str)
                or not isinstance(value.get("reference"), dict)
                or not isinstance(value.get("resource"), str)):
            return None
        return value
    except (OSError, ValueError):
        return None


def _local_writer_active(value):
    from drivers.terminals.managed_process import process_identity, _group_active
    owner = value.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return True
    if process_identity(owner["pid"]) == owner:
        return True
    transport = value.get("transport")
    if transport is not None:
        if (not isinstance(transport, dict) or transport.get("uid") != os.getuid()
                or transport.get("pid") != transport.get("pgid")):
            return True
        if _group_active(transport["pgid"]):
            return True
    return False


async def _scan(root):
    pending = []
    if not root.exists() or root.is_symlink():
        return pending
    for path in root.glob("*.external.json"):
        value = await asyncio.to_thread(_read, path)
        if value is None:
            continue
        resource = value["resource"]
        try:
            lease = lease_module.ResourceLease(resource, root=root, timeout=0)
            if lease.reservation_path != path:
                continue
            if await asyncio.to_thread(_local_writer_active, value):
                pending.append(resource)
                continue
            confirmed = value.get("phase") == "prepared"
            for weak in (() if confirmed else reversed(_resolvers)):
                resolver = weak()
                if resolver is None:
                    continue
                try:
                    confirmed = await asyncio.wait_for(resolver(value["reference"]), timeout=8) is True
                except Exception:
                    confirmed = False
                if confirmed:
                    break
            if confirmed:
                await lease.clear_external(value["launchId"])
            else:
                pending.append(resource)
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            pending.append(resource)
    return pending


async def reconcile_persisted():
    root = Path(lease_module.default_lease_root())
    pending = await _scan(root)
    identity = str(root)
    if pending and (identity not in _monitors or _monitors[identity].done()):
        async def monitor():
            try:
                while True:
                    await asyncio.sleep(POLL_SECONDS)
                    if not await _scan(root):
                        return
            finally:
                if _monitors.get(identity) is asyncio.current_task():
                    _monitors.pop(identity, None)
        _monitors[identity] = asyncio.create_task(monitor())
    return tuple(pending)
