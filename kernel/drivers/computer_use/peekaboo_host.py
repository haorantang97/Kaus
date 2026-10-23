"""Use an installed official Peekaboo app as the stable macOS execution host.

The CLI's implicit on-demand daemon inherits the launching application's TCC
grants. Pin the GUI bridge when available so readiness and MCP operations have
the same authorization owner, including when Kaus runs as a LaunchAgent.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import plistlib
import re


def app_bundle(search_paths=None) -> Path | None:
    for path in (search_paths if search_paths is not None else
                 (Path("/Applications/Peekaboo.app"), Path.home() / "Applications/Peekaboo.app")):
        try:
            info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
            version = str(info.get("CFBundleShortVersionString", ""))
            match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[^\d])", version)
            # The unattended launch contract has been verified for this release.
            if (info.get("CFBundleIdentifier") == "boo.peekaboo.mac" and match
                    and tuple(map(int, match.groups())) >= (4, 9, 6)
                    and os.access(path / "Contents/MacOS/Peekaboo", os.X_OK)):
                return path
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return None


def bridge_socket() -> str:
    return str(Path.home() / "Library/Application Support/Peekaboo/bridge.sock")


def runtime_arguments() -> list[str]:
    # Older/CLI-only installations retain Peekaboo's native host selection.
    return ["mcp", "--bridge-socket", bridge_socket()] if app_bundle() else ["mcp"]


def selected_socket(arguments) -> str | None:
    values = list(arguments)
    try:
        return str(values[values.index("--bridge-socket") + 1])
    except (ValueError, IndexError):
        return None


async def start_app_host(socket: str, timeout: float) -> None:
    """Launch the fixed signed app without onboarding or permission requests."""
    app = app_bundle()
    if app is None or socket != bridge_socket():
        raise RuntimeError("peekaboo_host_unavailable")
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/open", "-g", "-j", "-a", str(app), "--args", "--background-bridge-host",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=min(timeout, 5))
        if process.returncode:
            raise RuntimeError("peekaboo_host_unavailable")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
