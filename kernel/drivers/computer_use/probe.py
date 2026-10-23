"""Bounded setup probes. No model requests or computer-operation tools are called."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import shutil
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .discovery import discover_fallbacks
from .policy import CAPABILITIES, Candidate
from . import peekaboo_host

_TOOLS = {
    "playwright": frozenset({"browser_navigate", "browser_snapshot", "browser_click"}),
    "peekaboo": frozenset({"see", "click", "type"}),
    "cua-driver": frozenset({"get_window_state", "click", "type_text"}),
}
_CACHE: dict[tuple, tuple] = {}


async def _list_tools(server: Mapping[str, Any], timeout: float) -> set[str]:
    # Reuse the same official MCP client already used by session_mcp/broker.py.
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async with asyncio.timeout(timeout):
        with open(os.devnull, "w") as errors:
            transport = stdio_client(StdioServerParameters(
                command=server["command"], args=list(server.get("args", ())),
                env={item["name"]: item["value"] for item in server.get("env", ())},
            ), errlog=errors)
            async with Client(transport, read_timeout_seconds=timeout) as client:
                names, cursor, seen = set(), None, set()
                for _ in range(16):
                    page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                    names.update(tool.name for tool in page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        return names
                    if cursor in seen:
                        raise ValueError("Invalid MCP tool pagination")
                    seen.add(cursor)
                raise ValueError("MCP tool catalog exceeds probe limit")


async def _permissions(command: str, timeout: float, *, socket: str | None = None) -> Mapping[str, Any]:
    # Documented read-only command, unlike `permissions grant/request`.
    # https://github.com/openclaw/Peekaboo/blob/main/docs/commands/permissions.md
    process = None
    try:
        async with asyncio.timeout(timeout):
            if socket:
                await peekaboo_host.start_app_host(socket, timeout)
            args = ["--bridge-socket", socket] if socket else []
            process = await asyncio.create_subprocess_exec(
                command, "permissions", "status", *args, "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdout is not None
            # Bounded memory even when a broken executable writes endlessly.
            data = bytearray()
            while chunk := await process.stdout.read(16 * 1024):
                data.extend(chunk)
                if len(data) > 128 * 1024:
                    raise ValueError("Permission status exceeds limit")
            await process.wait()
            if process.returncode:
                raise ValueError("Permission status unavailable")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Invalid permission status")
            if result.get("success") is False:
                raise ValueError("Permission status unavailable")
            if socket and result.get("data", {}).get("source") != "bridge":
                raise ValueError("Permission host unavailable")
            return result
    finally:
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def permission_requirements(value: Mapping[str, Any]) -> tuple[str, ...]:
    """Parse native permission booleans, never infer approval from an executable."""
    if "accessibility" in value or "screen_recording" in value:
        source = value.get("source", {})
        if not isinstance(source, Mapping) or source.get("attribution") != "driver-daemon" or source.get("bundle_id") != "com.trycua.driver":
            return ("permission-probe",)
        fields = {"screen-recording": "screen_recording", "accessibility": "accessibility"}
        missing = tuple(key for key, field in fields.items() if value.get(field) is False)
        unknown = any(not isinstance(value.get(field), bool) for field in fields.values())
        return missing + (("permission-probe",) if unknown else ())
    data = value.get("data", value)
    rows = data.get("permissions") if isinstance(data, Mapping) else None
    if not isinstance(rows, list) or not rows:
        return ("permission-probe",)
    states = {}
    names = {"screen recording": "screen-recording", "screen-recording": "screen-recording",
             "accessibility": "accessibility"}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = names.get(str(row.get("name", "")).strip().lower())
        if key and isinstance(row.get("isGranted"), bool):
            states[key] = row["isGranted"]
    required = ("screen-recording", "accessibility")
    missing = tuple(key for key in required if states.get(key) is False)
    return missing + (("permission-probe",) if any(key not in states for key in required) else ())


async def probe_fallbacks(
    *, context_id: str | None = None,
    capabilities: Sequence[str] = CAPABILITIES,
    which: Callable[[str], str | None] = shutil.which,
    platform: str | None = None,
    timeout: float = 8,
    list_tools: Callable[[Mapping[str, Any], float], Awaitable[set[str]]] | None = None,
    permissions: Callable[[str, float], Awaitable[Mapping[str, Any]]] | None = None,
    force: bool = False,
    cache_ttl: float = 60,
) -> tuple[Candidate, ...]:
    """Confirm setup for installed exact executables, without installing anything.

    A successful MCP initialize/tools-list marks the provider launch-ready, not
    live computer-use accepted. A browser executable may still need setup when
    the first browser operation launches it. Desktop also needs current required
    macOS permissions from its own execution host.
    """
    if timeout <= 0:
        raise ValueError("Invalid probe timeout")
    use_cache = list_tools is None and permissions is None and cache_ttl > 0
    list_tools = list_tools or _list_tools
    permission_probe = permissions
    rows = discover_fallbacks(context_id=context_id, which=which, platform=platform)

    async def probe(candidate: Candidate) -> Candidate:
        if candidate.mcp_server is None:
            return candidate
        key = None
        if use_cache:
            try:
                info = os.stat(candidate.mcp_server["command"])
                key = (candidate.provider_id, str(candidate.mcp_server["command"]),
                       tuple(candidate.mcp_server.get("args", ())), info.st_mtime_ns, info.st_size)
                cached = _CACHE.get(key)
                if not force and cached and time.monotonic() - cached[0] < cache_ttl:
                    return replace(candidate, status=cached[1], requirements=cached[2], verified=cached[3], system_permissions=cached[4])
            except OSError:
                pass

        def remember(result: Candidate) -> Candidate:
            if key is not None:
                now = time.monotonic()
                for stale in [item for item, value in _CACHE.items() if now - value[0] > cache_ttl]:
                    _CACHE.pop(stale, None)
                _CACHE[key] = (now, result.status, result.requirements, result.verified, result.system_permissions)
            return result

        stage = "permission-probe" if candidate.capability == "desktop" else "mcp-probe"
        try:
            if candidate.capability == "desktop":
                if candidate.provider_id == "cua-driver":
                    args = list(candidate.mcp_server.get("args", ()))
                    command = args[args.index("--driver") + 1]
                    status = await (permission_probe(command, timeout) if permission_probe else
                                    _cua_permissions(command, timeout))
                else:
                    command = str(candidate.mcp_server["command"])
                    status = (await permission_probe(command, timeout) if permission_probe else
                              await _permissions(command, timeout, socket=peekaboo_host.selected_socket(
                                  candidate.mcp_server.get("args", ()))))
                required = permission_requirements(status)
                if candidate.provider_id == "cua-driver" and "permission-probe" not in required:
                    states = (("accessibility", status["accessibility"]),
                              ("screen-recording", status["screen_recording"]))
                    candidate = replace(candidate, system_permissions=states)
                if required:
                    return remember(replace(candidate, status=("unknown" if "permission-probe" in required
                                                               else "needs_setup"), requirements=required))
            stage = "mcp-probe"
            tools = await list_tools(candidate.mcp_server, timeout)
            if not _TOOLS[candidate.provider_id].issubset(tools):
                return remember(replace(candidate, status="needs_setup", requirements=("mcp-tool-catalog",)))
            return remember(replace(candidate, status="ready", requirements=(), verified=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Probe diagnostics can contain paths or account data. The details UI
            # needs the actionable requirement, never raw stderr/exception text.
            return remember(replace(candidate, status=("unknown" if stage == "permission-probe" else "needs_setup"),
                                    requirements=(stage,)))

    return tuple(await asyncio.gather(*(probe(row) for row in rows if row.capability in capabilities)))


async def _cua_permissions(command: str, timeout: float):
    """Query the exact app daemon used by the desktop projection; never prompt."""
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    from drivers.session_mcp.stdio_compat import stdio_client
    from . import installer
    async with asyncio.timeout(timeout):
        socket = await installer.ensure_runtime_host(timeout=timeout, executable=command)
        with open(os.devnull, "w") as errors:
            transport = stdio_client(StdioServerParameters(command=command,
                args=["mcp", "--socket", os.fspath(socket)]), errlog=errors)
            async with Client(transport, read_timeout_seconds=timeout) as client:
                result = await client.call_tool("check_permissions", {"prompt": False},
                                                read_timeout_seconds=timeout)
                if result.is_error:
                    raise ValueError("Permission status unavailable")
                value = cua_permission_payload(result)
                from pathlib import Path
                reported = value.get("source", {}).get("executable")
                if not isinstance(reported, str) or Path(reported).resolve() != Path(command).resolve():
                    raise ValueError("Permission host identity mismatch")
                return value


def cua_permission_payload(result):
    # Official 0.37 returns human-readable content plus structuredContent.
    # Preserve compatibility with older JSON text without parsing presentation.
    value = getattr(result, "structured_content", None)
    if isinstance(value, dict):
        return value
    for content in result.content:
        if getattr(content, "type", None) == "text":
            try:
                value = json.loads(content.text)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("Invalid permission status")
