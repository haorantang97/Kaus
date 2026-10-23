"""Fresh-window credentials go only to the authenticated local service."""
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'kernel')]
from desktop.workspace_bridge import WorkspaceBridge


def setup(monkeypatch, *, captured=None, url='http://127.0.0.1:8877/config'):
    calls = []
    window = SimpleNamespace(get_current_url=lambda: url)
    bridge = WorkspaceBridge(); bridge._bind(window)
    def opened(site, *, cancelled):
        assert site == 'com' and not cancelled()
        return captured
    monkeypatch.setitem(sys.modules, 'quota_login', SimpleNamespace(run_stepfun_login=opened))
    def request(req, timeout):
        calls.append(req)
        path = req.full_url
        if path.endswith('/bootstrap'): value = {'token': 'local-only'}
        elif path.endswith('/claim'): value = {'provider': 'stepfun-plan', 'config': {'site': 'com'}}
        elif path.endswith('/complete'): value = {'status': 'connected', 'connectionId': 'id'}
        elif path.endswith('/cancel'): value = {'status': 'cancelled'}
        else: value = {'status': 'waiting'}
        return BytesIO(json.dumps(value).encode())
    monkeypatch.setattr('urllib.request.urlopen', request)
    return bridge, window, calls


def test_bridge_keeps_secrets_out_of_ui_result(monkeypatch):
    bridge, _, calls = setup(monkeypatch, captured={'access': 'synthetic-access', 'refresh': 'synthetic-refresh'})
    result = bridge.login_quota_connection('A' * 43)
    assert result == {'status': 'connected', 'connectionId': 'id'}
    assert all(r.full_url.startswith('http://127.0.0.1:8877/api/') for r in calls)
    assert all(r.headers.get('Authorization') == 'Bearer local-only' for r in calls[1:])
    sent = json.loads(calls[-1].data)
    assert sent['secrets']['access'] == 'synthetic-access'
    assert 'synthetic' not in json.dumps(result)


def test_cancelled_window_cancels_ticket_without_credentials(monkeypatch):
    bridge, _, calls = setup(monkeypatch)
    assert bridge.login_quota_connection('B' * 43) == {'status': 'cancelled'}
    assert calls[-1].full_url.endswith('/cancel')
    assert not any('/complete' in r.full_url for r in calls)


def test_untrusted_origin_and_invalid_id_do_not_start_login(monkeypatch):
    bridge, window, calls = setup(monkeypatch, url='https://untrusted.invalid')
    assert bridge.login_quota_connection('C' * 43)['status'] == 'failed'
    window.get_current_url = lambda: 'http://127.0.0.1:8877/config'
    assert bridge.login_quota_connection('../bootstrap')['status'] == 'failed'
    assert not calls
