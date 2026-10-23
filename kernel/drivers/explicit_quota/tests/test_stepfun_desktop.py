"""Private IPC contract; no platform login or existing browser data required."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from test_stepfun import session

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('stepfun_desktop_login_tests', ROOT / 'desktop/quota_login.py')
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


def fake_child(monkeypatch, result, *, running=False):
    calls = []
    class Child:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            assert '--result-fd' in argv and kwargs['pass_fds']
            assert not any('access' in str(arg) or 'refresh' in str(arg) for arg in argv)
            fd = kwargs['pass_fds'][0]
            os.write(fd, json.dumps(result).encode())
            self.terminated = False
        def poll(self): return None if running and not self.terminated else 0
        def terminate(self): self.terminated = True
        def kill(self): self.terminated = True
        def wait(self, timeout): return 0
    monkeypatch.setattr(login.subprocess, 'Popen', Child)
    monkeypatch.setattr(login.sys, 'platform', 'darwin')
    return calls


def test_private_pipe_delivers_session_without_secret_arguments(monkeypatch):
    data = session()
    calls = fake_child(monkeypatch, dict(status='connected', secrets=data))
    assert login.run_stepfun_login('ai') == data
    assert calls[0][1]['stdout'] == login.subprocess.DEVNULL
    assert calls[0][1]['stderr'] == login.subprocess.DEVNULL


def test_cancelled_window_returns_no_session(monkeypatch):
    fake_child(monkeypatch, dict(status='cancelled'))
    assert login.run_stepfun_login('com') is None


def test_external_cancellation_terminates_only_login_child(monkeypatch):
    calls = fake_child(monkeypatch, dict(status='cancelled'), running=True)
    checks = iter([False, True])
    assert login.run_stepfun_login('ai', cancelled=lambda: next(checks)) is None
    assert len(calls) == 1


def test_rejects_worker_schema_errors_without_exposing_payload(monkeypatch):
    fake_child(monkeypatch, dict(status='connected', secrets={'access': 'sensitive-invalid'}))
    with pytest.raises(login.QuotaLoginError) as exc:
        login.run_stepfun_login('ai')
    assert str(exc.value) == 'request_failed'


def test_rejects_worker_raw_failure_text(monkeypatch):
    fake_child(monkeypatch, dict(status='failed', reason='server response contains token'))
    with pytest.raises(login.QuotaLoginError) as exc:
        login.run_stepfun_login('com')
    assert str(exc.value) == 'request_failed'


def test_pre_cancelled_login_does_not_spawn(monkeypatch):
    calls = fake_child(monkeypatch, dict(status='cancelled'))
    assert login.run_stepfun_login('ai', cancelled=lambda: True) is None
    assert not calls


def test_navigation_probe_reports_only_public_origin_and_boolean_state():
    event = login._navigation_event('com', 'https://platform.stepfun.com/login?token=private#account', True)
    assert event == dict(navigationCompleted=True, windowVisible=True,
                         originMatchesSelectedSite=True, origin='https://platform.stepfun.com')
    assert 'private' not in json.dumps(event) and 'account' not in json.dumps(event)


@pytest.mark.parametrize('url', ['https://other.example.test/private', 'https://platform.stepfun.com.evil.test/',
                                'https://account:secret@platform.stepfun.com/', 'http://platform.stepfun.com/',
                                'https://platform.stepfun.com:invalid/'])
def test_navigation_probe_does_not_report_other_hosts_or_credentials(url):
    event = login._navigation_event('com', url, False)
    assert event == dict(navigationCompleted=True, windowVisible=False, originMatchesSelectedSite=False, origin=None)


def test_production_worker_has_no_navigation_probe_by_default():
    import inspect
    assert inspect.signature(login._worker).parameters['_navigation_probe'].default is None
