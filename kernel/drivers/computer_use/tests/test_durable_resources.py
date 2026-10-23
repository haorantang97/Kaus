"""Remote reservations survive a backend crash without retaining credentials."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from drivers.computer_use import durable, lease as lease_module
from drivers.computer_use.lease import ResourceBusyError, ResourceLease
from drivers.terminals.managed_process import process_identity


REFERENCE = {"backendId":"backend:fixture", "bindingId":"binding:fixture",
             "conversationId":"conversation:fixture", "nativeSessionId":"native-fixture",
             "sessionKey":"agent:source:kaus-" + "b" * 24,
             "connectionId":"connection-fixture"}


class Resolver:
    def __init__(self, result=False):
        self.result, self.calls = result, []
        self.connection = REFERENCE["connectionId"]

    async def confirm(self, reference):
        self.calls.append(copy.deepcopy(reference))
        if reference.get("connectionId") != self.connection:
            return False
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    monkeypatch.setattr(lease_module, "default_lease_root", lambda: root)
    monkeypatch.setattr(durable, "_resolvers", [])
    monkeypatch.setattr(durable, "_monitors", {})
    monkeypatch.setattr(durable, "POLL_SECONDS", .01)
    yield root
    tasks = tuple(durable._monitors.values())
    for task in tasks:
        task.cancel()


async def crashed_reservation(root, reference=None, *, transport=None, dispatched=True):
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", """
import asyncio,json,os,sys
from drivers.computer_use.lease import ResourceLease
async def main():
    lease=ResourceLease('desktop:local',root=sys.argv[1])
    await lease.acquire()
    lease.reserve_card('unique-turn-owner',json.loads(sys.argv[2]),transport=json.loads(sys.argv[3]))
    if sys.argv[4] == 'dispatched':
        lease.mark_card_dispatched()
    os._exit(0)
asyncio.run(main())
""", str(root), json.dumps(reference or REFERENCE), json.dumps(transport),
        "dispatched" if dispatched else "prepared",
        env={"PATH":os.defpath,"PYTHONPATH":str(Path(__file__).resolve().parents[3]),
             "PYTHONDONTWRITEBYTECODE":"1", "SYNTHETIC_PRIVATE_TOKEN":"must-not-be-persisted"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, error = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, error.decode()
    assert process_identity(process.pid) is None
    path = ResourceLease("desktop:local", root=root).reservation_path
    assert path.exists()
    return path


async def busy(root):
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=root, timeout=0).acquire()


async def test_crashed_backend_reservation_survives_until_remote_completion(recovery):
    path = await crashed_reservation(recovery)
    await busy(recovery)
    resolver = Resolver(False)
    durable.register_resolver(resolver.confirm)
    assert await durable.reconcile_persisted() == ("desktop:local",)
    await busy(recovery)
    assert path.exists() and resolver.calls == [REFERENCE]
    resolver.result = True
    async with asyncio.timeout(5):
        while path.exists():
            await asyncio.sleep(.01)
    async with ResourceLease("desktop:local", root=recovery, timeout=0):
        pass


async def test_crash_before_dispatch_releases_prepared_reservation_without_query(recovery):
    path = await crashed_reservation(recovery, dispatched=False)
    resolver = Resolver(RuntimeError("must not query an undispatched turn"))
    durable.register_resolver(resolver.confirm)
    assert await durable.reconcile_persisted() == ()
    assert not path.exists() and not resolver.calls
    async with ResourceLease("desktop:local", root=recovery, timeout=0):
        pass


@pytest.mark.parametrize("result", [False, None, 1, {"complete":True}, RuntimeError("offline")])
async def test_unknown_and_nonboolean_confirmation_keep_ownership(recovery, result):
    path = await crashed_reservation(recovery)
    resolver = Resolver(result)
    durable.register_resolver(resolver.confirm)
    await durable.reconcile_persisted()
    await busy(recovery)
    assert path.exists()


async def test_waiting_next_turn_is_released_by_recovery_monitor(recovery):
    path = await crashed_reservation(recovery)
    resolver = Resolver(False)
    durable.register_resolver(resolver.confirm)
    await durable.reconcile_persisted()
    waiting = asyncio.Event()
    next_lease = ResourceLease("desktop:local", root=recovery, timeout=5,
                               poll_interval=.01, on_waiting=waiting.set)
    acquire = asyncio.create_task(next_lease.acquire())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not acquire.done()
        resolver.result = True
        await asyncio.wait_for(acquire, 5)
        assert next_lease.held and not path.exists()
    finally:
        if not acquire.done():
            acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
        next_lease.release()


async def test_recovery_reference_contains_no_process_environment_or_native_receipt(recovery):
    path = await crashed_reservation(recovery)
    text = path.read_text()
    journal = json.loads(text)
    assert journal["reference"] == REFERENCE
    assert not {"env","command","receipt","metadata","token"} & journal["reference"].keys()
    assert "must-not-be-persisted" not in text and "SYNTHETIC_PRIVATE_TOKEN" not in text
    assert path.stat().st_mode & 0o777 == 0o600


async def test_changed_connection_does_not_release_old_reservation(recovery):
    path = await crashed_reservation(recovery)
    resolver = Resolver(True)
    resolver.connection = "different-current-account"
    durable.register_resolver(resolver.confirm)
    assert await durable.reconcile_persisted() == ("desktop:local",)
    await busy(recovery)
    assert path.exists()


async def test_deleted_conversation_does_not_remove_recovery_reference(recovery):
    path = await crashed_reservation(recovery)
    conversations = {REFERENCE["conversationId"]:object()}
    conversations.clear()
    resolver = Resolver(True)
    durable.register_resolver(resolver.confirm)
    assert await durable.reconcile_persisted() == ()
    assert resolver.calls == [REFERENCE]
    assert not path.exists() and not conversations


async def test_previous_terminal_result_cannot_release_while_submitter_lives(recovery):
    transport = subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        identity = process_identity(transport.pid)
        assert identity and identity["pid"] == identity["pgid"]
        path = await crashed_reservation(recovery, transport=identity)
        resolver = Resolver(True)  # A stale prior-turn terminal projection.
        durable.register_resolver(resolver.confirm)
        await durable.reconcile_persisted()
        await busy(recovery)
        assert path.exists() and not resolver.calls
    finally:
        transport.terminate()
        await asyncio.to_thread(transport.wait, 5)


async def test_prepared_reservation_waits_until_transport_exits_then_recovers(recovery):
    transport = subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        identity = process_identity(transport.pid)
        assert identity and identity["pid"] == identity["pgid"]
        path = await crashed_reservation(recovery, transport=identity, dispatched=False)
        resolver = Resolver(RuntimeError("must not query an undispatched turn"))
        durable.register_resolver(resolver.confirm)
        assert await durable.reconcile_persisted() == ("desktop:local",)
        await busy(recovery)
        assert path.exists() and not resolver.calls
        transport.terminate()
        await asyncio.to_thread(transport.wait, 5)
        async with asyncio.timeout(5):
            while path.exists():
                await asyncio.sleep(.01)
        assert not resolver.calls
    finally:
        if transport.poll() is None:
            transport.terminate()
            await asyncio.to_thread(transport.wait, 5)


async def test_stale_recovery_owner_cannot_clear_replacement(recovery):
    path = await crashed_reservation(recovery)
    class ReplacingResolver(Resolver):
        async def confirm(self, reference):
            value = json.loads(path.read_text())
            value["launchId"] = "new-owner"
            path.write_text(json.dumps(value))
            return True
    resolver = ReplacingResolver()
    durable.register_resolver(resolver.confirm)
    await durable.reconcile_persisted()
    assert json.loads(path.read_text())["launchId"] == "new-owner"
    await busy(recovery)


def test_startup_recovers_computer_resources_before_conversation_reconciliation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root))
    import session_bootstrap
    from runtime.session_host import SessionHost
    order = []
    async def recover():
        order.append("computer")
        return ()
    async def conversations(self):
        order.append("conversations")
        return ()
    monkeypatch.setattr(durable,"reconcile_persisted",recover)
    monkeypatch.setattr(SessionHost,"reconcile_startup",conversations)
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR,"1")
    app=FastAPI()
    runtime=session_bootstrap.attach_session_api(app,db_path=tmp_path/"domain.sqlite3",
        config_loader=lambda:{"features":{"session_host_background":False}},log=lambda _:None)
    async def verify():
        try:
            hook=next(handler for handler in app.router.on_startup if handler.__name__=="_reconcile")
            await hook()
            assert order==["computer","conversations"]
        finally:
            await runtime.aclose()
    asyncio.run(verify())
