"""Synthetic onboarding: no model, desktop action, authorization, or network."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import tarfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drivers.computer_use import installer, settings


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("KAUS_COMPUTER_USE_HOME", str(tmp_path / "component"))
    monkeypatch.setattr(installer, "supported_error", lambda: None)
    installer._TASKS.clear()
    installer._HOST_TASKS.clear()
    installer._REQUEST_TASKS.clear()
    return tmp_path / "component"


def fake_bundle(destination):
    bundle = destination / "CuaDriver.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/MacOS/cua-driver").write_text("fixture")
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.trycua.driver", "CFBundleShortVersionString": settings.VERSION}))
    return bundle


def fake_install(monkeypatch):
    monkeypatch.setattr(installer, "_download", lambda path: path.write_text("archive"))
    monkeypatch.setattr(installer, "_extract", lambda archive, destination: fake_bundle(destination))
    monkeypatch.setattr(installer, "_verify", lambda bundle: None)


def test_default_disabled_read_does_not_create_files(private_state):
    assert settings.is_enabled() is False
    assert installer.snapshot() == {"status": "idle"}
    assert not private_state.exists()


def test_settings_are_private_atomic_strict_and_corruption_stays_disabled(private_state):
    settings.save_enabled(True)
    assert settings.is_enabled()
    assert (private_state / "settings.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        settings.save_enabled("true")
    (private_state / "settings.json").write_text("not-json")
    assert not settings.is_enabled()
    assert not list(private_state.glob(".state-*"))


def test_fresh_install_is_private_and_commits_only_verified_bundle(private_state, monkeypatch):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    bundle = settings.managed_bundle()
    assert bundle and bundle.is_relative_to(private_state / "versions")
    assert installer.snapshot() == {"status": "needs_permission", "version": settings.VERSION}
    assert settings.read_json("current.json")["archive_sha256"] == installer.ASSET_SHA256
    assert not list(private_state.glob(".install-*"))
    installer.mark_readiness(True)
    assert installer.snapshot()["status"] == "ready"
    settings.save_enabled(False)
    assert installer.snapshot() == {"status": "idle"}
    assert bundle.exists()  # disabling never uninstalls or ends an active host


def test_signature_failure_cleans_temporary_files_without_publishing(private_state, monkeypatch):
    fake_install(monkeypatch)
    monkeypatch.setattr(installer, "_verify", Mock(side_effect=installer.SetupError("signature_failed")))
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    assert installer.snapshot() == {"status": "failed", "error": "signature_failed"}
    assert settings.managed_executable() is None
    assert not (private_state / "current.json").exists()
    assert not list(private_state.glob(".install-*"))
    monkeypatch.setattr(installer, "_verify", lambda bundle: None)
    installer.install(reuse_existing=False)
    assert settings.managed_executable() is not None


def test_changed_executable_receipt_rejected_and_retry_preserves_old_generation(private_state, monkeypatch):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    old = settings.managed_executable()
    old.write_text("changed fixture")
    assert settings.managed_executable() is None
    assert installer.snapshot()["status"] == "failed"
    installer.install(reuse_existing=False)
    assert settings.managed_executable() != old
    assert old.exists()


def test_verified_existing_bundle_reuse_does_not_download_or_write_native(monkeypatch, private_state):
    fake_install(monkeypatch)
    bundle = fake_bundle(private_state / "versions" / settings.VERSION / ("a" * 32))
    installer._record(bundle, reused=False)
    before = bundle.joinpath("Contents/MacOS/cua-driver").read_bytes()
    download = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(installer, "_download", download)
    installer.install()
    assert bundle.joinpath("Contents/MacOS/cua-driver").read_bytes() == before
    download.assert_not_called()


def test_concurrent_start_deduplicates_and_disable_does_not_cancel(monkeypatch):
    async def scenario():
        entered, finish = threading.Event(), threading.Event()
        calls = []
        def work():
            calls.append(1)
            entered.set()
            finish.wait(2)
        monkeypatch.setattr(installer, "install", work)
        settings.save_enabled(True)
        installer.start()
        installer.start()
        assert await asyncio.to_thread(entered.wait, 1)
        task = installer._TASKS[str(settings.storage_root())]
        settings.save_enabled(False)
        assert not task.cancelled()
        finish.set()
        await task
        assert calls == [1]
    asyncio.run(scenario())


def test_snapshot_never_runs_installer_and_interrupted_can_retry(monkeypatch):
    settings.save_enabled(True)
    settings.write_json("setup.json", {"status": "downloading", "error": "/private/token"})
    assert installer.snapshot() == {"status": "failed", "error": "setup_interrupted"}
    monkeypatch.setattr(installer, "supported_error", lambda: "platform_unsupported")
    assert installer.snapshot() == {"status": "unsupported", "error": "platform_unsupported"}


def test_pinned_download_hash_and_redirect_checks(tmp_path, monkeypatch):
    payload = b"official fixture"
    class Response(io.BytesIO):
        url = "https://release-assets.githubusercontent.com/fixture"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: Response(payload))
    monkeypatch.setattr(installer, "ASSET_SHA256", hashlib.sha256(payload).hexdigest())
    installer._download(tmp_path / "archive")
    assert (tmp_path / "archive").read_bytes() == payload
    monkeypatch.setattr(installer, "ASSET_SHA256", "0" * 64)
    with pytest.raises(installer.SetupError, match="integrity_failed"):
        installer._download(tmp_path / "bad")
    Response.url = "https://untrusted.example/archive"
    with pytest.raises(installer.SetupError, match="download_failed"):
        installer._download(tmp_path / "untrusted")


@pytest.mark.parametrize("name,kind,link", [
    ("/tmp/outside", tarfile.REGTYPE, ""),
    (f"cua-driver-rs-{settings.VERSION}-darwin-universal/../outside", tarfile.REGTYPE, ""),
    (f"cua-driver-rs-{settings.VERSION}-darwin-universal/CuaDriver.app/link", tarfile.SYMTYPE, "/tmp/outside"),
])
def test_archive_rejects_path_traversal_and_links(tmp_path, name, kind, link):
    path = tmp_path / "archive.tgz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, link
        archive.addfile(member)
    with pytest.raises(installer.SetupError, match="integrity_failed"):
        installer._extract(path, tmp_path / "extract")
    assert not (tmp_path / "outside").exists()


def test_signature_requires_pinned_id_version_and_team(monkeypatch, tmp_path):
    bundle = fake_bundle(tmp_path / "fixture")
    run = Mock()
    monkeypatch.setattr(installer.subprocess, "run", run)
    installer._verify(bundle)
    args = run.call_args.args[0]
    assert "--deep" in args and "--strict" in args
    assert installer.SIGNING_REQUIREMENT in args and 'YCK386LBJ7' in installer.SIGNING_REQUIREMENT
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "wrong"}))
    with pytest.raises(installer.SetupError, match="signature_failed"):
        installer._verify(bundle)


def test_socket_is_short_private_version_specific_and_not_default(monkeypatch, private_state):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    first = settings.runtime_socket()
    executable = settings.managed_executable()
    assert len(str(first).encode()) < 104 and str(first).startswith('/tmp/kaus-cua-')
    assert settings.runtime_socket(executable) == first
    executable.write_text('new fixture')
    assert settings.runtime_socket(executable) != first


def test_runtime_launch_uses_exact_bundle_no_permission_gate(monkeypatch, private_state, tmp_path):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    executable, bundle = settings.managed_executable(), settings.managed_bundle()
    endpoint = tmp_path / 'private-host' / 'driver.sock'
    monkeypatch.setattr(settings, 'runtime_socket', lambda *a: endpoint)
    listening = iter([False, True])
    monkeypatch.setattr(installer, '_listening', lambda p: next(listening))
    launch = Mock()
    monkeypatch.setattr(installer.subprocess, 'run', launch)
    assert installer._runtime_host(1, executable) == endpoint
    args = launch.call_args.args[0]
    assert str(bundle) in args and str(endpoint) in args
    assert '--no-permissions-gate' in args and '--no-overlay' in args
    assert '--permission-mode' not in args and '--grant' not in args


def test_permission_request_targets_exact_bundle_and_no_capture(monkeypatch, private_state):
    monkeypatch.setattr(installer, "sys", SimpleNamespace(platform="darwin"))
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    bundle = settings.managed_bundle()
    commands = []
    class Process:
        returncode = 0
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        commands.append(args)
        result = Path(args[-1])
        assert result.name.startswith('cua-driver-permissions-')
        assert result.stat().st_mode & 0o777 == 0o600
        result.write_text(json.dumps({"structuredContent": {"accessibility": False,
            "screen_recording": False, "source": {"attribution": "driver-daemon",
            "bundle_id": "com.trycua.driver", "executable": str(bundle / "Contents/MacOS/cua-driver")}}}))
        return Process()
    monkeypatch.setattr(installer.asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(installer.request_permissions()) is True
    args = commands[0]
    assert str(bundle) in args and '__permissions-host-request' in args
    assert '--probe-direct-capture' not in args
    assert not Path(args[-1]).exists()


@pytest.mark.parametrize('exit_code, receipt, succeeds', [(1, True, True), (0, False, False)])
def test_permission_receipt_is_authoritative_over_launchservices_exit(monkeypatch, exit_code, receipt, succeeds):
    monkeypatch.setattr(installer, "sys", SimpleNamespace(platform="darwin"))
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    bundle = settings.managed_bundle()
    class Process:
        returncode = exit_code
        async def wait(self): return self.returncode
    async def spawn(*args, **kwargs):
        if receipt:
            Path(args[-1]).write_text(json.dumps({'structuredContent': {
                'accessibility': True, 'screen_recording': False,
                'source': {'attribution': 'driver-daemon', 'bundle_id': 'com.trycua.driver',
                           'executable': str(bundle / 'Contents/MacOS/cua-driver')}}}))
        return Process()
    monkeypatch.setattr(installer.asyncio, 'create_subprocess_exec', spawn)
    if succeeds:
        assert asyncio.run(installer.request_permissions()) is True
    else:
        with pytest.raises(installer.SetupError, match='permission_request_failed'):
            asyncio.run(installer.request_permissions())


def test_permission_refresh_never_stops_an_active_desktop_owner(monkeypatch, tmp_path):
    from drivers.computer_use import cua_guard, lease
    from drivers.computer_use.lease import ResourceLease
    monkeypatch.setattr(lease, 'default_lease_root', lambda: tmp_path / 'leases')
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    stop = Mock(side_effect=AssertionError('Active desktop must not be stopped'))
    monkeypatch.setattr(cua_guard, '_stop_orphan', stop)
    async def scenario():
        async with ResourceLease('desktop:local'):
            assert await installer.refresh_permission_host() is False
    asyncio.run(scenario())
    stop.assert_not_called()


def test_concurrent_process_lock_skips_duplicate_download(monkeypatch):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    calls = []
    def download(path):
        calls.append(1)
        path.write_text('archive')
    monkeypatch.setattr(installer, '_download', download)
    threads = [threading.Thread(target=installer.install, kwargs={'reuse_existing': False}) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert len(calls) == 1


def test_runtime_failure_is_not_mislabeled_missing_permission(monkeypatch):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    installer.mark_readiness(False, permission_required=False)
    assert installer.snapshot()['error'] == 'runtime_check_failed'
    installer.mark_readiness(False, permission_required=True)
    assert installer.snapshot()['status'] == 'needs_permission'


def test_retry_replaces_invalid_receipt_without_overwriting_old_bundle(monkeypatch, private_state):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    old = settings.managed_bundle()
    def verify(bundle):
        if bundle == old:
            raise installer.SetupError('signature_failed')
    monkeypatch.setattr(installer, '_verify', verify)
    stale = private_state / '.install-crashed'
    stale.mkdir()
    (stale / 'archive').write_text('incomplete')
    installer.install(reuse_existing=False)
    assert settings.managed_bundle() != old and old.exists()
    assert not stale.exists()


def test_only_active_projected_runtime_may_start_host_after_toggle_off(monkeypatch, private_state, tmp_path):
    fake_install(monkeypatch)
    settings.save_enabled(True)
    installer.install(reuse_existing=False)
    executable = settings.managed_executable()
    endpoint = tmp_path / "active-host" / "driver.sock"
    monkeypatch.setattr(settings, "runtime_socket", lambda *a: endpoint)
    settings.save_enabled(False)
    with pytest.raises(ValueError, match="not_enabled"):
        installer._runtime_host(1, executable)
    monkeypatch.setattr(installer, "_listening", lambda p: True)
    assert installer._runtime_host(1, executable, authorized_runtime=True) == endpoint
    async def probe():
        with pytest.raises(ValueError, match="not_enabled"):
            await installer.ensure_runtime_host(executable=executable)
        assert await installer.ensure_runtime_host(executable=executable, authorized_runtime=True) == endpoint
        with pytest.raises(ValueError, match="not_enabled"):
            await installer.ensure_runtime_host(executable=executable)
    asyncio.run(probe())
