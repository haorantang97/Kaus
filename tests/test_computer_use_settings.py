"""Computer-use settings expose setup summaries, never private runtime config."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "kernel")]

import computer_use_settings as settings
from drivers.computer_use.policy import Candidate


def driver(backend="openclaw", result=None, *, kind="native", failure=None):
    probe = AsyncMock(side_effect=failure, return_value=result)
    return SimpleNamespace(backend_id=backend, driver_kind=kind,
                           preset=SimpleNamespace(id=backend, label="Agent"),
                           computer_use_status=probe)


def registry(*drivers):
    return SimpleNamespace(list_drivers=lambda: list(drivers))


@pytest.fixture
def probes(monkeypatch, tmp_path):
    monkeypatch.setenv('KAUS_COMPUTER_USE_HOME', str(tmp_path / 'computer-use'))
    settings.component_settings.save_enabled(True)
    fallback = AsyncMock(return_value=(
        Candidate("playwright", "browser", "fallback", "ready", verified=True,
                  mcp_server={"command": "/private/secret-launcher",
                              "env": {"TOKEN": "fixture-secret"},
                              "headers": {"Authorization": "Bearer fixture-secret"}}),
    ))
    reconcile = AsyncMock(return_value=("desktop:local",))
    monkeypatch.setattr(settings, "probe_fallbacks", fallback)
    monkeypatch.setattr(settings, "reconcile_resources", reconcile)
    return fallback, reconcile


def test_snapshot_excludes_private_config_and_mock_drivers(probes):
    native = driver(result={"browser": {"status": "ready", "requirements": [],
                                       "endpoint": "http://private", "token": "fixture-secret"},
                            "desktop": {"status": "unavailable", "requirements": ["screen-recording"]},
                            "config": {"env": {"TOKEN": "fixture-secret"}}})
    mock = driver("mock", result={}, kind="mock")
    unsupported = SimpleNamespace(backend_id="fixture", driver_kind="native")
    result = asyncio.run(settings.ComputerUseSettings(registry(native, mock, unsupported)).snapshot(force=True))
    assert len(result["agents"]) == 1
    assert result["agents"][0]["browser"] == {"status": "ready", "requirements": []}
    assert result["agents"][0]["desktop"]["requirements"] == ["screen-recording"]
    assert result["fallbacks"][0]["verified"] is True
    assert result["pendingResources"] == ["desktop:local"]
    assert "fixture-secret" not in json.dumps(result)
    assert "/private/" not in json.dumps(result)
    assert "endpoint" not in json.dumps(result) and "mcp_server" not in json.dumps(result)
    mock.computer_use_status.assert_not_called()
    probes[0].assert_awaited_once_with(force=True)
    probes[1].assert_awaited_once()


def test_one_driver_failure_does_not_hide_other_agents(probes):
    failed = driver("openclaw", failure=RuntimeError("/private/config?token=fixture-secret"))
    ready = driver("codex", result={"browser": {"status": "ready", "requirements": []}})
    result = asyncio.run(settings.ComputerUseSettings(registry(failed, ready)).snapshot())
    assert [item["browser"]["status"] for item in result["agents"]] == ["unknown", "ready"]
    assert "fixture-secret" not in json.dumps(result)


def test_product_connections_share_one_setup_row_without_hiding_differences(probes):
    status = {"browser": {"status": "ready", "requirements": []}}
    first, second = driver("hermes", result=status), driver("hermes-acp", result=status)
    result = asyncio.run(settings.ComputerUseSettings(registry(first, second)).snapshot())
    assert len(result["agents"]) == 1
    assert result["agents"][0]["label"] == "Hermes"
    assert result["agents"][0]["browser"]["status"] == "ready"
    second.computer_use_status.return_value = {"browser": {"status": "disabled"}}
    result = asyncio.run(settings.ComputerUseSettings(registry(first, second)).snapshot())
    assert len(result["agents"]) == 1
    assert result["agents"][0]["browser"] == {
        "status": "unknown", "requirements": ["connection_specific"],
    }


@pytest.mark.parametrize("invalid", [
    None, [], {"browser": None}, {"browser": {"requirements": None}},
    {"browser": {"status": []}}, {"browser": {"status": {"error": "fixture-secret"}}},
])
def test_malformed_probe_stays_unknown_without_breaking_settings(probes, invalid):
    result = asyncio.run(settings.ComputerUseSettings(registry(driver(result=invalid))).snapshot())
    assert result["agents"][0]["browser"] == {"status": "unknown", "requirements": []}


def test_status_and_requirements_are_codes_not_raw_native_diagnostics(probes):
    result = asyncio.run(settings.ComputerUseSettings(registry(driver(result={
        "browser": {"status": "/private/fixture-secret",
                    "requirements": ["/private/config", "Bearer fixture-secret", {"token": "fixture-secret"},
                                     "native-browser-start"]},
    }))).snapshot())
    assert result["agents"][0]["browser"] == {
        "status": "unknown", "requirements": ["native-browser-start"],
    }
    assert "fixture-secret" not in json.dumps(result) and "/private/" not in json.dumps(result)


def test_permission_setup_follows_native_host_and_missing_fallback_items(probes, monkeypatch):
    from drivers.computer_use import permissions
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    probes[0].return_value = (Candidate("peekaboo", "desktop", "fallback", "needs_setup",
                                      requirements=("screen-recording",)),)
    native = driver("fixture", result={"desktop": {"status": "needs_setup",
        "requirements": ["computer_system_permission_required"],
        "permissionSetup": {"host": "cua-driver", "permissions": ["accessibility"],
                            "path": "/private/fixture-secret"}}})
    result = asyncio.run(settings.ComputerUseSettings(registry(native)).snapshot())
    assert result["agents"][0]["desktop"]["permissionSetup"] == {
        "host": "cua-driver", "label": "CuaDriver", "permissions": ["accessibility"]}
    assert result["fallbacks"][0]["permissionSetup"] == {
        "host": "peekaboo", "label": "Peekaboo", "permissions": ["screen-recording"]}
    assert "fixture-secret" not in json.dumps(result)


def test_repeated_status_poll_uses_cache_and_force_refreshes(probes):
    async def scenario():
        native = driver(result={'desktop': {'status': 'unknown'}})
        service = settings.ComputerUseSettings(registry(native))
        await service.snapshot()
        await service.snapshot()
        native.computer_use_status.assert_awaited_once()
        await service.snapshot(force=True)
        assert native.computer_use_status.await_count == 2
    asyncio.run(scenario())


def test_enable_returns_immediately_and_busy_poll_skips_native_probes(monkeypatch, tmp_path, probes):
    monkeypatch.setenv('KAUS_COMPUTER_USE_HOME', str(tmp_path / 'private'))
    start = SimpleNamespace(calls=0)
    def schedule():
        start.calls += 1
    monkeypatch.setattr(settings.installer, 'start', schedule)
    monkeypatch.setattr(settings.installer, 'snapshot', lambda: {'status': 'downloading'})
    async def scenario():
        native = driver(result={})
        service = settings.ComputerUseSettings(registry(native))
        result = await service.set_enabled(True)
        assert result['enabled'] is True and result['setup']['status'] == 'downloading'
        assert start.calls == 1
        await service.snapshot(force=True)
        native.computer_use_status.assert_not_called()
        probes[0].assert_not_called()
        result = await service.set_enabled(False)
        assert result['enabled'] is False and start.calls == 1
    asyncio.run(scenario())


def test_status_get_never_starts_install_and_disabled_retry_requires_enable(monkeypatch, tmp_path, probes):
    monkeypatch.setenv('KAUS_COMPUTER_USE_HOME', str(tmp_path / 'private'))
    def never(): raise AssertionError('GET attempted install')
    monkeypatch.setattr(settings.installer, 'start', never)
    async def scenario():
        service = settings.ComputerUseSettings(registry())
        result = await service.snapshot()
        assert result['enabled'] is False and result['setup'] == {'status': 'idle'}
        with pytest.raises(ValueError, match='not_enabled'):
            await service.retry_setup()
    asyncio.run(scenario())


def test_managed_permission_reveal_uses_verified_bundle_not_path_lookup(monkeypatch, tmp_path):
    from drivers.computer_use import permissions
    bundle = tmp_path / 'CuaDriver.app'
    monkeypatch.setattr(settings.component_settings, 'managed_bundle', lambda: bundle)
    opened = AsyncMock(return_value=True)
    monkeypatch.setattr(permissions, '_open', opened)
    assert asyncio.run(settings.ComputerUseSettings(registry()).reveal_permission_host('cua-driver')) is True
    opened.assert_awaited_once_with('-R', str(bundle))


def test_permission_refresh_is_scoped_and_reports_both_grants(probes, monkeypatch):
    refreshed = AsyncMock(return_value=True)
    monkeypatch.setattr(settings.installer, 'refresh_permission_host', refreshed)
    probes[0].return_value = (Candidate('cua-driver', 'desktop', 'fallback', 'ready', verified=True,
        system_permissions=(('accessibility', True), ('screen-recording', True))),)
    native = driver(result={})
    service = settings.ComputerUseSettings(registry(native))
    result = asyncio.run(service.permission_status())
    assert result['setup']['permissions'] == {'accessibility': True, 'screen-recording': True}
    native.computer_use_status.assert_not_called()
    refreshed.assert_awaited_once()
    probes[0].assert_awaited_once_with(capabilities=['desktop'], force=True)
    asyncio.run(service.permission_status())
    refreshed.assert_awaited_once()  # Ready hosts do not need a restart.


def test_disabled_snapshot_skips_discovery_and_still_reconciles_owned_resources(monkeypatch, tmp_path, probes):
    monkeypatch.setenv('KAUS_COMPUTER_USE_HOME', str(tmp_path / 'off'))
    native = driver(result={})
    service = settings.ComputerUseSettings(registry(native))
    service._cache = {'agents': [{'id': 'cached'}], 'fallbacks': [], 'pendingResources': []}
    result = asyncio.run(service.snapshot(force=True))
    assert result['enabled'] is False
    assert result['agents'] == [{'id': 'cached'}]
    assert result['pendingResources'] == ['desktop:local']
    native.computer_use_status.assert_not_called()
    probes[0].assert_not_called()
    probes[1].assert_awaited_once()


def test_off_exposes_only_connected_native_permission_hosts_without_probing(monkeypatch, tmp_path, probes):
    settings.component_settings.save_enabled(False)
    native = driver('openclaw')
    native.native_computer_use_permission_control = 'agent'
    second = driver('openclaw')
    second.native_computer_use_permission_control = 'agent'
    native.preset.label = second.preset.label = 'OpenClaw'
    managed = driver('hermes')
    mock = driver('openclaw', kind='mock')
    mock.native_computer_use_permission_control = 'agent'
    service = settings.ComputerUseSettings(registry(native, second, managed, mock))
    result = asyncio.run(service.snapshot(force=True))
    assert result['nativePermissionHosts'] == ['OpenClaw']
    for item in (native, second, managed, mock):
        item.computer_use_status.assert_not_called()
    probes[0].assert_not_called()
    assert settings.ComputerUseSettings(registry(managed))._current_snapshot()['nativePermissionHosts'] == []
