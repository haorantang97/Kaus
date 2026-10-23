from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from drivers.computer_use import peekaboo_host as host


def test_only_compatible_installed_app_is_selected(tmp_path):
    app = tmp_path / "Peekaboo.app"
    executable = app / "Contents/MacOS/Peekaboo"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    executable.chmod(0o700)
    info = app / "Contents/Info.plist"
    for identity, version, expected in [
        ("boo.peekaboo.mac", "4.9.6", app),
        ("boo.peekaboo.mac", "4.9.5", None),
        ("other.host", "4.9.6", None),
        ("boo.peekaboo.mac", "unknown", None),
    ]:
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": identity,
                                        "CFBundleShortVersionString": version}))
        assert host.app_bundle([app]) == expected
    assert host.app_bundle([tmp_path / "absent"]) is None


async def test_background_start_has_no_permission_or_foreground_request(monkeypatch, tmp_path):
    app, socket = tmp_path / "Peekaboo.app", str(tmp_path / "bridge.sock")
    monkeypatch.setattr(host, "app_bundle", lambda: app)
    monkeypatch.setattr(host, "bridge_socket", lambda: socket)
    process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(host.asyncio, "create_subprocess_exec", spawn)
    await host.start_app_host(socket, 8)
    assert spawn.call_args.args == (
        "/usr/bin/open", "-g", "-j", "-a", str(app), "--args", "--background-bridge-host")


async def test_host_start_does_not_accept_arbitrary_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "app_bundle", lambda: tmp_path / "Peekaboo.app")
    spawn = AsyncMock()
    monkeypatch.setattr(host.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="peekaboo_host_unavailable"):
        await host.start_app_host("/unrelated/bridge.sock", 8)
    spawn.assert_not_called()


async def test_host_launch_timeout_reaps_only_launcher(monkeypatch, tmp_path):
    socket = str(tmp_path / "bridge.sock")
    monkeypatch.setattr(host, "app_bundle", lambda: tmp_path / "Peekaboo.app")
    monkeypatch.setattr(host, "bridge_socket", lambda: socket)
    process = SimpleNamespace(returncode=None, wait=AsyncMock(side_effect=[TimeoutError, -9]), kill=Mock())
    monkeypatch.setattr(host.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await host.start_app_host(socket, 8)
    process.kill.assert_called_once()
    assert process.wait.await_count == 2
