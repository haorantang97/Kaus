"""Small, fixed macOS setup actions; never changes a permission grant."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from pathlib import Path
import shutil
import sys
import time

from .peekaboo_host import app_bundle as peekaboo_app_bundle

logger = logging.getLogger(__name__)

PERMISSION_PANES = {
    "screen-recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}
_HOSTS = {"cua-driver": "CuaDriver", "peekaboo": "Peekaboo"}
_NAVIGATION_REPLY_TIMEOUT = 5.0
_NAVIGATION_LIFETIME = 30.0
_PENDING: dict[tuple[str, ...], asyncio.Task[bool]] = {}


def setup_details(host, permissions):
    """Keep driver diagnostics and arbitrary paths out of the public UI."""
    if sys.platform != "darwin" or not isinstance(host, str) or host not in _HOSTS:
        return None
    if not isinstance(permissions, (list, tuple)):
        return None
    missing = [key for key in PERMISSION_PANES if key in permissions]
    if not missing:
        return None
    return {"host": host, "label": _HOSTS[host], "permissions": missing}


def native_setup(raw):
    value = raw.get("permissionSetup") if isinstance(raw, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    return setup_details(value.get("host"), value.get("permissions"))


async def _dispatch_open(arguments):
    # These arguments are fixed settings URLs or locally discovered tool paths.
    # Keep bounded diagnostics in developer logs, never in the API response.
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/open", *arguments,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.exception("macOS navigation could not launch: target=%r", arguments)
        raise
    diagnostic = bytearray()

    async def collect_errors():
        if (stream := getattr(process, "stderr", None)) is not None:
            while chunk := await stream.read(1024):
                diagnostic.extend(chunk[:max(0, 4096 - len(diagnostic))])

    reader = asyncio.create_task(collect_errors())
    try:
        await asyncio.wait_for(process.wait(), timeout=_NAVIGATION_LIFETIME)
        if process.returncode:
            await reader
            logger.warning("macOS navigation failed: exit=%s elapsed=%.3f target=%r stderr=%r",
                           process.returncode, time.monotonic() - started,
                           arguments, diagnostic.decode("utf-8", errors="replace"))
            raise RuntimeError("settings_open_failed")
        logger.info("macOS navigation completed: elapsed=%.3f target=%r",
                    time.monotonic() - started, arguments)
        return True
    except asyncio.TimeoutError:
        logger.warning("macOS navigation acknowledgement unavailable: elapsed=%.3f target=%r stderr=%r",
                       time.monotonic() - started, arguments,
                       diagnostic.decode("utf-8", errors="replace"))
        return False
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def _open(*arguments):
    """A dispatched navigation may open its pane before LaunchServices replies.

    Keep a bounded helper alive after the UI response deadline, share it with
    repeated clicks, and report pending rather than inventing a failed action.
    """
    key = tuple(arguments)
    current = _PENDING.get(key)
    if current is not None and not current.done():
        return False
    task = asyncio.create_task(_dispatch_open(key))
    _PENDING[key] = task

    def completed(result):
        if _PENDING.get(key) is result:
            _PENDING.pop(key, None)
        if not result.cancelled():
            result.exception()  # failures are already logged, including after HTTP returned

    task.add_done_callback(completed)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_NAVIGATION_REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        logger.info("macOS navigation awaiting acknowledgement: target=%r", key)
        return False


async def open_permission_settings(permission):
    # The API can open these two panes only. No supplied URL, executable or path.
    if permission not in PERMISSION_PANES:
        raise ValueError("unknown_permission")
    if sys.platform != "darwin":
        raise RuntimeError("platform_unavailable")
    return await _open(PERMISSION_PANES[permission])


async def reveal_permission_host(host):
    if host not in _HOSTS:
        raise ValueError("unknown_permission_host")
    if sys.platform != "darwin":
        raise RuntimeError("platform_unavailable")
    if host == "peekaboo" and (app := peekaboo_app_bundle()):
        return await _open("-R", str(app))
    if host == "cua-driver":
        from . import settings
        if app := settings.managed_bundle():
            return await _open("-R", str(app))
    command = shutil.which(host)
    if not command:
        raise RuntimeError("permission_host_unavailable")
    path = Path(command).resolve()
    if host == "cua-driver":
        # Reveal the signed bundle rather than its internal executable.
        path = next((parent for parent in path.parents if parent.suffix == ".app"), path)
    return await _open("-R", str(path))
