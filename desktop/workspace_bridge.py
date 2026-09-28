"""The desktop bridge exposes only a user-mediated directory picker."""
from pathlib import Path
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
