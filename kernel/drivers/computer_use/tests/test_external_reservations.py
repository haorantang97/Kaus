"""Persistent terminal reservations reuse the existing process-group entrypoint."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from drivers.base import CliLaunchSpec
from drivers.computer_use.lease import ResourceBusyError, ResourceLease
from drivers.terminals import integrations, managed_process


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)


async def busy(root):
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=root, timeout=0).acquire()


async def test_live_terminal_reservation_survives_backend_owner_exit_and_ignores_closed_fds(tmp_path):
    launch, locks = tmp_path / "launch", tmp_path / "locks"
    launch.mkdir()
    marker = tmp_path / "ready"
    native = tmp_path / "native"
    native.write_text(f"#!{sys.executable}\nimport os,time\nos.closerange(3,256)\n"
                      f"open({str(marker)!r},'w').close()\ntime.sleep(60)\n")
    native.chmod(0o700)
    command = integrations.prepare_command(CliLaunchSpec(command=(str(native),)), launch, "terminal")
    lease = ResourceLease("desktop:local", root=locks)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_file(marker)
        backend = await asyncio.create_subprocess_exec(sys.executable, "-c", """
import asyncio,sys
from pathlib import Path
from drivers.computer_use.lease import ResourceLease
async def main():
    async with ResourceLease('desktop:local', root=sys.argv[1]) as lease:
        lease.reserve_external('launch-one',Path(sys.argv[2]))
asyncio.run(main())
""", str(locks), str(launch), env={"PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3]), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, error = await asyncio.wait_for(backend.communicate(), 5)
        assert backend.returncode == 0, error.decode()
        assert managed_process.process_identity(backend.pid) is None
        # The reservation-writing backend process exited and its flock is gone;
        # a new process still sees the native process identity and blocks control.
        await busy(locks)
        async with ResourceLease("browser:context:independent", root=locks, timeout=0):
            pass
        assert await asyncio.to_thread(managed_process.stop, launch, str(process.pid)) == (True, None)
        await asyncio.to_thread(process.wait, 5)
        async with ResourceLease("desktop:local", root=locks, timeout=0):
            pass
    finally:
        await asyncio.to_thread(managed_process.stop, launch, str(process.pid))
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


async def test_terminal_leader_exit_keeps_reservation_until_child_exits(tmp_path):
    launch, locks = tmp_path / "launch", tmp_path / "locks"
    launch.mkdir()
    marker = tmp_path / "child.pid"
    native = tmp_path / "native"
    native.write_text(f"#!{sys.executable}\nimport subprocess,sys\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"open({str(marker)!r},'w').write(str(p.pid))\n")
    native.chmod(0o700)
    command = integrations.prepare_command(CliLaunchSpec(command=(str(native),)), launch, "terminal")
    lease = ResourceLease("desktop:local", root=locks)
    await lease.acquire(); lease.reserve_external("launch-one", launch); lease.release()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_file(marker)
        await asyncio.to_thread(process.wait, 5)
        await busy(locks)
        child = int(marker.read_text())
        os.kill(child, signal.SIGKILL)
        async with asyncio.timeout(5):
            while managed_process._group_active(process.pid):
                await asyncio.sleep(.02)
        async with ResourceLease("desktop:local", root=locks, timeout=0):
            pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


async def test_crashed_pending_launch_revoked_before_resource_is_reused(tmp_path):
    launch = tmp_path / "launch"
    launch.mkdir()
    lease = ResourceLease("desktop:local", root=tmp_path / "locks")
    await lease.acquire(); lease.reserve_external("pending", launch); lease.release()
    saved = json.loads(lease.reservation_path.read_text())
    saved["owner"]["started"] = "previous backend generation"
    lease.reservation_path.write_text(json.dumps(saved))
    async with ResourceLease("desktop:local", root=lease.root, timeout=0):
        assert (launch / "revoked").exists()


async def test_recorded_detached_service_keeps_reservation_after_terminal_leader_exit(tmp_path):
    launch, locks = tmp_path / "launch", tmp_path / "locks"
    launch.mkdir()
    marker = tmp_path / "service.pid"
    native = tmp_path / "native"
    kernel = str(Path(__file__).resolve().parents[3])
    native.write_text(f"#!{sys.executable}\nimport subprocess,sys\nsys.path.insert(0,{kernel!r})\n"
        "from drivers.terminals.managed_process import register_owned_child\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
        "register_owned_child(p.pid)\n"
        f"open({str(marker)!r},'w').write(str(p.pid))\n")
    native.chmod(0o700)
    command = integrations.prepare_command(CliLaunchSpec(command=(str(native),)), launch, "terminal")
    async with ResourceLease("desktop:local", root=locks) as lease:
        lease.reserve_external("detached-service", launch)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_file(marker)
        await asyncio.to_thread(process.wait, 5)
        child = int(marker.read_text())
        assert not managed_process._group_active(process.pid)
        assert managed_process._group_active(child)
        await busy(locks)
        assert await asyncio.to_thread(managed_process.stop, launch, str(process.pid)) == (True, None)
        assert not managed_process._group_active(child)
        async with ResourceLease("desktop:local", root=locks, timeout=0):
            pass
    finally:
        await asyncio.to_thread(managed_process.stop, launch, str(process.pid))
        process.wait(timeout=5)


def test_registered_service_identity_mismatch_is_not_signalled(tmp_path, monkeypatch):
    child = tmp_path / "owned-processes" / "123"
    child.mkdir(parents=True)
    saved = {"pid": 123, "pgid": 123, "uid": os.getuid(), "started": "old-generation"}
    (child / "process.json").write_text(json.dumps(saved))
    (tmp_path / "native.json").write_text("{}")
    monkeypatch.setattr(managed_process, "process_identity", lambda _: {**saved, "started": "new-generation"})
    def must_not_signal(*_):
        pytest.fail("Unrelated process generation was signalled")
    monkeypatch.setattr(managed_process.os, "killpg", must_not_signal)
    assert managed_process.stop(tmp_path, None)[0] is False


async def test_remote_confirmation_is_not_inferred_from_local_exit_and_old_owner_cannot_clear_new(tmp_path):
    launch = tmp_path / "launch"
    launch.mkdir()
    lease = ResourceLease("desktop:local", root=tmp_path / "locks")
    await lease.acquire(); lease.reserve_external("remote-one", launch, remote=True); lease.release()
    (launch / "exit").write_text("0")
    await busy(lease.root)
    await lease.clear_external("different-launch")
    await busy(lease.root)
    await lease.clear_external("remote-one")
    async with ResourceLease("desktop:local", root=lease.root, timeout=0):
        pass
