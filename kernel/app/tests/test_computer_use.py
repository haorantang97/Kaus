"""The setup endpoint shares Kaus token and origin authorization."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.computer_use import build_computer_use_router
from app.api.session_auth import SessionAuthPolicy


def application():
    service = SimpleNamespace(snapshot=AsyncMock(return_value={
        "agents": [], "fallbacks": [], "pendingResources": [],
    }), open_permission_settings=AsyncMock(), reveal_permission_host=AsyncMock())
    app = FastAPI()
    app.include_router(build_computer_use_router(
        service, auth_policy=SessionAuthPolicy(lambda: "fixture-local-token")))
    return TestClient(app, base_url="http://127.0.0.1:8877"), service


def test_status_requires_bearer_token_and_does_not_accept_query_token():
    client, service = application()
    for path, headers in (
        ("/api/computer-use", {}),
        ("/api/computer-use?token=fixture-local-token", {}),
        ("/api/computer-use", {"Authorization": "Bearer wrong-fixture-token"}),
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert "fixture-local-token" not in response.text
        assert "wrong-fixture-token" not in response.text
    service.snapshot.assert_not_called()


def test_cross_site_origin_is_rejected_even_with_correct_token():
    client, service = application()
    response = client.get("/api/computer-use", headers={
        "Authorization": "Bearer fixture-local-token", "Origin": "https://untrusted.example",
    })
    assert response.status_code == 403
    service.snapshot.assert_not_called()


def test_authorized_snapshot_preserves_refresh_flag():
    client, service = application()
    response = client.get("/api/computer-use?force=true", headers={
        "Authorization": "Bearer fixture-local-token", "Origin": "http://127.0.0.1:8877",
    })
    assert response.status_code == 200
    assert response.json() == {"agents": [], "fallbacks": [], "pendingResources": []}
    service.snapshot.assert_awaited_once_with(force=True)


def test_invalid_force_value_never_calls_service():
    client, service = application()
    response = client.get("/api/computer-use?force=unexpected", headers={
        "Authorization": "Bearer fixture-local-token",
    })
    assert response.status_code == 422
    service.snapshot.assert_not_called()


def test_permission_navigation_uses_same_token_and_origin_guards():
    client, service = application()
    endpoint = "/api/computer-use/permissions/accessibility"
    assert client.post(endpoint).status_code == 401
    assert client.post(endpoint, headers={"Authorization": "Bearer fixture-local-token",
        "Origin": "https://untrusted.example"}).status_code == 403
    service.open_permission_settings.assert_not_called()
    response = client.post(endpoint, headers={"Authorization": "Bearer fixture-local-token"})
    assert response.status_code == 200 and response.json() == {"opened": True}
    service.open_permission_settings.assert_awaited_once_with("accessibility")


def test_permission_navigation_rejects_arbitrary_targets_and_hides_os_errors():
    client, service = application()
    headers = {"Authorization": "Bearer fixture-local-token"}
    assert client.post("/api/computer-use/permissions/full-disk-access", headers=headers).status_code == 404
    service.open_permission_settings.assert_not_called()
    service.open_permission_settings.side_effect = OSError("/private/fixture/secret")
    response = client.post("/api/computer-use/permissions/screen-recording", headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "settings_open_failed"}
    assert "secret" not in response.text


def test_reveal_requires_auth_and_only_accepts_installed_tool_identifiers():
    client, service = application()
    assert client.post("/api/computer-use/permissions/hosts/peekaboo/reveal").status_code == 401
    headers = {"Authorization": "Bearer fixture-local-token"}
    assert client.post("/api/computer-use/permissions/hosts/arbitrary/reveal", headers=headers).status_code == 404
    service.reveal_permission_host.assert_not_called()
    response = client.post("/api/computer-use/permissions/hosts/peekaboo/reveal", headers=headers)
    assert response.status_code == 200
    service.reveal_permission_host.assert_awaited_once_with("peekaboo")


def test_pending_navigation_does_not_claim_opened_or_return_failure():
    client, service = application()
    headers = {"Authorization": "Bearer fixture-local-token"}
    service.open_permission_settings.return_value = False
    service.reveal_permission_host.return_value = False
    for endpoint in ("/api/computer-use/permissions/accessibility",
                     "/api/computer-use/permissions/hosts/peekaboo/reveal"):
        response = client.post(endpoint, headers=headers)
        assert response.status_code == 202
        assert response.json() == {"opened": False, "pending": True}


def test_setup_mutations_require_auth_and_reject_foreign_origin():
    client, service = application()
    for method, path, body in (
        ('put', '/api/computer-use/enabled', {'enabled': True}),
        ('post', '/api/computer-use/setup', None),
        ('post', '/api/computer-use/permissions/request', None),
        ('post', '/api/computer-use/permissions/verify', None),
    ):
        request = getattr(client, method)
        assert request(path, json=body).status_code == 401
        assert request(path, json=body, headers={'Authorization': 'Bearer fixture-local-token',
            'Origin': 'https://untrusted.example'}).status_code == 403


def test_enable_payload_is_strict_and_returns_service_snapshot():
    client, service = application()
    service.set_enabled = AsyncMock(return_value={'enabled': True, 'setup': {'status': 'checking'}})
    headers = {'Authorization': 'Bearer fixture-local-token'}
    for payload in ({}, {'enabled': 'true'}, {'enabled': 1}, {'enabled': True, 'command': 'unexpected'}):
        assert client.put('/api/computer-use/enabled', json=payload, headers=headers).status_code == 422
    service.set_enabled.assert_not_called()
    response = client.put('/api/computer-use/enabled', json={'enabled': True}, headers=headers)
    assert response.status_code == 200 and response.json()['setup']['status'] == 'checking'
    service.set_enabled.assert_awaited_once_with(True)


def test_setup_retry_and_permission_request_hide_errors_and_handle_disabled():
    client, service = application()
    service.retry_setup = AsyncMock(side_effect=ValueError('private error'))
    service.request_permissions = AsyncMock(side_effect=RuntimeError('/private/secret'))
    headers = {'Authorization': 'Bearer fixture-local-token'}
    response = client.post('/api/computer-use/setup', headers=headers)
    assert response.status_code == 409 and response.json() == {'detail': 'not_enabled'}
    response = client.post('/api/computer-use/permissions/request', headers=headers)
    assert response.status_code == 503 and response.json() == {'detail': 'permission_request_failed'}
    service.request_permissions.side_effect = None
    service.request_permissions.return_value = False
    response = client.post('/api/computer-use/permissions/request', headers=headers)
    assert response.status_code == 202 and response.json() == {'opened': False, 'pending': True}
    service.open_permission_settings.assert_not_called()


def test_permission_status_is_authenticated_and_uses_lightweight_probe():
    client, service = application()
    service.permission_status = AsyncMock(return_value={'setup': {
        'status': 'ready', 'permissions': {'accessibility': True, 'screen-recording': True}}})
    endpoint = '/api/computer-use/permissions'
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={'Authorization': 'Bearer fixture-local-token',
        'Origin': 'https://untrusted.example'}).status_code == 403
    service.permission_status.assert_not_called()
    result = client.get(endpoint, headers={'Authorization': 'Bearer fixture-local-token'})
    assert result.status_code == 200
    assert result.json()['setup']['permissions']['screen-recording'] is True
    service.permission_status.assert_awaited_once_with()
    service.snapshot.assert_not_called()


def test_capture_verification_reports_pending_success_and_safe_failure():
    client, service = application()
    service.verify_capture = AsyncMock(return_value=False)
    headers = {'Authorization': 'Bearer fixture-local-token'}
    endpoint = '/api/computer-use/permissions/verify'
    result = client.post(endpoint, headers=headers)
    assert result.status_code == 202 and result.json()['pending'] is True
    service.verify_capture.return_value = True
    assert client.post(endpoint, headers=headers).json() == {'opened': True}
    service.verify_capture.side_effect = RuntimeError('/private/secret')
    result = client.post(endpoint, headers=headers)
    assert result.status_code == 503 and result.json() == {'detail': 'capture_verification_failed'}
    service.verify_capture.side_effect = ValueError('disabled')
    assert client.post(endpoint, headers=headers).status_code == 409
    service.open_permission_settings.assert_not_called()
