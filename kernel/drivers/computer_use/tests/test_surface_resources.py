"""Computer resources follow the existing card/native-terminal handoff."""
import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from app.conversations.models import Conversation
from app.tests.test_surface_handoff_api import Harness
from drivers.base import CliLaunchSpec
from drivers.computer_use import lease as resource_module
from drivers.computer_use import integration
from drivers.computer_use.lease import ResourceBusyError, ResourceLease
from runtime.surface_handoff import SurfaceConflictError, SurfaceCoordinator


async def setup_harness(tmp_path, monkeypatch, *, remote=False):
    monkeypatch.setattr(resource_module, "default_lease_root", lambda: tmp_path / "locks")
    harness = Harness(tmp_path)
    await harness.seed()
    conversation = await harness.repositories.conversations.save(Conversation.create(
        project_id=harness.project_id, agent_binding_id=harness.binding.id, title="Synthetic terminal"))
    harness.driver.external_computer_use = lambda _: {"resources": ("desktop:local",), "remote": remote}
    harness.driver.stop_external_computer_use = AsyncMock(return_value=True)
    return harness, conversation


async def assert_busy():
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", timeout=0).acquire()


async def test_idle_handoff_keeps_resources_across_coordinator_restart_until_native_group_stops(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch)
    marker = tmp_path / "native.ready"
    harness.run_script = True
    harness.driver.build_external_cli_launch = AsyncMock(return_value=CliLaunchSpec(command=(sys.executable,
        "-c", f"import time;open({str(marker)!r},'w').close();time.sleep(60)"), terminal_integration="codex"))
    handoff = None
    try:
        handoff = await harness.coordinator.to_external(conversation)
        assert handoff.launched
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        await assert_busy()
        # No in-memory lease list is needed to preserve ownership or return it.
        restarted = SurfaceCoordinator(session_host=harness.host, repositories=harness.repositories,
            registry=harness.registry, launcher=harness.launcher)
        await restarted.to_card(handoff.conversation, force=True)
        async with ResourceLease("desktop:local", timeout=0):
            pass
        assert await harness.host.leases.describe(conversation.id) is None
    finally:
        if handoff:
            await harness.launcher.stop(handoff.launch)
        await harness.aclose()


async def test_busy_desktop_does_not_open_terminal_or_release_current_owner(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch)
    try:
        async with ResourceLease("desktop:local"):
            with pytest.raises(SurfaceConflictError, match="电脑工具正在使用"):
                await harness.coordinator.to_external(conversation)
            assert harness.opened == []
            await assert_busy()
        assert await harness.host.leases.describe(conversation.id) is None
    finally:
        await harness.aclose()


async def test_remote_task_requires_native_stop_confirmation_before_unlocking(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch, remote=True)
    harness.run_script = True
    harness.driver.build_external_cli_launch = AsyncMock(return_value=CliLaunchSpec(
        command=(sys.executable, "-c", "import time;time.sleep(60)")))
    try:
        handoff = await harness.coordinator.to_external(conversation)
        async with asyncio.timeout(5):
            while not (harness.launcher.root / handoff.launch.id / "process.json").exists():
                await asyncio.sleep(.01)
        harness.driver.stop_external_computer_use.return_value = False
        with pytest.raises(SurfaceConflictError, match="终端工具仍在运行"):
            await harness.coordinator.to_card(handoff.conversation, force=True)
        await assert_busy()
        assert await harness.host.leases.describe(conversation.id) is not None
        harness.driver.stop_external_computer_use.return_value = True
        await harness.coordinator.to_card(handoff.conversation, force=True)
        async with ResourceLease("desktop:local", timeout=0):
            pass
    finally:
        harness.driver.stop_external_computer_use.return_value = True
        await harness.aclose()


async def test_launch_failure_clears_only_its_reservation(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch)
    harness.launcher._opener = lambda *_: (False, "Synthetic opener failure")
    try:
        handoff = await harness.coordinator.to_external(conversation)
        assert not handoff.launched
        async with ResourceLease("desktop:local", timeout=0):
            pass
        assert await harness.host.leases.describe(conversation.id) is None
    finally:
        await harness.aclose()


async def test_never_started_remote_launch_does_not_wait_for_remote_stop(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch, remote=True)
    harness.driver.stop_external_computer_use.return_value = False
    harness.launcher._opener = lambda *_: (False, "Synthetic opener failure")
    try:
        handoff = await harness.coordinator.to_external(conversation)
        assert not handoff.launched
        harness.driver.stop_external_computer_use.assert_not_awaited()
        assert await harness.host.leases.describe(conversation.id) is None
        async with ResourceLease("desktop:local", timeout=0):
            pass
    finally:
        await harness.aclose()


async def test_unconfirmed_native_group_stop_keeps_shared_and_conversation_ownership(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch)
    try:
        handoff = await harness.coordinator.to_external(conversation)
        harness.launcher.stop = AsyncMock(return_value=(False, "Synthetic child still active"))
        with pytest.raises(SurfaceConflictError, match="终端工具仍在运行"):
            await harness.coordinator.to_card(handoff.conversation, force=True)
        await assert_busy()
        assert await harness.host.leases.describe(conversation.id) is not None
        harness.launcher.stop.return_value = (True, None)
        await harness.coordinator.to_card(handoff.conversation, force=True)
        async with ResourceLease("desktop:local", timeout=0):
            pass
    finally:
        await harness.aclose()


async def test_restart_recovers_reserved_launch_before_terminal_pid_was_written(tmp_path, monkeypatch):
    from drivers.terminals import managed_process
    harness, conversation = await setup_harness(tmp_path, monkeypatch)
    try:
        handoff = await harness.coordinator.to_external(conversation)
        directory = harness.launcher.root / handoff.launch.id
        (directory / "pid").unlink()
        launch = handoff.launch.evolve(external_process_ref=None)
        await harness.repositories.terminal_launches.save(launch)
        actual = managed_process.process_identity
        monkeypatch.setattr(managed_process, "process_identity", lambda pid: None
                            if pid == launch.metadata["computerUse"]["owner"]["pid"] else actual(pid))
        signals = await harness.monitor.poll_once()
        assert len(signals) == 1 and signals[0].source == "launch-owner-gone"
        assert (directory / "revoked").exists()
        assert await harness.host.leases.describe(conversation.id) is None
        async with ResourceLease("desktop:local", timeout=0):
            pass
    finally:
        await harness.aclose()


async def test_terminal_handoff_does_not_steal_control_from_completed_remote_parent(tmp_path, monkeypatch):
    harness, conversation = await setup_harness(tmp_path, monkeypatch, remote=True)
    monkeypatch.setattr(integration, "_quarantined", {})
    monkeypatch.setattr(integration, "_QUARANTINE_POLL_SECONDS", .01)
    completed = False

    async def complete(*_):
        return completed

    lease = await ResourceLease("desktop:local").acquire()
    integration.quarantine([lease], complete, None, "owned-background-session", {}, monitor=True)
    try:
        with pytest.raises(SurfaceConflictError, match="电脑工具正在使用"):
            await harness.coordinator.to_external(conversation)
        assert lease.held and harness.opened == []
        assert await harness.host.leases.describe(conversation.id) is None
        completed = True
        async with asyncio.timeout(2):
            while lease.held:
                await asyncio.sleep(.01)
        handoff = await harness.coordinator.to_external(conversation)
        assert handoff.launched
        await harness.coordinator.to_card(handoff.conversation, force=True)
    finally:
        completed = True
        await integration.reconcile_resources()
        await harness.aclose()
