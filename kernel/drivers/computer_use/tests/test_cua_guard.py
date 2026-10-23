"""No desktop actions: use synthetic identities and a plain Unix listener."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from drivers.computer_use import cua_guard as guard
from drivers.computer_use import integration, lease as lease_module
from drivers.computer_use.lease import ResourceLease, ResourceBusyError
from drivers.computer_use.tests.test_driver_control import driver_state, locks


@pytest.fixture
def identities(monkeypatch):
    caller = {"pid": os.getpid(), "uid": os.getuid(), "pgid": os.getpgrp(), "started": "caller"}
    daemon = {"pid": os.getpid() + 1000000, "uid": os.getuid(), "pgid": 99, "started": "daemon"}
    active = {caller["pid"]: caller, daemon["pid"]: daemon}
    monkeypatch.setattr(guard, "_identity", lambda pid: active.get(pid))
    monkeypatch.setattr(guard, "_peer_identity", lambda _: daemon)
    return caller, daemon, active


async def test_returned_rpc_removes_receipt_without_recording_arguments(tmp_path, identities):
    async with guard.Operation("/installed/cua-driver", "/synthetic/socket", root=tmp_path):
        value = json.loads((tmp_path / guard.RECEIPT).read_text())
        assert set(value) == {"version", "id", "daemon", "caller", "socket", "executable"}
        assert guard.reconcile(tmp_path) is False
    assert guard.reconcile(tmp_path) is True
    assert not (tmp_path / guard.RECEIPT).exists()


@pytest.mark.parametrize("error", [asyncio.CancelledError, TimeoutError, BrokenPipeError])
async def test_cancel_timeout_or_transport_exit_does_not_acknowledge_daemon(tmp_path, identities, error):
    with pytest.raises(error):
        async with guard.Operation("/installed/cua-driver", "/synthetic/socket", root=tmp_path):
            raise error()
    assert (tmp_path / guard.RECEIPT).exists()
    assert guard.reconcile(tmp_path) is False
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=tmp_path, timeout=0).acquire()
    # Unrelated isolated browser work does not wait for the desktop.
    async with ResourceLease("browser:context:separate", root=tmp_path, timeout=0):
        pass


async def test_calls_in_one_turn_serialize_and_waiter_cancel_keeps_owner(tmp_path, identities):
    entered = asyncio.Event()
    async with guard.Operation("/installed/cua-driver", "/synthetic/socket", root=tmp_path):
        async def waiting():
            async with guard.Operation("/installed/cua-driver", "/synthetic/socket", root=tmp_path):
                entered.set()
        task = asyncio.create_task(waiting())
        await asyncio.sleep(.04)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not entered.is_set() and guard.reconcile(tmp_path) is False
    assert guard.reconcile(tmp_path) is True


async def abandoned(root):
    with pytest.raises(TimeoutError):
        async with guard.Operation("/installed/cua-driver", "/synthetic/socket", root=root):
            raise TimeoutError()


async def test_orphan_does_not_release_until_daemon_stop_is_confirmed(tmp_path, identities, monkeypatch):
    caller, daemon, active = identities
    await abandoned(tmp_path)
    active.pop(caller["pid"])
    stops = []
    monkeypatch.setattr(guard, "_stop_orphan", lambda value: stops.append(value["daemon"]) or False)
    assert guard.reconcile(tmp_path) is False
    assert stops == [daemon] and (tmp_path / guard.RECEIPT).exists()
    # Confirmation means the exact saved daemon is gone, not its MCP caller.
    active.pop(daemon["pid"])
    assert guard.reconcile(tmp_path) is True
    async with ResourceLease("desktop:local", root=tmp_path, timeout=0):
        pass


async def test_pid_reuse_never_signals_new_daemon_incarnation(tmp_path, identities, monkeypatch):
    caller, daemon, active = identities
    await abandoned(tmp_path)
    active[daemon["pid"]] = {**daemon, "started": "different process"}
    monkeypatch.setattr(guard, "_stop_orphan", lambda _: pytest.fail("must not signal reused PID"))
    assert guard.reconcile(tmp_path) is True


async def test_prepare_runs_after_orphan_recovery_before_new_dispatch(tmp_path, identities, monkeypatch):
    caller, daemon, active = identities
    await abandoned(tmp_path)
    active.pop(caller['pid'])
    order = []
    def stop(value):
        order.append('old_host_stopped')
        active.pop(daemon['pid'])
        return True
    monkeypatch.setattr(guard, '_stop_orphan', stop)
    replacement = {**daemon, 'started': 'replacement'}
    async def prepare():
        assert not (tmp_path / guard.RECEIPT).exists()
        order.append('new_host_connected')
        active[caller['pid']] = caller
        active[replacement['pid']] = replacement
        monkeypatch.setattr(guard, '_peer_identity', lambda _: replacement)
    async with guard.Operation('/installed/cua-driver', '/synthetic/socket', root=tmp_path, prepare=prepare):
        assert json.loads((tmp_path / guard.RECEIPT).read_text())['daemon'] == replacement
        order.append('new_rpc_dispatched')
    assert order == ['old_host_stopped', 'new_host_connected', 'new_rpc_dispatched']


async def test_unknown_status_or_invalid_receipt_stays_pending(tmp_path, identities, monkeypatch):
    await abandoned(tmp_path)
    def unknown(_):
        raise OSError("process query unavailable")
    monkeypatch.setattr(guard, "_identity", unknown)
    assert guard.reconcile(tmp_path) is False
    (tmp_path / guard.RECEIPT).write_text("not json")
    assert guard.reconcile(tmp_path) is False
    outside = tmp_path / "outside"
    outside.write_text("private")
    (tmp_path / guard.RECEIPT).unlink()
    (tmp_path / guard.RECEIPT).symlink_to(outside)
    assert guard.reconcile(tmp_path) is False
    assert outside.read_text() == "private"


def test_orphan_stop_requires_private_socket_exact_command_and_identity(monkeypatch):
    daemon = {"pid": 654321, "uid": os.getuid(), "pgid": 20, "started": "saved"}
    value = {"daemon": daemon, "socket": f"/tmp/kaus-cua-{os.getuid()}-{'a' * 20}/driver.sock",
             "executable": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"}
    command = f"{value['executable']} serve --socket {value['socket']} --no-permissions-gate --no-overlay"
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: type("PS", (), {"returncode": 0, "stdout": command})())
    assert guard._owned_daemon(value)
    assert not guard._owned_daemon({**value, "socket": "/tmp/cua-driver.sock"})
    assert not guard._owned_daemon({**value, "executable": "/Applications/UserApp.app/Contents/MacOS/other"})
    active = daemon
    signals = []
    monkeypatch.setattr(guard, "_identity", lambda _: active)
    def kill(pid, sig):
        nonlocal active
        signals.append((pid, sig))
        active = None
    monkeypatch.setattr(guard.os, "kill", kill)
    assert guard._stop_orphan(value)
    assert signals == [(daemon["pid"], guard.signal.SIGTERM)]


@pytest.mark.parametrize("receipt", ["cancelled", "end_turn"])
async def test_acp_result_and_closed_child_do_not_release_busy_daemon(locks, identities, monkeypatch, receipt):
    monkeypatch.setattr(lease_module, "default_lease_root", lambda: locks.root)
    caller, daemon, active = identities
    driver, state = driver_state()
    async def invoke():
        await abandoned(locks.root)
        return {"stopReason": receipt}, None
    assert await driver._computer_prompt(state, invoke) == ({"stopReason": receipt}, None)
    assert state.connection.closed is (receipt == "cancelled")
    # New group member or terminal cannot gain desktop ownership after either
    # the completed parent reply or a confirmed ACP child close.
    assert await integration.reconcile_resources() == ("desktop:local",)
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=locks.root, timeout=0).acquire()
    ordinary, ordinary_state = driver_state()
    ordinary_state.runtime_metadata["_kausComputerUse"]["resources"] = []
    async def chat():
        return "ordinary chat"
    assert await ordinary._computer_prompt(ordinary_state, chat) == "ordinary chat"
    active.pop(daemon["pid"])
    assert await integration.reconcile_resources() == ()
    async with ResourceLease("desktop:local", root=locks.root, timeout=0):
        pass


async def test_process_death_persists_receipt_while_daemon_lives_and_recovers_after_exit(tmp_path, monkeypatch):
    # A plain AF_UNIX listener stands in for the external daemon: no CuaDriver,
    # model, desktop capture, tool action or native configuration is involved.
    short = tempfile.TemporaryDirectory(prefix="kaus-peer-", dir="/tmp")
    endpoint = Path(short.name) / "driver.sock"
    daemon = subprocess.Popen([sys.executable, "-c", "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(60)", str(endpoint)])
    caller = None
    root = tmp_path / "locks"
    kernel = str(Path(__file__).resolve().parents[3])
    try:
        async with asyncio.timeout(5):
            while not endpoint.exists():
                await asyncio.sleep(.01)
        assert guard._peer_identity(endpoint)["pid"] == daemon.pid
        code = f'''import asyncio,sys
sys.path.insert(0,{kernel!r})
from drivers.computer_use.cua_guard import Operation
async def main():
 async with Operation('/synthetic/cua-driver',{str(endpoint)!r},root={str(root)!r}):
  await asyncio.sleep(60)
asyncio.run(main())
'''
        caller = subprocess.Popen([sys.executable, "-c", code])
        async with asyncio.timeout(5):
            while not (root / guard.RECEIPT).exists():
                await asyncio.sleep(.01)
        caller.kill()
        caller.wait(timeout=3)
        assert daemon.poll() is None
        # It is not an installer-owned CuaDriver, so recovery must NOT kill it.
        assert guard.reconcile(root) is False
        assert daemon.poll() is None
        with pytest.raises(ResourceBusyError):
            await ResourceLease("desktop:local", root=root, timeout=0).acquire()
        daemon.terminate()
        daemon.wait(timeout=3)
        assert guard.reconcile(root) is True
        async with ResourceLease("desktop:local", root=root, timeout=0):
            pass
    finally:
        for process in (caller, daemon):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        short.cleanup()


def test_module_and_idle_status_remain_usable_without_posix_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, 'fcntl', None)
    assert guard.reconcile(tmp_path) is True
    (tmp_path / guard.RECEIPT).write_text('{}')
    assert guard.reconcile(tmp_path) is False
    with pytest.raises(OSError, match='locking unavailable'):
        guard._lock_fd(tmp_path, 'fixture.lock')


async def test_another_live_operation_prevents_orphan_host_stop(tmp_path, identities, monkeypatch):
    caller, daemon, active = identities
    async with guard.Operation('/installed/cua-driver', '/synthetic/socket', root=tmp_path):
        active.pop(caller['pid'])
        monkeypatch.setattr(guard, '_stop_orphan', lambda _: pytest.fail('active RPC must retain host'))
        assert guard.reconcile(tmp_path) is False
