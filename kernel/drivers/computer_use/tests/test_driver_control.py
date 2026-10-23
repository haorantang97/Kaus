"""Real resource locks around synthetic ACP turns, without starting an Agent."""

import asyncio
from types import SimpleNamespace

import pytest

from drivers.acp.driver import AcpDriver
from drivers.computer_use import integration, lease as lease_module
from drivers.computer_use.lease import ResourceBusyError, ResourceLease


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class Connection:
    def __init__(self, *, failure=None, gate=None):
        self.failure, self.gate = failure, gate
        self.closing = asyncio.Event()
        self.closed = False
        self.notifications = []

    async def aclose(self):
        self.closing.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        self.closed = True

    def notify(self, *args):
        self.notifications.append(args)


def driver_state(*, resource="desktop:local", connection=None, stop=None, complete=None):
    driver = AcpDriver.__new__(AcpDriver)
    driver._runtime_adapter = lambda: SimpleNamespace(
        **({"stop_computer_use": stop} if stop else {}),
        **({"confirm_computer_use_complete": complete} if complete else {}))
    state = SimpleNamespace(
        runtime_metadata={"_kausComputerUse": {"resources": [resource]}},
        runtime_spec=object(), session_id="synthetic-session",
        computer_waiting=False, interrupt_requested=False,
        connection=connection or Connection(), prompt_task=None,
    )
    return driver, state


@pytest.fixture
def locks(monkeypatch, tmp_path):
    acquired = []

    def make(resource, **kwargs):
        result = ResourceLease(resource, root=tmp_path, poll_interval=0.001, **kwargs)
        acquired.append(result)
        return result

    monkeypatch.setattr(lease_module, "ResourceLease", make)
    monkeypatch.setattr(integration, "_quarantined", {})
    yield SimpleNamespace(root=tmp_path, made=acquired)
    for item in acquired:
        item.release()
    integration._quarantined.clear()


async def test_two_drivers_serialize_shared_desktop_for_whole_turn(locks):
    first, first_state = driver_state()
    second, second_state = driver_state()
    entered, finish, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def invoke_first():
        entered.set()
        await finish.wait()
        return "first"

    async def invoke_second():
        second_entered.set()
        return "second"

    one = asyncio.create_task(first._computer_prompt(first_state, invoke_first))
    await entered.wait()
    two = asyncio.create_task(second._computer_prompt(second_state, invoke_second))
    await eventually(lambda: second_state.computer_waiting)
    assert not second_entered.is_set()
    finish.set()
    assert await one == "first"
    assert await two == "second"
    assert all(not item.held for item in locks.made)


async def test_separate_browser_contexts_can_run_concurrently(locks):
    first, first_state = driver_state(resource="browser:context:first")
    second, second_state = driver_state(resource="browser:context:second")
    count = 0
    entered, finish = asyncio.Event(), asyncio.Event()

    async def invoke():
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await finish.wait()

    tasks = [asyncio.create_task(driver._computer_prompt(state, invoke))
             for driver, state in ((first, first_state), (second, second_state))]
    await asyncio.wait_for(entered.wait(), 2)
    finish.set()
    await asyncio.gather(*tasks)
    assert all(not item.held for item in locks.made)


async def test_interrupt_cancels_waiting_turn_without_starting_or_closing_owner(locks):
    owner = ResourceLease("desktop:local", root=locks.root)
    await owner.acquire()
    driver, state = driver_state()
    called = []

    async def capabilities():
        return SimpleNamespace(card=SimpleNamespace(interrupt=True))

    async def invoke():
        called.append(True)

    driver.get_capabilities = capabilities
    driver._state = lambda _: state
    driver._cancel_pending_interactions = lambda _: None
    state.prompt_task = asyncio.create_task(driver._computer_prompt(state, invoke))
    try:
        await eventually(lambda: state.computer_waiting)
        await driver.interrupt(object())
        assert state.prompt_task.cancelled()
        assert owner.held
        assert not called
        assert not state.connection.closed
        assert not state.connection.notifications
        assert not integration._quarantined
    finally:
        owner.release()


async def test_cancel_during_second_resource_wait_releases_first_resource(locks):
    owner = ResourceLease("desktop:local", root=locks.root)
    await owner.acquire()
    driver, state = driver_state()
    state.runtime_metadata["_kausComputerUse"]["resources"].append("browser:shared")

    async def invoke():
        raise AssertionError("must not start")

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    try:
        await eventually(lambda: len(locks.made) == 2 and locks.made[1].waiting)
        assert locks.made[0].held
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not locks.made[0].held
        assert owner.held
    finally:
        owner.release()


async def test_preexisting_interrupt_does_not_invoke_model(locks):
    driver, state = driver_state()
    state.interrupt_requested = True

    async def invoke():
        raise AssertionError("must not start")

    assert await driver._computer_prompt(state, invoke) == ({"stopReason": "cancelled"}, None)
    assert not state.connection.closed
    assert all(not item.held for item in locks.made)


async def test_restricted_runtime_discards_prior_control_projection(locks, tmp_path, monkeypatch):
    from drivers.acp.presets import AcpPreset
    from drivers.acp.testing.harness import FakeAcpHarness
    from drivers.base import CreateSessionOptions
    from drivers import workbench_execution

    async def restricted_prepare(spec, metadata, versions):
        return spec, dict(metadata)

    preset = AcpPreset(id="restricted-control-test", label="Test", command=("unused",),
                       restricted_execution_adapter="fixture")
    harness = FakeAcpHarness(preset=preset, workspace_root=str(tmp_path))
    driver = harness.make_driver()
    harness.arrange_script(driver, "text-stream")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    monkeypatch.setattr(workbench_execution, "adapter_for",
                        lambda _: SimpleNamespace(prepare=restricted_prepare))
    runtime = await driver.start_runtime(
        harness.make_conversation(project, binding), "card",
        session_options=CreateSessionOptions(workspace_root=str(tmp_path), metadata={
            "_kausExecutionPolicy": "context-only",
            "_kausComputerUse": {"resources": ["desktop:local"]},
        }))
    try:
        state = driver._state(runtime)
        assert "_kausComputerUse" not in state.runtime_metadata

        async def invoke():
            return "restricted"

        assert await driver._computer_prompt(state, invoke) == "restricted"
        assert not locks.made
    finally:
        await driver.stop_runtime(runtime)


async def test_runtime_failure_closes_owned_process_before_releasing(locks):
    gate = asyncio.Event()
    connection = Connection(gate=gate)
    driver, state = driver_state(connection=connection)

    async def invoke():
        raise ValueError("synthetic failure")

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    await connection.closing.wait()
    assert locks.made[0].held
    gate.set()
    with pytest.raises(ValueError, match="synthetic failure"):
        await task
    assert connection.closed
    assert not locks.made[0].held
    assert not integration._quarantined


async def test_remote_stop_confirmation_retains_then_reconciles_lease(locks):
    confirmed = False
    seen = []

    async def stop(spec, session, metadata):
        seen.append(session)
        return confirmed

    driver, state = driver_state(stop=stop)

    async def invoke():
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError):
        await driver._computer_prompt(state, invoke)
    assert state.connection.closed and seen == ["synthetic-session"]
    assert locks.made[0].held
    with pytest.raises(ResourceBusyError):
        async with ResourceLease("desktop:local", root=locks.root, timeout=0):
            pass
    confirmed = True
    assert await integration.reconcile_resources() == ()
    assert not locks.made[0].held


async def test_transport_close_failure_cannot_release_even_if_remote_confirms(locks):
    async def stop(*args):
        return True

    driver, state = driver_state(connection=Connection(failure=OSError("close failed")), stop=stop)

    async def invoke():
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        await driver._computer_prompt(state, invoke)
    assert locks.made[0].held
    assert await integration.reconcile_resources() == ("desktop:local",)
    assert locks.made[0].held


async def test_next_turn_retries_failed_shutdown_before_acquiring_resource(locks):
    connection = Connection(failure=OSError("temporary close failure"))
    driver, state = driver_state(connection=connection)

    async def fail():
        raise ValueError("original failure")

    with pytest.raises(ValueError):
        await driver._computer_prompt(state, fail)
    assert locks.made[0].held
    connection.failure = None
    next_driver, next_state = driver_state()

    async def succeed():
        assert connection.closed and not locks.made[0].held
        return "success"

    assert await next_driver._computer_prompt(next_state, succeed) == "success"
    assert not integration._quarantined


async def test_closed_flag_without_shutdown_confirmation_keeps_lease(locks):
    connection = Connection()
    connection.close_confirmed = False
    driver, state = driver_state(connection=connection)

    async def fail():
        raise ValueError("original failure")

    with pytest.raises(ValueError):
        await driver._computer_prompt(state, fail)
    assert connection.closed and locks.made[0].held
    assert await integration.reconcile_resources() == ("desktop:local",)
    connection.close_confirmed = True
    assert await integration.reconcile_resources() == ()
    assert not locks.made[0].held


async def test_second_cancel_during_shutdown_keeps_lock_until_background_cleanup_finishes(locks):
    gate, entered = asyncio.Event(), asyncio.Event()
    connection = Connection(gate=gate)
    driver, state = driver_state(connection=connection)

    async def invoke():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    await entered.wait()
    task.cancel()
    await connection.closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert locks.made[0].held
    assert "desktop:local" in integration._quarantined
    gate.set()
    await eventually(lambda: not locks.made[0].held)
    assert connection.closed
    assert not integration._quarantined


@pytest.mark.parametrize("stop_reason,interrupted", [("cancelled", False), ("end_turn", True)])
async def test_cancel_receipt_waits_for_native_stop_before_releasing_control(locks, stop_reason, interrupted):
    confirming, stopped = asyncio.Event(), asyncio.Event()

    async def stop(*_):
        confirming.set()
        await stopped.wait()
        return True

    driver, state = driver_state(stop=stop)

    async def invoke():
        state.interrupt_requested = interrupted
        # Matches bridges which return a terminal receipt before remote abort.
        return {"stopReason": stop_reason}, None

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    await confirming.wait()
    assert state.connection.closed and state.transport_retired
    assert not task.done() and locks.made[0].held
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=locks.root, timeout=0).acquire()
    stopped.set()
    assert await task == ({"stopReason": stop_reason}, None)
    assert not locks.made[0].held and not integration._quarantined


async def test_cancel_receipt_quarantines_unconfirmed_remote_work_then_reconciles(locks):
    confirmed = False

    async def stop(*_):
        return confirmed

    driver, state = driver_state(stop=stop)

    async def invoke():
        return {"stopReason": "cancelled"}, None

    assert await driver._computer_prompt(state, invoke) == ({"stopReason": "cancelled"}, None)
    assert state.transport_retired and locks.made[0].held
    assert await integration.reconcile_resources() == ("desktop:local",)
    confirmed = True
    assert await integration.reconcile_resources() == ()
    assert not locks.made[0].held


async def test_repeated_cancel_during_success_receipt_shutdown_preserves_control(locks):
    gate = asyncio.Event()
    connection = Connection(gate=gate)
    driver, state = driver_state(connection=connection)

    async def invoke():
        return {"stopReason": "cancelled"}, None

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    await connection.closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert locks.made[0].held and integration._quarantined
    gate.set()
    await eventually(lambda: not locks.made[0].held)
    assert connection.closed and not integration._quarantined


@pytest.mark.parametrize("resources,stop_reason", [(["desktop:local"], "end_turn"), ([], "cancelled")])
async def test_completed_turn_and_text_only_cancellation_keep_ordinary_runtime(locks, resources, stop_reason):
    async def stop(*_):
        raise AssertionError("must not stop ordinary runtime")

    driver, state = driver_state(stop=stop)
    state.runtime_metadata["_kausComputerUse"]["resources"] = resources

    async def invoke():
        return {"stopReason": stop_reason}, None

    assert await driver._computer_prompt(state, invoke) == ({"stopReason": stop_reason}, None)
    assert not state.connection.closed and not getattr(state, "transport_retired", False)
    assert not integration._quarantined and all(not lease.held for lease in locks.made)


@pytest.mark.parametrize("first_status", [False, None, {"status": "unknown"}, OSError("unavailable")])
async def test_successful_reply_keeps_background_control_until_read_only_completion(locks, monkeypatch, first_status):
    monkeypatch.setattr(integration, "_QUARANTINE_POLL_SECONDS", .01)
    completed, entered = False, asyncio.Event()
    seen = []

    async def complete(spec, session_id, metadata):
        seen.append(session_id)
        if completed:
            return True
        if isinstance(first_status, Exception):
            raise first_status
        return first_status

    async def stop(*_):
        raise AssertionError("success must not abort planned background work")

    driver, state = driver_state(stop=stop, complete=complete)

    async def invoke():
        return {"stopReason": "end_turn"}, None

    assert await driver._computer_prompt(state, invoke) == ({"stopReason": "end_turn"}, None)
    assert not state.connection.closed and not getattr(state, "transport_retired", False)
    assert locks.made[0].held
    next_driver, next_state = driver_state()

    async def next_invoke():
        entered.set()
        return "next"

    next_turn = asyncio.create_task(next_driver._computer_prompt(next_state, next_invoke))
    await eventually(lambda: next_state.computer_waiting)
    assert not entered.is_set()
    completed = True
    assert await asyncio.wait_for(next_turn, 2) == "next"
    assert not integration._quarantined and seen == [state.session_id] * len(seen)


async def test_interrupt_during_completion_check_switches_to_confirmed_stop(locks):
    checking, observed = asyncio.Event(), asyncio.Event()
    stops = []

    async def complete(*_):
        checking.set()
        await observed.wait()
        return True

    async def stop(*_):
        stops.append(True)
        return True

    driver, state = driver_state(stop=stop, complete=complete)

    async def invoke():
        return {"stopReason": "end_turn"}, None

    task = asyncio.create_task(driver._computer_prompt(state, invoke))
    await checking.wait()
    state.interrupt_requested = True
    observed.set()
    assert await task == ({"stopReason": "end_turn"}, None)
    assert state.transport_retired and state.connection.closed and stops == [True]
    assert not integration._quarantined and not locks.made[0].held


async def test_cancelled_remote_work_automatically_unblocks_an_already_waiting_turn(locks, monkeypatch):
    monkeypatch.setattr(integration, "_QUARANTINE_POLL_SECONDS", .01)
    stopped, entered = False, asyncio.Event()

    async def stop(spec, session_id, metadata):
        assert session_id == "synthetic-session"
        return stopped

    driver, state = driver_state(stop=stop)

    async def invoke():
        return {"stopReason": "cancelled"}, None

    await driver._computer_prompt(state, invoke)
    assert state.connection.closed and locks.made[0].held
    following, next_state = driver_state()

    async def next_invoke():
        entered.set()
        return "next"

    task = asyncio.create_task(following._computer_prompt(next_state, next_invoke))
    await eventually(lambda: next_state.computer_waiting)
    assert not entered.is_set()
    stopped = True
    assert await asyncio.wait_for(task, 2) == "next"
    assert not integration._quarantined


async def test_remote_card_persists_before_dispatch_and_clears_only_after_completion(locks):
    import json
    seen = []
    driver, state = driver_state()
    state.handle = SimpleNamespace(backend_id="backend:fixture", binding_id="binding:fixture",
                                   conversation_id="conversation:fixture")

    async def reference(*_):
        return {"nativeSessionId": state.session_id, "sessionKey": "source:exact", "connectionId": "hash"}

    async def complete(*_):
        seen.append(True)
        return True

    driver._runtime_adapter = lambda: SimpleNamespace(computer_use_reference=reference,
        confirm_computer_use_complete=complete)

    async def invoke():
        stored = json.loads(locks.made[0].reservation_path.read_text())
        assert stored["kind"] == "card" and stored["phase"] == "dispatched"
        assert stored["reference"]["nativeSessionId"] == state.session_id
        assert stored["reference"]["backendId"] == "backend:fixture"
        return {"stopReason": "end_turn"}, None

    await driver._computer_prompt(state, invoke)
    assert seen == [True] and not locks.made[0].reservation_path.exists()


async def test_cancel_during_preflight_reference_does_not_dispatch_or_persist(locks):
    driver, state = driver_state()

    async def reference(*_):
        state.interrupt_requested = True
        return {}

    driver._runtime_adapter = lambda: SimpleNamespace(computer_use_reference=reference)

    async def invoke():
        raise AssertionError("must not dispatch after cancellation")

    assert await driver._computer_prompt(state, invoke) == ({"stopReason": "cancelled"}, None)
    assert not state.connection.closed and not locks.made[0].reservation_path.exists()
