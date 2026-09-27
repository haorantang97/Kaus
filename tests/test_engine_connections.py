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
