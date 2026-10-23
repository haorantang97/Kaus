"""Version changes use fake probes and temporary installations, never real Agents."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'kernel'))
import managed_runtimes as managed
from session_bootstrap import BackendSpec, MaintenanceLoop, build_driver
from app.persistence.memory import in_memory_repository_set
from app.projects.models import Backend
from app.runtimes.models import RuntimeLease
from drivers.acp.driver import AcpDriver
from drivers.base import BackendProbeResult
from drivers.registry import BackendDriverRegistry


def installed(root, version):
    executable = root / version / 'bin' / 'opencode'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    return {'command': [f'{version}/bin/opencode', 'acp'], 'path': [f'{version}/bin'], 'version': version}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / 'runtimes'
    root.mkdir()
    monkeypatch.setattr(managed, 'RUNTIME_ROOT', root)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('KAUS_SKILL_STATE_ROOT', str(tmp_path / 'skills'))
    native = AsyncMock(return_value={'engineVersion': '3.2.1', 'nativePath': '/fixture/engine', 'nativeVersionState': 'observed'})
    monkeypatch.setattr(managed, 'read_native_version', native)
    async def probe(self):
        self._protocol_version = 1
        return BackendProbeResult(backend_id=self.backend_id, driver_kind='acp', state='ready', installed=True, version='2.0.0')
    monkeypatch.setattr(AcpDriver, 'probe', probe)
    return root, native


def loop_for(driver, tmp_path):
    host = SimpleNamespace(_start_locks={}, active_conversation_ids=lambda: (), handle_for=lambda _: None)
    return MaintenanceLoop(session_host=host, registry=BackendDriverRegistry([driver]),
        repositories=in_memory_repository_set(), metadata_root=tmp_path / 'state', log=lambda _: None)


def test_installed_version_activation_rollback_and_conflict_are_atomic(isolated):
    root, _ = isolated
    old, new = installed(root, '1.0.0'), installed(root, '2.0.0')
    first = managed.activate_runtime('opencode', old)
    second = managed.activate_runtime('opencode', new, expected_revision=first['revision'])
    raw = (root / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='selection changed'):
        managed.activate_runtime('opencode', old, expected_revision=first['revision'])
    assert (root / 'manifest.json').read_bytes() == raw
    assert managed.rollback_runtime('opencode', expected_revision=second['revision']) == first
    assert managed.managed_launch('opencode')[0][0].endswith('/1.0.0/bin/opencode')
    assert (root / '2.0.0/bin/opencode').exists()
    with pytest.raises(ValueError, match='No previous'):
        managed.rollback_runtime('opencode')


def test_invalid_or_missing_version_never_replaces_selection(isolated):
    root, _ = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    raw = (root / 'manifest.json').read_bytes()
    for entry in ({'command':['../escape'], 'version':'2.0.0'},
                  {'command':['missing'], 'version':'2.0.0'},
                  {'command':['1.0.0/bin/opencode'], 'version':'latest'}):
        with pytest.raises(ValueError):
            managed.activate_runtime('opencode', entry)
        assert (root / 'manifest.json').read_bytes() == raw


def test_idle_refresh_rebuilds_from_manifest_and_preserves_session_ledger(isolated, tmp_path):
    root, native = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    driver.aclose = AsyncMock()
    driver.session_ledger = {'binding:one': {'native-id': object()}}
    driver._catalog['binding:one'] = object()
    loop = loop_for(driver, tmp_path)
    asyncio.run(loop.refresh_backends())
    managed.activate_runtime('opencode', installed(root, '2.0.0'))
    asyncio.run(loop.refresh_backends(['backend:test']))
    updated = loop.registry.get('backend:test')
    assert updated is not driver
    driver.aclose.assert_awaited_once()
    assert updated.agent_spec.command[0].endswith('/2.0.0/bin/opencode')
    assert updated.session_ledger == driver.session_ledger
    assert not updated._catalog
    assert updated.runtime_identity['engineVersion'] == '3.2.1'
    assert updated.runtime_identity['adapterVersion'] == '2.0.0'
    assert updated.runtime_identity['protocolVersion'] == 1
    assert updated.runtime_identity['managedVersion'] == '2.0.0'
    assert native.await_count == 2
    asyncio.run(loop.refresh_backends())
    assert native.await_count == 2, 'unchanged versions must not spawn --version on each tick'
    observation = json.loads(next((tmp_path/'state/runtime-observations').glob('*.json')).read_text())
    assert observation['backendId'] == 'backend:test'
    assert 'command' not in observation and 'env' not in observation
    assert observation['launchFingerprint'] == updated.assembly_fingerprint


@pytest.mark.parametrize('owner', ['card', 'external', 'starting'])
def test_busy_owner_keeps_old_driver_until_idle(isolated, tmp_path, owner):
    root, _ = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    loop = loop_for(driver, tmp_path)
    if owner == 'card':
        loop.session_host.active_conversation_ids = lambda: ('conversation:00000000-0000-0000-0000-000000000001',)
        loop.session_host.handle_for = lambda _: SimpleNamespace(backend_id='backend:test')
    elif owner == 'starting':
        loop.session_host._start_locks = {'conversation:00000000-0000-0000-0000-000000000001': SimpleNamespace(locked=lambda: True)}
    else:
        asyncio.run(loop.repositories.leases.acquire(RuntimeLease(conversation_id='conversation:00000000-0000-0000-0000-000000000001',
            owner_type='external-cli', owner_id='terminal', metadata={'backendId':'backend:test'})))
    managed.activate_runtime('opencode', installed(root, '2.0.0'))
    asyncio.run(loop.refresh_backends())
    assert loop.registry.get('backend:test') is driver
    assert driver.runtime_identity['updateState'] == 'pending_idle'
    assert driver.agent_spec.command[0].endswith('/1.0.0/bin/opencode')
    loop.session_host.active_conversation_ids = lambda: ()
    loop.session_host._start_locks = {}
    if owner == 'external':
        asyncio.run(loop.repositories.leases.release('conversation:00000000-0000-0000-0000-000000000001'))
    asyncio.run(loop.refresh_backends())
    assert loop.registry.get('backend:test') is not driver
    assert loop.registry.get('backend:test').runtime_identity['updateState'] == 'current'


def test_explicit_command_ignores_unrelated_managed_update(isolated, tmp_path):
    root, _ = isolated
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode', command=('/custom/entry', 'acp')))
    loop = loop_for(driver, tmp_path)
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    asyncio.run(loop.refresh_backends())
    assert loop.registry.get('backend:test') is driver


def test_changed_native_path_and_auth_configuration_invalidate_fingerprint(isolated, tmp_path, monkeypatch):
    root, _ = isolated
    first = installed(root, '1.0.0')
    second = installed(root, '2.0.0')
    path1, path2 = root / first['command'][0], root / second['command'][0]
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='codex',
        env_keys=('FIXTURE_AUTH',), options={'native_cli_path':str(path1)}))
    fingerprint = driver.assembly_fingerprint
    monkeypatch.setenv('FIXTURE_AUTH', 'SECRET_fixture_credential')
    assert managed.launch_fingerprint(driver) != fingerprint
    loop = loop_for(driver, tmp_path)
    driver.assembly_spec = replace(driver.assembly_spec, options={'native_cli_path':str(path2)})
    asyncio.run(loop.refresh_backends())
    updated = loop.registry.get('backend:test')
    assert updated.agent_spec.env['CODEX_PATH'] == str(path2)
    assert updated.runtime_identity['nativeVersionScope'] == 'launch_override'
    observations = ''.join(p.read_text() for p in (tmp_path/'state/runtime-observations').glob('*'))
    assert 'SECRET_fixture_credential' not in observations


def test_broken_manifest_records_failure_without_fallback_or_cache_replacement(isolated, tmp_path):
    root, native = isolated
    item = installed(root, '1.0.0')
    managed.activate_runtime('opencode', item)
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    loop = loop_for(driver, tmp_path)
    (root/item['command'][0]).unlink()
    assert asyncio.run(loop.refresh_backends()) == {'backend:test':'unavailable'}
    assert loop.registry.get('backend:test') is driver
    assert driver.runtime_identity['updateState'] == 'invalid'
    assert native.await_count == 0


def test_concurrent_refresh_rebuilds_only_once(isolated, tmp_path):
    root, native = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    loop = loop_for(driver, tmp_path)
    managed.activate_runtime('opencode', installed(root, '2.0.0'))
    changes = []
    loop.log = changes.append
    async def refresh():
        return await asyncio.gather(loop.refresh_backends(), loop.refresh_backends(['backend:test']))
    assert asyncio.run(refresh()) == [{'backend:test':'available'}, {'backend:test':'available'}]
    assert len(changes) == 1
    assert native.await_count == 2, 'manual refresh explicitly rechecks the native version'


def test_native_version_reader_never_persists_arbitrary_process_output(tmp_path, monkeypatch):
    path = tmp_path / 'native'
    path.write_text('fixture')
    class Process:
        returncode = 0
        stdout = SimpleNamespace(read=AsyncMock(return_value=b'Agent 4.5.6\nSECRET_value\n'))
        wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=Process())
    monkeypatch.setattr(managed, 'native_path', lambda _: str(path))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    driver = SimpleNamespace(agent_spec=SimpleNamespace(build_env=lambda: {'FIXTURE':'yes'}))
    result = asyncio.run(managed.read_native_version(driver))
    assert result['engineVersion'] == '4.5.6'
    assert 'SECRET' not in json.dumps(result)
    assert spawn.call_args.args == (str(path), '--version')


def test_native_file_and_config_changes_invalidate_existing_launch(isolated, tmp_path):
    root, _ = isolated
    entry = installed(root, '1.0.0')
    path = root / entry['command'][0]
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='codex',
        options={'native_cli_path':str(path)}))
    original = managed.launch_fingerprint(driver)
    path.write_text('#!/bin/sh\n# updated in place\nexit 0\n')
    changed = managed.launch_fingerprint(driver)
    assert changed != original
    config = tmp_path/'home/.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('model = "test"\n')
    assert managed.launch_fingerprint(driver) != changed


def test_protocol_version_is_retained_from_handshake(isolated):
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', command=('fixture',)))
    driver._absorb_initialize({'protocolVersion': 1, 'agentInfo': {'name':'fixture', 'version':'9.0.1'}, 'agentCapabilities': {}})
    assert driver._protocol_version == 1
    assert driver._backend_version == '9.0.1'


def test_native_version_timeout_cleans_only_its_probe_process(tmp_path, monkeypatch):
    path = tmp_path/'native'
    path.write_text('fixture')
    class Process:
        returncode = None
        killed = False
        async def read(self, _):
            await asyncio.sleep(10)
        async def wait(self):
            self.returncode = -9
        def kill(self):
            self.killed = True
    process = Process()
    process.stdout = process
    monkeypatch.setattr(managed, 'native_path', lambda _: str(path))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    driver = SimpleNamespace(agent_spec=SimpleNamespace(build_env=lambda: {}))
    result = asyncio.run(managed.read_native_version(driver, timeout=0.01))
    assert result['nativeVersionState'] == 'unavailable'
    assert process.killed and process.returncode == -9


def test_removing_managed_record_does_not_fall_back_to_path_cli(isolated, tmp_path):
    root, native = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    loop = loop_for(driver, tmp_path)
    (root/'manifest.json').unlink()
    assert asyncio.run(loop.refresh_backends()) == {'backend:test':'unavailable'}
    assert loop.registry.get('backend:test') is driver
    assert driver.runtime_identity['updateState'] == 'invalid'
    assert native.await_count == 0
    assert driver.agent_spec.command[0].endswith('/1.0.0/bin/opencode')
    # First-time native installations never had a managed source to preserve.
    assert build_driver(BackendSpec(backend_id='backend:fresh', driver='acp', preset='opencode')).agent_spec.command == ('opencode','acp')


def test_manual_refresh_rechecks_native_wrapper_version_and_invalidates_catalog(isolated, tmp_path):
    root, native = isolated
    managed.activate_runtime('opencode', installed(root, '1.0.0'))
    driver = build_driver(BackendSpec(backend_id='backend:test', driver='acp', preset='opencode'))
    loop = loop_for(driver, tmp_path)
    asyncio.run(loop.refresh_backends())
    native.return_value = {'engineVersion':'3.2.2', 'nativeVersionState':'observed'}
    driver._catalog['fixture'] = object()
    asyncio.run(loop.refresh_backends())
    assert native.await_count == 1
    assert driver.runtime_identity['engineVersion'] == '3.2.1'
    asyncio.run(loop.refresh_backends(['backend:test']))
    assert native.await_count == 2
    assert driver.runtime_identity['engineVersion'] == '3.2.2'
    assert driver._catalog == {}


def test_runtime_shutdown_waits_for_driver_discovery_cleanup():
    from session_bootstrap import SessionRuntime
    calls = []
    async def close_host():
        calls.append('sessions')
    async def close_driver():
        calls.append('inventory')
    driver = SimpleNamespace(aclose=close_driver)
    runtime = SessionRuntime(router=None, session_host=SimpleNamespace(aclose=close_host),
        registry=SimpleNamespace(list_drivers=lambda: (driver,)), repositories=None)
    asyncio.run(runtime.aclose())
    assert calls == ['sessions', 'inventory']
