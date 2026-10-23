"""Trusted desktop actions for local pickers and isolated quota sign-in."""
from pathlib import Path
import os
from threading import Lock
from urllib.parse import urlsplit


class WorkspaceBridge:
    def __init__(self):
        self._window = None
        self._dialog_lock = Lock()

    def _bind(self, window):
        self._window = window

    def _trusted(self):
        if self._window is None:
            return False
        url = urlsplit(self._window.get_current_url() or "")
        return (url.scheme, url.hostname, url.port) == ("http", "127.0.0.1", 8877)

    def login_quota_connection(self, login_id):
        """Only the fresh login window's credentials cross the local API boundary."""
        import json
        import re
        import urllib.request
        from quota_login import run_stepfun_login

        if not self._trusted() or not isinstance(login_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', login_id):
            return {'status': 'failed', 'reason': 'login_failed'}
        if not self._dialog_lock.acquire(blocking=False):
            return {'status': 'failed', 'reason': 'login_in_progress'}
        token = None
        prefix = 'http://127.0.0.1:8877/api/'
        def request(path, payload=None):
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            if payload is not None: headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(prefix + path, headers=headers,
                    data=json.dumps(payload).encode() if payload is not None else None)
            with urllib.request.urlopen(req, timeout=45 if path.endswith('/complete') else 4) as response:
                return json.load(response)
        try:
            token = request('session-auth/bootstrap')['token']
            path = 'quota-connections/login/' + login_id
            login = request(path + '/claim', {})
            if login.get('provider') != 'stepfun-plan':
                return {'status': 'failed', 'reason': 'login_failed'}
            def cancelled():
                if not self._trusted(): return True
                try: return request(path).get('status') not in ('waiting', 'verifying')
                except Exception: return True
            captured = run_stepfun_login(login['config']['site'], cancelled=cancelled)
            if not self._trusted() or not captured:
                return request(path + '/cancel', {})
            return request(path + '/complete', {'secrets': captured})
        except Exception:
            if token:
                try: request('quota-connections/login/' + login_id + '/complete', {'reason': 'login_failed'})
                except Exception: pass
            return {'status': 'failed', 'reason': 'login_failed'}
        finally:
            token = None
            self._dialog_lock.release()

    def choose_directory(self, initial_path=None):
        import webview

        if not self._trusted():
            raise PermissionError("Folder selection is available only in Kaus")
        if not self._dialog_lock.acquire(blocking=False):
            return None
        try:
            directory = Path.home()
            if isinstance(initial_path, str) and initial_path:
                candidate = Path(initial_path).expanduser()
                if candidate.is_absolute() and candidate.is_dir():
                    directory = candidate.resolve()
            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=str(directory), allow_multiple=False,
            )
            if not self._trusted():
                raise PermissionError("The Kaus window has navigated away")
            if not selected:
                return None
            path = Path(selected[0]).resolve()
            if not path.is_dir():
                raise ValueError("The selected directory is unavailable")
            return str(path)
        finally:
            self._dialog_lock.release()

    def choose_executable(self, initial_path=None):
        import webview

        if not self._trusted():
            raise PermissionError("File selection is available only in Kaus")
        if not self._dialog_lock.acquire(blocking=False):
            return None
        try:
            directory = Path.home()
            if isinstance(initial_path, str) and initial_path:
                candidate = Path(initial_path).expanduser()
                candidate = candidate.parent if candidate.is_file() else candidate
                if candidate.is_absolute() and candidate.is_dir():
                    directory = candidate
            selected = self._window.create_file_dialog(
                webview.FileDialog.OPEN, directory=str(directory), allow_multiple=False,
            )
            if not self._trusted():
                raise PermissionError("The Kaus window has navigated away")
            if not selected:
                return None
            path = Path(selected[0]).expanduser().absolute()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("The selected file is not executable")
            return str(path)
        finally:
            self._dialog_lock.release()
