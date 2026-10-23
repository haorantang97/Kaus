"""A retired computer-tool transport resumes through the normal SessionHost."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpRpcError
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.computer_use import lease as lease_module
from drivers.computer_use import integration
from drivers.computer_use.lease import ResourceLease
from runtime.tests.test_session_host import make_world


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_closed_computer_transport_retires_once_and_next_turn_resumes_native_session(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(lease_module, "default_lease_root", lambda: tmp_path / "locks")
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key="synthetic")
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key="synthetic")
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    conversation = conversation.evolve(model_id="small", title="Persistent configuration")
    await world.conversations.save(conversation)
    failed = False
    async def fail_once(*_):
        nonlocal failed
        if not failed:
            failed = True
            raise AcpRpcError(-32000, "Synthetic tool service failed")
    monkeypatch.setattr(driver, "_runtime_adapter", lambda: SimpleNamespace(**{stage + "_runtime_prompt": fail_once}))
    try:
        first = await world.host.ensure_runtime(conversation)
        state = driver._runtimes[first.runtime_id]
        state.runtime_metadata["_kausComputerUse"] = {"resources": ["desktop:local"]}
        await world.host.send_message(conversation.id, "Synthetic first turn")
        await wait_for(lambda: not world.host.is_active(conversation.id))
        # The event pump itself performed retirement without awaiting itself or
        # adding a second failure. Native ID and conversation settings survive.
        events = await world.event_store.replay(conversation.id)
        assert [e.event.type for e in events if e.event.type in
                ("run.completed", "run.failed", "run.interrupted")] == ["run.failed"]
        assert state.connection.close_confirmed
        assert not state.translator.pending_permissions() and not state.fs_writes
        assert await world.leases.describe(conversation.id) is None
        saved = await world.conversations.get(conversation.id)
        assert saved.native_session_id == first.native_session_id
        assert saved.model_id == "small" and saved.title == "Persistent configuration"
        async with ResourceLease("desktop:local", timeout=0):
            pass
        second = await world.host.ensure_runtime(saved)
        assert second.runtime_id != first.runtime_id
        assert second.native_session_id == first.native_session_id and second.metadata["resumed"]
        assert driver._runtimes[second.runtime_id].connection is not state.connection
        await world.host.send_message(conversation.id, "Synthetic retry")
        async with asyncio.timeout(5):
            while (await world.event_store.replay(conversation.id))[-1].event.type != "run.completed":
                await asyncio.sleep(.01)
        events = await world.event_store.replay(conversation.id)
        assert [e.event.type for e in events if e.event.type in
                ("run.completed", "run.failed", "run.interrupted")] == ["run.failed", "run.completed"]
        assert world.host.is_active(conversation.id)
    finally:
        await world.host.aclose()


async def test_deleting_completed_conversation_retains_background_owner_until_native_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_module, "default_lease_root", lambda: tmp_path / "locks")
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))
    monkeypatch.setattr(integration, "_QUARANTINE_POLL_SECONDS", .01)
    monkeypatch.setattr(integration, "_quarantined", {})
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key="synthetic")
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key="synthetic")
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    completed = False
    observed = []

    async def complete(spec, native_id, metadata):
        observed.append(native_id)
        return completed

    stop = AsyncMock(side_effect=AssertionError("do not abort successful background work"))
    monkeypatch.setattr(driver, "_runtime_adapter", lambda: SimpleNamespace(
        confirm_computer_use_complete=complete, stop_computer_use=stop))
    try:
        handle = await world.host.ensure_runtime(conversation)
        state = driver._runtimes[handle.runtime_id]
        state.runtime_metadata["_kausComputerUse"] = {"resources": ["desktop:local"]}
        await world.host.send_message(conversation.id, "Synthetic background turn")
        async with asyncio.timeout(5):
            while not any(e.event.type == "run.completed" for e in await world.event_store.replay(conversation.id)):
                await asyncio.sleep(.01)
        assert "desktop:local" in integration._quarantined
        # Same lifecycle as DELETE: close the runtime, then remove Kaus history.
        await world.host.stop_runtime(conversation.id)
        await world.conversations.delete(conversation.id)
        await world.host.purge_conversation(conversation)
        assert "desktop:local" in integration._quarantined
        assert await integration.reconcile_resources() == ("desktop:local",)
        stop.assert_not_awaited()
        completed = True
        await wait_for(lambda: not integration._quarantined)
        assert observed and set(observed) == {handle.native_session_id}
        async with ResourceLease("desktop:local", timeout=0):
            pass
    finally:
        completed = True
        await integration.reconcile_resources()
        await world.host.aclose()


async def test_cancelled_prompt_retires_computer_runtime_after_confirmed_stop_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_module, "default_lease_root", lambda: tmp_path / "locks")
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))
    driver = AcpDriver(fake_agent_spec("interrupt", cwd=str(tmp_path)), backend_key="synthetic")
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key="synthetic")
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(driver, "_runtime_adapter", lambda: SimpleNamespace(stop_computer_use=stop))
    try:
        first = await world.host.ensure_runtime(conversation)
        state = driver._runtimes[first.runtime_id]
        state.runtime_metadata["_kausComputerUse"] = {"resources": ["desktop:local"]}
        await world.host.send_message(conversation.id, "Synthetic cancellable turn")
        async with asyncio.timeout(5):
            while not any(e.event.type == "message.delta" for e in await world.event_store.replay(conversation.id)):
                await asyncio.sleep(.01)
        await world.host.interrupt(conversation.id)
        await wait_for(lambda: not world.host.is_active(conversation.id))
        events = await world.event_store.replay(conversation.id)
        assert [e.event.type for e in events if e.event.type in
                ("run.completed", "run.failed", "run.interrupted")] == ["run.interrupted"]
        stop.assert_awaited_once()
        assert state.transport_retired and state.connection.close_confirmed
        assert await world.leases.describe(conversation.id) is None
        async with ResourceLease("desktop:local", timeout=0):
            pass
        saved = await world.conversations.get(conversation.id)
        second = await world.host.ensure_runtime(saved)
        assert second.runtime_id != first.runtime_id
        assert second.native_session_id == first.native_session_id and second.metadata["resumed"]
    finally:
        await world.host.aclose()
