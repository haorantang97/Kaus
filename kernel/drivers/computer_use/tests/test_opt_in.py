"""Explicit opt-in applies at dispatch boundaries, retaining in-flight ownership."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.computer_use import integration, settings
from drivers.computer_use.policy import effective_controls
from drivers.computer_use import native_policy
from drivers.computer_use.tests.test_integration import Effective
from drivers.computer_use.tests.test_runtime_recovery import wait_for
from runtime.tests.test_session_host import make_world


async def test_default_off_has_no_discovery_or_auto_tools_and_keeps_normal_project_features(monkeypatch):
    (settings.storage_root() / 'settings.json').unlink()
    assert settings.is_enabled() is False
    native = AsyncMock(side_effect=AssertionError('must not probe optional native tooling'))
    fallback = AsyncMock(side_effect=AssertionError('must not launch optional desktop services'))
    monkeypatch.setattr(integration, 'native_status', native)
    monkeypatch.setattr(integration, 'ambient_mcp', lambda *_: ())
    monkeypatch.setattr('drivers.computer_use.probe.probe_fallbacks', fallback)
    computer = {'name': 'computer', 'command': 'cua-driver', 'args': ['mcp']}
    database = {'name': 'database', 'command': 'db-mcp'}
    metadata = {'mcpServers': [computer, database], '_meta': {'skills': ['writing']}}
    result = await integration.prepare_projection(None, None, None,
        SimpleNamespace(id='default-off'), Effective([computer, database]), metadata)
    assert result['mcpServers'] == [database]
    assert result['_meta'] == metadata['_meta']
    assert metadata['mcpServers'] == [computer, database]
    assert result['_kausComputerUse']['controls'] == {'browser': False, 'desktop': False}
    assert result['_kausComputerUse']['resources'] == []
    native.assert_not_awaited()
    fallback.assert_not_awaited()


def test_global_opt_in_is_and_not_override_of_project_denial():
    policy = Effective(controls={'desktop': 'off'})
    assert effective_controls(policy) == {'browser': True, 'desktop': False}
    settings.save_enabled(False)
    assert effective_controls(policy) == {'browser': False, 'desktop': False}


async def test_idle_conversation_restarts_same_native_session_when_opt_in_changes(tmp_path, monkeypatch):
    monkeypatch.setattr('drivers.computer_use.probe.probe_fallbacks', AsyncMock(return_value=()))
    monkeypatch.setattr(integration, 'ambient_mcp', lambda *_: ())
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='synthetic')
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key='synthetic')
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    try:
        first = await world.host.ensure_runtime(conversation)
        state = driver._runtimes[first.runtime_id]
        settings.save_enabled(False)
        # Direct send goes through the same refresh path; callers need not know
        # that a feature switch required rebuilding a native tool catalog.
        await world.host.send_message(conversation.id, 'Synthetic ordinary message')
        async with asyncio.timeout(5):
            while not any(e.event.type == 'run.completed' for e in await world.event_store.replay(conversation.id)):
                await asyncio.sleep(.01)
        second = world.host._runtimes[conversation.id].handle
        assert second.runtime_id != first.runtime_id
        assert second.native_session_id == first.native_session_id
        assert state.connection.close_confirmed
        assert driver._runtimes[second.runtime_id].computer_use_enabled is False
        assert driver._runtimes[second.runtime_id].runtime_metadata.get('_kausComputerUse', {}).get('resources', []) == []
    finally:
        await world.host.aclose()


async def test_disabling_does_not_retire_active_turn_or_release_its_resource(tmp_path, monkeypatch):
    from drivers.computer_use.lease import ResourceLease, ResourceBusyError
    monkeypatch.setattr('drivers.computer_use.lease.default_lease_root', lambda: tmp_path / 'locks')
    monkeypatch.setattr('drivers.computer_use.probe.probe_fallbacks', AsyncMock(return_value=()))
    monkeypatch.setattr(integration, 'ambient_mcp', lambda *_: ())
    driver = AcpDriver(fake_agent_spec('interrupt', cwd=str(tmp_path)), backend_key='synthetic')
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key='synthetic')
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    try:
        handle = await world.host.ensure_runtime(conversation)
        state = driver._runtimes[handle.runtime_id]
        state.runtime_metadata['_kausComputerUse'] = {'resources': ['desktop:local']}
        await world.host.send_message(conversation.id, 'Synthetic running turn')
        async with asyncio.timeout(5):
            while not any(e.event.type == 'message.delta' for e in await world.event_store.replay(conversation.id)):
                await asyncio.sleep(.01)
        settings.save_enabled(False)
        assert (await world.host.ensure_runtime(conversation)).runtime_id == handle.runtime_id
        assert not state.closed
        with pytest.raises(ResourceBusyError):
            async with ResourceLease('desktop:local', timeout=0):
                pass
        await world.host.interrupt(conversation.id)
        await wait_for(lambda: not world.host.is_active(conversation.id))
        async with ResourceLease('desktop:local', timeout=0):
            pass
    finally:
        await world.host.aclose()


async def test_known_native_mcp_disable_preserves_source_configuration(tmp_path, monkeypatch):
    native = tmp_path / 'codex-home'
    native.mkdir()
    config = native / 'config.toml'
    original = '[mcp_servers.screen]\ncommand="cua-driver"\nargs=["mcp"]\n'
    config.write_text(original)
    from drivers.acp.client import AcpAgentSpec
    spec = AcpAgentSpec(command=('synthetic',), env={'CODEX_HOME': str(native), 'HOME': str(tmp_path)})
    settings.save_enabled(False)
    metadata = await integration.prepare_projection(spec, SimpleNamespace(__name__='drivers.skill_execution.codex'),
        None, SimpleNamespace(id='native'), EffectiveCapabilities(project_id='project:synthetic'), {})
    assert metadata['_kausComputerUse']['nativeDisabledServers'] == ['screen']
    assert metadata['_kausComputerUse']['resources'] == []
    assert config.read_text() == original


def test_native_claude_policy_preserves_skills_and_existing_denials():
    adapter = SimpleNamespace(__name__='drivers.skill_execution.claude')
    metadata = {'_kausComputerUse': {'nativeDisabledServers': ['screen']}, '_meta': {'claudeCode': {
        'options': {'skills': ['writing'], 'disallowedTools': ['Bash']}}}}
    spec = object()
    assert native_policy.prepare_spec(spec, adapter, metadata) is spec
    assert metadata['_meta']['claudeCode']['options'] == {
        'skills': ['writing'], 'disallowedTools': ['Bash', 'mcp__screen__*']}
    command = native_policy.external_command(('claude', '--resume', 'session'), adapter, metadata)
    assert command == ('claude', '--resume', 'session', '--disallowedTools', 'mcp__screen__*')


async def test_native_codex_wrapper_preserves_first_arg_and_scope_then_can_restore(tmp_path):
    from drivers.acp.client import AcpAgentSpec
    executable = tmp_path / 'codex'
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o700)
    spec = AcpAgentSpec(command=('synthetic',), env={'CODEX_PATH': str(executable), 'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'projection')})
    adapter = SimpleNamespace(__name__='drivers.skill_execution.codex')
    metadata = {'_kausComputerUse': {'nativeDisabledServers': ['my screen']}}
    prepared = native_policy.prepare_spec(spec, adapter, metadata)
    process = await asyncio.create_subprocess_exec(prepared.env['CODEX_PATH'], 'app-server', '--stdio',
        stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    assert output.decode().splitlines() == ['app-server', '--stdio', '-c', 'mcp_servers."my screen".enabled=false']
    restored = native_policy.prepare_spec(prepared, adapter, {})
    assert restored.env['CODEX_PATH'] == str(executable)
    assert 'KAUS_CU_CODEX_PATH' not in restored.env


def test_runtime_revision_ignores_progress_polling_but_tracks_catalog_and_readiness(tmp_path, monkeypatch):
    first = settings.runtime_revision()
    settings.write_json('setup.json', {'status': 'downloading', 'updatedAt': 1})
    assert settings.runtime_revision() == first
    settings.write_json('setup.json', {'status': 'verifying', 'updatedAt': 2})
    assert settings.runtime_revision() == first
    executable = tmp_path / 'cua-driver'
    executable.write_text('synthetic')
    monkeypatch.setattr(settings, 'managed_executable', lambda: executable)
    installed = settings.runtime_revision()
    assert installed != first
    settings.write_json('setup.json', {'status': 'needs_permission'})
    assert settings.runtime_revision() == installed
    settings.write_json('setup.json', {'status': 'ready'})
    ready = settings.runtime_revision()
    assert ready != installed
    settings.record_runtime_readiness([{'id': 'native', 'browser': {'status': 'ready'}}], [])
    catalog = settings.runtime_revision()
    assert catalog != ready
    settings.record_runtime_readiness([{'id': 'native', 'browser': {'status': 'ready'}}], [])
    assert settings.runtime_revision() == catalog
    settings.save_enabled(False)
    settings.write_json('setup.json', {'status': 'failed'})
    assert settings.runtime_revision() == 'off'


async def test_install_and_permission_refresh_rebuild_idle_same_native_session(tmp_path, monkeypatch):
    from drivers.computer_use.policy import Candidate
    executable = tmp_path / 'cua-driver'
    def catalog(**kwargs):
        if settings.read_json('setup.json').get('status') == 'ready':
            return (Candidate('cua-driver', 'desktop', 'fallback', 'ready', verified=True,
                mcp_server={'name': 'synthetic-cua', 'command': 'synthetic-cua'}, resource_id='desktop:local'),)
        return ()
    monkeypatch.setattr('drivers.computer_use.probe.probe_fallbacks', AsyncMock(side_effect=catalog))
    monkeypatch.setattr(integration, 'ambient_mcp', lambda *_: ())
    monkeypatch.setattr(settings, 'managed_executable', lambda: executable if executable.exists() else None)
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='synthetic')
    world = make_world()
    _, conversation = await world.add_backend(driver=driver, backend_key='synthetic')
    binding = await world.bindings.get(conversation.agent_binding_id)
    driver.register_binding(binding)
    try:
        first = await world.host.ensure_runtime(conversation)
        first_state = driver._runtimes[first.runtime_id]
        assert first_state.runtime_metadata.get('_kausComputerUse', {}).get('resources', []) == []
        executable.write_text('synthetic installation')
        settings.write_json('setup.json', {'status': 'needs_permission'})
        second = await world.host.ensure_runtime(await world.conversations.get(conversation.id))
        assert second.runtime_id != first.runtime_id
        assert second.native_session_id == first.native_session_id
        assert first_state.connection.close_confirmed
        second_state = driver._runtimes[second.runtime_id]
        settings.write_json('setup.json', {'status': 'ready'})
        third = await world.host.ensure_runtime(await world.conversations.get(conversation.id))
        assert third.runtime_id != second.runtime_id
        assert third.native_session_id == first.native_session_id
        assert second_state.connection.close_confirmed
        assert driver._runtimes[third.runtime_id].runtime_metadata['_kausComputerUse']['resources'] == ['desktop:local']
        assert (await world.host.ensure_runtime(await world.conversations.get(conversation.id))).runtime_id == third.runtime_id
    finally:
        await world.host.aclose()
