from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from drivers.computer_use import permissions


def test_setup_only_exposes_named_hosts_and_actual_missing_permissions(monkeypatch):
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    assert permissions.native_setup({"permissionSetup": {
        "host": "cua-driver", "permissions": ["accessibility", "/private/secret"],
        "label": "/private/secret", "path": "/private/secret",
    }}) == {"host": "cua-driver", "label": "CuaDriver", "permissions": ["accessibility"]}
    assert permissions.setup_details("peekaboo", []) is None
    assert permissions.setup_details("/private/unknown", ["accessibility"]) is None
    monkeypatch.setattr(permissions.sys, "platform", "linux")
    assert permissions.setup_details("peekaboo", ["accessibility"]) is None


@pytest.mark.parametrize("permission", ["accessibility", "screen-recording"])
async def test_navigation_only_opens_fixed_system_pane(monkeypatch, permission):
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", spawn)
    assert await permissions.open_permission_settings(permission) is True
    assert spawn.call_args.args == ("/usr/bin/open", permissions.PERMISSION_PANES[permission])
    process.wait.assert_awaited_once()


async def test_navigation_invalid_target_and_non_mac_do_not_spawn(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError):
        await permissions.open_permission_settings("file:///private/fixture")
    monkeypatch.setattr(permissions.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="platform_unavailable"):
        await permissions.open_permission_settings("accessibility")
    spawn.assert_not_called()


async def test_failed_open_child_is_reaped(monkeypatch):
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    process = SimpleNamespace(returncode=None, wait=AsyncMock(side_effect=[TimeoutError, -9]), kill=Mock())
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await permissions.open_permission_settings("accessibility") is False
    process.kill.assert_called_once()
    assert process.wait.await_count == 2


async def test_pending_navigation_can_complete_and_reuses_repeated_clicks(monkeypatch):
    import asyncio
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions, "_NAVIGATION_REPLY_TIMEOUT", .01)
    monkeypatch.setattr(permissions, "_NAVIGATION_LIFETIME", 1)
    release = asyncio.Event()
    process = SimpleNamespace(returncode=None, kill=Mock())
    async def wait():
        await release.wait()
        process.returncode = 0
        return 0
    process.wait = AsyncMock(side_effect=wait)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", spawn)
    assert await permissions.open_permission_settings("accessibility") is False
    assert await permissions.open_permission_settings("accessibility") is False
    spawn.assert_awaited_once()
    process.kill.assert_not_called()
    task = next(iter(permissions._PENDING.values()))
    release.set()
    assert await task is True
    await asyncio.sleep(0)
    assert not permissions._PENDING
    process.kill.assert_not_called()


async def test_pending_navigation_final_timeout_only_reaps_its_helper(monkeypatch):
    import asyncio
    monkeypatch.setattr(permissions, "_NAVIGATION_REPLY_TIMEOUT", .01)
    monkeypatch.setattr(permissions, "_NAVIGATION_LIFETIME", .03)
    process = SimpleNamespace(returncode=None, kill=Mock())
    calls = 0
    async def wait():
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        process.returncode = -9
        return -9
    process.wait = AsyncMock(side_effect=wait)
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await permissions._open("fixed-target") is False
    task = next(iter(permissions._PENDING.values()))
    process.kill.assert_not_called()
    assert await task is False
    process.kill.assert_called_once()
    await asyncio.sleep(0)
    assert not permissions._PENDING


@pytest.mark.parametrize("host,binary,expected", [
    ("cua-driver", "/Applications/CuaDriver.app/Contents/MacOS/cua-driver", "/Applications/CuaDriver.app"),
    ("peekaboo", "/private/fixture/peekaboo", "/private/fixture/peekaboo"),
])
async def test_reveal_uses_resolved_tool_not_user_path(monkeypatch, host, binary, expected):
    monkeypatch.setattr(permissions, "peekaboo_app_bundle", lambda: None)
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.shutil, "which", lambda name: binary)
    opener = AsyncMock()
    monkeypatch.setattr(permissions, "_open", opener)
    await permissions.reveal_permission_host(host)
    opener.assert_awaited_once_with("-R", expected)
    with pytest.raises(ValueError):
        await permissions.reveal_permission_host("/private/secret")


async def test_uninstalled_host_reveal_does_not_open_finder(monkeypatch):
    monkeypatch.setattr(permissions, "peekaboo_app_bundle", lambda: None)
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.shutil, "which", lambda name: None)
    opener = AsyncMock()
    monkeypatch.setattr(permissions, "_open", opener)
    with pytest.raises(RuntimeError, match="permission_host_unavailable"):
        await permissions.reveal_permission_host("peekaboo")
    opener.assert_not_called()


async def test_reveal_uses_fixed_app_authorization_owner(monkeypatch, tmp_path):
    app = tmp_path / "Peekaboo.app"
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions, "peekaboo_app_bundle", lambda: app)
    opener = AsyncMock()
    monkeypatch.setattr(permissions, "_open", opener)
    await permissions.reveal_permission_host("peekaboo")
    opener.assert_awaited_once_with("-R", str(app))


async def test_failed_navigation_retains_bounded_developer_diagnostic(monkeypatch, caplog):
    import asyncio
    stream = asyncio.StreamReader()
    stream.feed_data(b"LaunchServices refused fixed target\n" + b"x" * 6000)
    stream.feed_eof()
    process = SimpleNamespace(returncode=1, wait=AsyncMock(return_value=1), stderr=stream)
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RuntimeError, match="^settings_open_failed$"):
        await permissions._open("x-apple.systempreferences:fixed")
    assert "LaunchServices refused" in caplog.text
    assert len(caplog.text) < 4400
