"""Real router + isolated DB; registrations never spawn an agent."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'kernel')]
from engine_connections import merge_connections, read_connections
from session_bootstrap import attach_session_api
from drivers.acp.driver import AcpDriver

_start_skill_inventory_refresh = AcpDriver.start_skill_inventory_refresh


@pytest.fixture(autouse=True)
def isolate_native_inventory(monkeypatch):
    # These router tests must not read/run the test machine's installed parsers.
    monkeypatch.setattr(AcpDriver, "start_skill_inventory_refresh", lambda _: None)


def application(tmp_path, config=None):
    app = FastAPI()
    config = config or {'features': {'session_host_v1': True, 'session_host_background': False}}
    runtime = attach_session_api(app, config_loader=lambda: config,
        db_path=tmp_path/'test.db', token_path=tmp_path/'token', log=lambda _: None)
    client = TestClient(app)
    token = client.get('/api/session-auth/bootstrap').json()['token']
    return client, runtime, {'Authorization': f'Bearer {token}'}


def test_catalog_auth_required_and_never_infers_login_from_installed(tmp_path):
    client, runtime, auth = application(tmp_path)
    try:
        assert client.get('/api/engine-catalog').status_code == 401
        r = client.get('/api/engine-catalog', headers=auth)
        assert r.status_code == 200
        rows = r.json()['engines']
        assert any(row['id'] == 'openclaw' for row in rows)
        assert all(row['registered'] is False and row['probeState'] == 'unknown' for row in rows)
        assert not (tmp_path/'engine-connections.json').exists()
    finally:
        asyncio.run(runtime.aclose())


def test_connect_is_idempotent_persists_and_keeps_custom_config(tmp_path):
    client, runtime, auth = application(tmp_path)
    try:
        for _ in range(2):
            r = client.post('/api/engine-connections', headers=auth, json={'presetId':'openclaw'})
            assert r.status_code == 200, r.text
            assert r.json()['backendId'] == 'backend:openclaw'
        assert read_connections(tmp_path/'engine-connections.json') == ('openclaw',)
        assert runtime.registry.get('openclaw').preset.id == 'openclaw'
        assert asyncio.run(runtime.repositories.backends.get('backend:openclaw')).display_name == 'OpenClaw'
        custom = {'backends':[{'id':'backend:openclaw','driver':'acp','preset':'openclaw','command':['custom-binary']}], 'extra': 7}
        merged = merge_connections(custom, tmp_path/'engine-connections.json')
        assert merged == custom
    finally:
        asyncio.run(runtime.aclose())
    client2, runtime2, _ = application(tmp_path)
    try:
        assert runtime2.registry.get('openclaw').preset.id == 'openclaw'
    finally:
        asyncio.run(runtime2.aclose())


def test_first_connect_enrolls_skill_flow_and_skip_persists(tmp_path, monkeypatch):
    from app.projects.models import Project
    from drivers.skill_inventory import NativeSkillRoot
    skills = tmp_path / "synthetic-skills"
    path = skills / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: Review\n---\nSynthetic test\n")
    monkeypatch.setattr("drivers.skill_inventory.configured_roots", lambda *_: [
        NativeSkillRoot(skills, "openclaw", "OpenClaw", "project:default")])
    client, runtime, auth = application(tmp_path)
    try:
        asyncio.run(runtime.repositories.projects.save(Project.create(slug="default", display_name="X")))
        assert client.get("/api/skill-tree/onboarding").status_code == 401
        assert client.get("/api/skill-tree/onboarding", headers=auth).json()["items"] == []
        assert client.post("/api/engine-connections", headers=auth, json={"presetId": "openclaw"}).status_code == 200
        invitation = client.get("/api/skill-tree/onboarding", headers=auth).json()["items"][0]
        assert invitation["agentKey"] == "openclaw"
        assert invitation["skillCount"] == 1
        assert client.post("/api/skill-tree/onboarding", headers=auth,
            json={"id": invitation["id"], "outcome": "skipped", "agentKey": "other"}).status_code == 422
        assert client.post("/api/skill-tree/onboarding", headers=auth,
            json={"id": invitation["id"], "outcome": "skipped"}).status_code == 200
        client.post("/api/engine-connections", headers=auth, json={"presetId": "openclaw"})
        assert client.get("/api/skill-tree/onboarding", headers=auth).json()["items"] == []
    finally:
        asyncio.run(runtime.aclose())
    client, runtime, auth = application(tmp_path)
    try:
        assert client.get("/api/skill-tree/onboarding", headers=auth).json() == {"items": [], "waiting": False}
    finally:
        asyncio.run(runtime.aclose())


def test_second_transport_for_existing_product_does_not_trigger_onboarding(tmp_path):
    from app.projects.models import Backend
    from drivers.mock.driver import MockDriver
    client, runtime, auth = application(tmp_path)
    try:
        native = MockDriver(backend_key="existing-http")
        native.driver_kind = "native"
        native.product_id = "hermes"
        runtime.registry.register(native)
        asyncio.run(runtime.repositories.backends.save(Backend.create(key="existing-http", driver_kind="native")))
        assert client.post("/api/engine-connections", headers=auth, json={"presetId": "hermes-acp"}).status_code == 200
        assert client.get("/api/skill-tree/onboarding", headers=auth).json() == {"items": [], "waiting": False}
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize('body', [{'presetId':'unknown'}, {'presetId':'openclaw','command':['sh']}, {'presetId':[]}, {}])
def test_connect_rejects_untrusted_launch_fields(tmp_path, body):
    client, runtime, auth = application(tmp_path)
    try:
        assert client.post('/api/engine-connections', headers=auth, json=body).status_code == 400
        assert not (tmp_path/'engine-connections.json').exists()
        assert client.post('/api/engine-connections', headers={**auth, 'Origin':'https://untrusted.invalid'}, json={'presetId':'openclaw'}).status_code == 403
    finally:
        asyncio.run(runtime.aclose())


def test_malformed_persisted_data_does_not_silently_spawn_commands(tmp_path):
    path = tmp_path/'engine-connections.json'
    path.write_text(json.dumps({'version':1,'presets':['sh']}))
    with pytest.raises(ValueError):
        read_connections(path)


@pytest.mark.parametrize("preset_id", ["alma", "devin"])
def test_retired_presets_cannot_be_new_connections_or_trigger_onboarding(tmp_path, preset_id):
    client, runtime, auth = application(tmp_path)
    try:
        rows = client.get('/api/engine-catalog', headers=auth).json()['engines']
        assert preset_id not in {row['id'] for row in rows}
        response = client.post('/api/engine-connections', headers=auth, json={'presetId': preset_id})
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'engine_not_selectable'
        assert not (tmp_path/'engine-connections.json').exists()
        assert client.get('/api/skill-tree/onboarding', headers=auth).json() == {'items': [], 'waiting': False}
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize("preset_id", ["alma", "devin"])
def test_retired_connections_and_history_survive_but_cannot_create_new_targets(tmp_path, preset_id, monkeypatch):
    from app.conversations.models import Conversation
    from app.api.router import build_domain_router
    from app.projects.models import AgentBinding, Backend, Project
    from drivers.acp.presets import get_preset

    path = tmp_path/'engine-connections.json'
    original = json.dumps({'version':1, 'presets':[preset_id]})
    path.write_text(original)
    client, runtime, auth = application(tmp_path)
    try:
        client.app.include_router(build_domain_router(runtime.repositories))
        driver = runtime.registry.get(preset_id)
        assert driver.preset is get_preset(preset_id)
        async def forbidden(*args, **kwargs):
            raise AssertionError('catalog/history must not start an agent')
        monkeypatch.setattr(driver, '_connect', forbidden)
        project = Project.create(slug='work', display_name='Work')
        binding = AgentBinding.create(project=project, backend=preset_id)
        conversation = Conversation.create(project_id=project.id, agent_binding_id=binding.id, title='Saved work')
        asyncio.run(runtime.repositories.backends.save(Backend.create(key=preset_id, display_name=driver.preset.label, driver_kind="acp")))
        asyncio.run(runtime.repositories.projects.save(project))
        asyncio.run(runtime.repositories.bindings.save(binding))
        asyncio.run(runtime.repositories.conversations.save(conversation))
        before = conversation.model_dump_json()
        connected = client.get('/api/engine-connections', headers=auth).json()['backends']
        row = next(row for row in connected if row['id'] == driver.backend_id)
        assert row['preset']['catalogStatus'] == 'legacy'
        assert not any(row['backendId'] == driver.backend_id for row in client.get('/api/engine-catalog', headers=auth).json()['engines'])
        details = client.get('/api/engine-catalog', params={'backendId': driver.backend_id}, headers=auth).json()['engines']
        assert next(row for row in details if row['backendId'] == driver.backend_id)['catalogStatus'] == 'legacy'
        # Idempotent management of an existing connection remains available.
        assert client.post('/api/engine-connections', headers=auth, json={'presetId':preset_id}).status_code == 200
        bindings = client.get(f'/api/projects/{project.id}/bindings', params={'includeStatus':False}, headers=auth).json()['bindings']
        assert bindings[0]['catalogStatus'] == 'legacy'
        for url, body in [
            (f'/api/projects/{project.id}/bindings', {'backendId':driver.backend_id, 'discriminator':'new'}),
            (f'/api/projects/{project.id}/bindings/ensure', {'backendId':driver.backend_id}),
            (f'/api/bindings/{binding.id}/make-default', {}),
            (f'/api/projects/{project.id}/conversations', {'bindingId':binding.id}),
        ]:
            response = client.post(url, headers=auth, json=body)
            assert response.status_code == 409, (url, response.text)
            assert response.json()['error']['code'] == 'engine_not_selectable'
        detail = client.get(f'/api/conversations/{conversation.id}', headers=auth)
        assert detail.status_code == 200, detail.text
        assert detail.json()['conversation']['title'] == 'Saved work'
        listed = client.get(f'/api/projects/{project.id}/conversations', headers=auth)
        assert listed.status_code == 200 and conversation.id in listed.text
        assert asyncio.run(runtime.repositories.conversations.get(conversation.id)).model_dump_json() == before
        assert path.read_text() == original
    finally:
        asyncio.run(runtime.aclose())


def test_new_connection_starts_delayed_native_inventory_without_probe_or_send(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    import time
    from app.projects.models import Project
    from drivers.skill_inventory import NativeSkillRoot

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    skills = tmp_path / "dynamic-skills"
    calls = []

    async def refresh_inventory(spec):
        calls.append(spec)
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        path = skills / "review" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: Native review\n---\nSynthetic built-in skill\n")
        finished.set()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("onboarding must not initialize or prompt an Agent")

    monkeypatch.setattr(AcpDriver, "start_skill_inventory_refresh", _start_skill_inventory_refresh)
    monkeypatch.setattr(AcpDriver, "_skill_adapter", lambda _: SimpleNamespace(refresh_inventory=refresh_inventory))
    monkeypatch.setattr(AcpDriver, "probe", forbidden)
    monkeypatch.setattr(AcpDriver, "_connect", forbidden)
    monkeypatch.setattr(AcpDriver, "send_message", forbidden)
    monkeypatch.setattr("drivers.skill_inventory.configured_roots", lambda *_: [
        NativeSkillRoot(skills, "openclaw", "OpenClaw", "project:default")])
    client, runtime, auth = application(tmp_path)
    try:
        asyncio.run(runtime.repositories.projects.save(Project.create(slug="default", display_name="X")))
        with client:
            response = client.post("/api/engine-connections", headers=auth, json={"presetId": "openclaw"})
            assert response.status_code == 200
            assert entered.wait(1)
            assert client.get("/api/skill-tree/onboarding", headers=auth).json() == {"items": [], "waiting": True}
            client.post("/api/engine-connections", headers=auth, json={"presetId": "openclaw"})
            assert len(calls) == 1
            release.set()
            assert finished.wait(1)
            for _ in range(50):
                response = client.get("/api/skill-tree/onboarding", headers=auth).json()
                if response["items"]:
                    break
                time.sleep(0.01)
            assert response["items"][0]["skillCount"] == 1
            assert response["items"][0]["rootProjectId"] == "project:default"
    finally:
        release.set()
        asyncio.run(runtime.aclose())


def test_global_connections_include_registered_custom_drivers_without_starting_them(tmp_path):
    from app.projects.models import Backend
    from drivers.mock.driver import MockDriver

    client, runtime, auth = application(tmp_path)
    try:
        assert client.get('/api/engine-connections').status_code == 401
        assert client.get('/api/engine-connections', headers=auth).json()['backends'] == []
        native = MockDriver(backend_key='custom-native')
        native.driver_kind = 'native'
        runtime.registry.register(native)
        asyncio.run(runtime.repositories.backends.save(Backend.create(key='custom-native', driver_kind='native')))
        asyncio.run(runtime.repositories.backends.save(Backend.create(key='orphan', driver_kind='native')))
        rows = client.get('/api/engine-connections', headers=auth).json()['backends']
        assert [row['id'] for row in rows] == ['backend:custom-native']
        assert native._session_counter == 0
        assert not (tmp_path/'engine-connections.json').exists()
        assert not asyncio.run(runtime.repositories.bindings.list_for_backend('project:default', native.backend_id))
    finally:
        asyncio.run(runtime.aclose())


def test_product_catalog_unifies_native_and_acp_without_changing_connections(tmp_path, monkeypatch):
    from app.projects.models import Backend
    from drivers.mock.driver import MockDriver
    from drivers.acp.presets import PRESETS

    monkeypatch.setattr('engine_connections.shutil.which', lambda _: None)
    monkeypatch.setattr('managed_runtimes.managed_launch', lambda _: None)

    client, runtime, auth = application(tmp_path)
    native = MockDriver(backend_key='custom-hermes-http')
    native.driver_kind = 'native'
    native.product_id = 'hermes'
    runtime.registry.register(native)
    record = Backend.create(key='custom-hermes-http', display_name='Hermes HTTP', driver_kind='native').evolve(probe_state='available', installed=True)
    asyncio.run(runtime.repositories.backends.save(record))
    try:
        rows = client.get('/api/engine-catalog', headers=auth).json()['engines']
        product = [r for r in rows if r['productId'] == 'hermes']
        assert len(product) == 2
        assert {r['productName'] for r in product} == {'Hermes'}
        assert [r['backendId'] for r in product if r['preferred']] == [native.backend_id]
        assert {r['id'] for r in rows if r['productId'] == 'deepseek-harness'} == {'dsh', 'deepseek-acp'}
        assert [r['id'] for r in rows if r['productId'] == 'deepseek-harness' and r['preferred']] == ['deepseek-acp']
        connected = client.get('/api/engine-connections', headers=auth).json()['backends']
        assert connected[0]['productName'] == 'Hermes' and connected[0]['id'] == native.backend_id
        assert native._session_counter == 0
        assert not (tmp_path/'engine-connections.json').exists()
        assert asyncio.run(runtime.repositories.backends.get(native.backend_id)) == record

        # A known available adapter wins over an unavailable old native route.
        asyncio.run(runtime.repositories.backends.save(record.evolve(probe_state='unavailable')))
        acp = MockDriver(backend_key='my-hermes-acp')
        acp.driver_kind = 'acp'
        acp.preset = PRESETS['hermes-acp']
        runtime.registry.register(acp)
        asyncio.run(runtime.repositories.backends.save(Backend.create(key='my-hermes-acp', driver_kind='acp').evolve(probe_state='available')))
        rows = client.get('/api/engine-catalog', headers=auth).json()['engines']
        assert [r['backendId'] for r in rows if r['productId'] == 'hermes' and r['preferred']] == [acp.backend_id]
        assert acp._session_counter == 0
    finally:
        asyncio.run(runtime.aclose())


def executable(tmp_path, name='fake cli'):
    path = tmp_path / name
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize('preset_id,env_key', [('codex', 'CODEX_PATH'), ('claude-code', 'CLAUDE_CODE_EXECUTABLE'), ('openclaw', None), ('crush', None)])
def test_selected_cli_path_drives_launch_and_survives_restart(tmp_path, preset_id, env_key):
    from engine_connections import read_connection_settings
    native = executable(tmp_path)
    client, runtime, auth = application(tmp_path)
    try:
        result = client.post('/api/engine-connections', headers=auth, json={'presetId': preset_id, 'nativePath': native})
        assert result.status_code == 200, result.text
        driver = runtime.registry.get(preset_id)
        if env_key:
            assert driver.agent_spec.build_env()[env_key] == native
        elif preset_id == 'crush':
            assert driver.agent_spec.command[-2:] == ('--crush-command', native)
        else:
            assert driver.agent_spec.command == (native, 'acp')
        assert read_connection_settings(tmp_path/'engine-connections.json')[1] == {preset_id: native}
        row = next(x for x in client.get('/api/engine-catalog', headers=auth).json()['engines'] if x['id'] == preset_id)
        assert row['nativePath'] == native and row['detected'] is True
        assert row['probeState'] == 'unknown'
    finally:
        asyncio.run(runtime.aclose())
    _, runtime, _ = application(tmp_path)
    try:
        assert runtime.registry.get(preset_id).assembly_spec.options['native_cli_path'] == native
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize('path', ['relative/path', '/missing/executable', '$(touch /tmp/kaus-invalid)', 123])
def test_custom_path_rejects_non_executable_input(tmp_path, path):
    client, runtime, auth = application(tmp_path)
    try:
        result = client.post('/api/engine-connections', headers=auth, json={'presetId':'openclaw', 'nativePath':path})
        assert result.status_code == 400
        assert not (tmp_path/'engine-connections.json').exists()
    finally:
        asyncio.run(runtime.aclose())


def test_login_launches_exact_native_argv_and_refresh_uses_shared_probe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine_connections import build_engine_connection_router
    from app.api.session_auth import SessionAuthPolicy
    from drivers.registry import BackendDriverRegistry
    from app.projects.models import Backend
    from session_bootstrap import build_driver, BackendSpec
    native = executable(tmp_path, "cli with ' quote")
    driver = build_driver(BackendSpec(backend_id='backend:codex', driver='acp', preset='codex', options={'native_cli_path':native}))
    driver.agent_spec = driver.agent_spec.with_env(CODEX_HOME=str(tmp_path/'native-config'), PATH=f'{tmp_path}/support/bin:/usr/bin')
    registry = BackendDriverRegistry(); registry.register(driver)
    backend = Backend.create(key='codex', driver_kind='acp')
    repositories = SimpleNamespace(backends=SimpleNamespace(get=AsyncMock(return_value=backend)))
    launcher = SimpleNamespace(launch=AsyncMock(return_value=SimpleNamespace(launched=True, launch=SimpleNamespace(launcher='cmux'))))
    refresh = AsyncMock()
    app = FastAPI()
    app.include_router(build_engine_connection_router(registry=registry, repositories=repositories,
        auth_policy=SessionAuthPolicy(lambda: 'test'), path=tmp_path/'connections.json', launcher=launcher, refresh_backends=refresh))
    client = TestClient(app); auth={'Authorization':'Bearer test'}
    assert client.post('/api/engine-connections/backend:codex/login').status_code == 401
    result=client.post('/api/engine-connections/backend:codex/login', headers=auth)
    assert result.status_code == 200, result.text
    spec = launcher.launch.call_args.args[0]
    assert spec.command == (native, 'login')
    assert launcher.launch.call_args.kwargs['conversation_id'] == 'agent-login:backend:codex'
    assert spec.path_prefix == (str(tmp_path), str(tmp_path/'support/bin'), '/usr/bin')
    assert spec.config_directory == ('CODEX_HOME', str(tmp_path/'native-config'))
    assert not (tmp_path/'connections.json').exists()
    result = client.post('/api/engine-connections/refresh', headers=auth, json={'backendIds':['backend:codex']})
    assert result.status_code == 200, result.text
    refresh.assert_awaited_once_with(['backend:codex'])
    launcher.launch.return_value.launched=False
    launcher.launch.return_value.reason='synthetic launch failure'
    assert client.post('/api/engine-connections/backend:codex/login', headers=auth).status_code == 503


def test_native_path_adapter_metadata_retains_bridge_and_rejects_unsupported(tmp_path):
    from drivers.acp.presets import PRESETS
    from drivers.acp.native_cli import apply_native_cli, supports_native_cli
    path = executable(tmp_path)
    command, env = apply_native_cli(PRESETS['pi'].command, {}, PRESETS['pi'], path)
    assert command == PRESETS['pi'].command
    assert env['PI_ACP_PI_COMMAND'] == path
    for preset in ['antigravity', 'alma']:
        assert not supports_native_cli(PRESETS[preset])
        with pytest.raises(ValueError):
            apply_native_cli(PRESETS[preset].command, {}, PRESETS[preset], path)
    for preset, flag in [('crush', '--crush-command'), ('commandcode', '--command'), ('zcode', '--command')]:
        command, _ = apply_native_cli(PRESETS[preset].command, {}, PRESETS[preset], path)
        assert command == (*PRESETS[preset].command, flag, path)


def test_selected_path_preserves_symlink_and_shell_characters_as_literal(tmp_path):
    from drivers.acp.native_cli import validated_cli_path
    binary = executable(tmp_path)
    link = tmp_path / 'cli $(this-is-a-literal)'
    link.symlink_to(binary)
    assert validated_cli_path(str(link)) == str(link)
    binary = Path(binary); binary.chmod(0o600)
    with pytest.raises(ValueError):
        validated_cli_path(str(link))
