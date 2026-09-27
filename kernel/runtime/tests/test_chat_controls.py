from __future__ import annotations

import asyncio

import pytest

from drivers.mock.fixtures import hold_script
from runtime.session_host import StaleRunError
from drivers.base import TurnAlreadyRunningError
from runtime.tests.test_session_host import make_world, next_run_started, drive_until_run_terminal


async def test_settings_serialize_with_send_and_reject_during_a_turn():
    world = make_world()
    _, conversation = await world.add_backend(script=hold_script(run_id="settings-race"))
    await world.host.start_runtime(conversation)
    try:
        async with world.host.conversation_control(conversation.id):
            sending = asyncio.create_task(world.host.send_message(conversation.id, "hello"))
            await asyncio.sleep(0)
            assert not sending.done()
        await sending
        with pytest.raises(TurnAlreadyRunningError):
            async with world.host.conversation_control(conversation.id):
                pass
    finally:
        await world.host.aclose()


async def test_stop_for_previous_run_waits_for_send_then_rejects_without_forwarding(monkeypatch):
    world = make_world()
    driver, conversation = await world.add_backend(script=hold_script(run_id="first"))
    host = world.host
    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    try:
        await host.send_message(conversation.id, "first")
        first = (await next_run_started(subscription)).event.run_id
        await host.interrupt(conversation.id, expected_run_id=first)
        await drive_until_run_terminal(host, subscription, conversation.id)
        driver.set_script(conversation.id, hold_script(run_id="second", message_id="second-msg"))
        entered = asyncio.Event()
        release = asyncio.Event()
        original_send = driver.send_message
        original_stop = driver.interrupt
        stops = []

        async def delayed(runtime, content):
            entered.set()
            await release.wait()
            await original_send(runtime, content)

        async def record_stop(runtime):
            stops.append(runtime.runtime_id)
            await original_stop(runtime)

        monkeypatch.setattr(driver, "send_message", delayed)
        monkeypatch.setattr(driver, "interrupt", record_stop)
        sending = asyncio.create_task(host.send_message(conversation.id, "second"))
        await asyncio.wait_for(entered.wait(), 2)
        stopping = asyncio.create_task(host.interrupt(conversation.id, expected_run_id=first))
        await asyncio.sleep(0)
        assert not stopping.done()
        assert not stops
        release.set()
        await sending
        with pytest.raises(StaleRunError):
            await stopping
        assert not stops
        second = (await next_run_started(subscription)).event.run_id
        assert second != first
        assert host.timeline(conversation.id).run_state == "running"
        await host.interrupt(conversation.id, expected_run_id=second)
        assert len(stops) == 1
    finally:
        subscription.close()
        await host.aclose()


async def test_workspace_metadata_event_is_preserved_without_a_visible_card(tmp_path):
    world = make_world()
    driver, conversation = await world.add_backend()
    original = driver.start_runtime

    async def start_with_workspace(*args, **kwargs):
        handle = await original(*args, **kwargs)
        return handle.model_copy(update={"metadata": {"workspaceRoot": str(tmp_path)}})

    driver.start_runtime = start_with_workspace
    await world.host.start_runtime(conversation)
    try:
        events = await world.event_store.replay(conversation.id)
        workspace = next(e for e in events if e.event.type == "extension.event" and e.event.name == "runtime.workspace")
        assert workspace.event.data["workspaceRoot"] == str(tmp_path)
        assert not any(getattr(item, "name", None) == "runtime.workspace" for item in world.host.timeline(conversation.id).items)
    finally:
        await world.host.aclose()
