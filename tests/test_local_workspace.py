"""Real temporary filesystem navigation, authentication, and native cancellation."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "kernel")]
from app.api.session_auth import SessionAuthPolicy
from local_workspace import build_workspace_router
from desktop.workspace_bridge import WorkspaceBridge


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_workspace_router(auth_policy=SessionAuthPolicy(lambda: "test-workspace-token")))
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-workspace-token"}


def test_lists_directories_without_file_contents_and_navigates_up(client, tmp_path):
    (tmp_path / "a space ' $folder").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "private.txt").write_text("never in directory responses")
    result = client.get("/api/workspace/directories", headers=AUTH, params={"path": str(tmp_path)})
    assert result.status_code == 200
    body = result.json()
    assert body["directories"] == [{"name": "a space ' $folder", "path": str(tmp_path / "a space ' $folder")}]
    assert body["parent"] == str(tmp_path.parent)
    assert "private.txt" not in result.text
    hidden = client.get("/api/workspace/directories", headers=AUTH, params={"path": str(tmp_path), "show_hidden": True}).json()
    assert len(hidden["directories"]) == 2
    child = client.get("/api/workspace/directories", headers=AUTH, params={"path": body["directories"][0]["path"]}).json()
    assert child["parent"] == str(tmp_path)


def test_auth_origin_and_invalid_paths(client, tmp_path):
    route = "/api/workspace/directories"
    assert client.get(route).status_code == 401
    assert client.get(route, headers={**AUTH, "Origin": "https://untrusted.invalid"}).status_code == 403
    assert client.get(route, headers=AUTH, params={"path": "relative/path"}).status_code == 400
    assert client.get(route, headers=AUTH, params={"path": str(tmp_path / "missing")}).status_code == 404
    (tmp_path / "file").touch()
    assert client.get(route, headers=AUTH, params={"path": str(tmp_path / "file")}).status_code == 404


def native_bridge(monkeypatch, result, url="http://127.0.0.1:8877/conversations/new"):
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=20)))
    calls = []
    def dialog(kind, **kwargs):
        calls.append((kind, kwargs))
        return result
    window = SimpleNamespace(get_current_url=lambda: url, create_file_dialog=dialog)
    bridge = WorkspaceBridge()
    bridge._bind(window)
    return bridge, calls, window


def test_native_selection_and_cancel(monkeypatch, tmp_path):
    bridge, calls, _ = native_bridge(monkeypatch, (str(tmp_path),))
    assert bridge.choose_directory(str(tmp_path)) == str(tmp_path)
    assert calls == [(20, {"directory": str(tmp_path), "allow_multiple": False})]
    bridge, _, _ = native_bridge(monkeypatch, None)
    assert bridge.choose_directory() is None


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1:8000/", "file:///tmp/index.html"])
def test_native_bridge_rejects_other_origins(monkeypatch, url):
    bridge, calls, _ = native_bridge(monkeypatch, None, url)
    with pytest.raises(PermissionError):
        bridge.choose_directory()
    assert not calls


def test_native_selection_does_not_leak_after_navigation(monkeypatch, tmp_path):
    bridge, _, window = native_bridge(monkeypatch, (str(tmp_path),))
    def dialog(*args, **kwargs):
        window.get_current_url = lambda: "https://example.com"
        return (str(tmp_path),)
    window.create_file_dialog = dialog
    with pytest.raises(PermissionError):
        bridge.choose_directory()
