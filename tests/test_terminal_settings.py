"""Local terminal preference, project scope, and auth boundaries."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "kernel")]
from app.projects.models import Project
from session_bootstrap import attach_session_api
from terminal_settings import TerminalPreferences


def application(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_settings.installed_terminals", lambda: [
        {"id": "cmux", "label": "cmux", "installed": True},
        {"id": "terminal", "label": "Terminal", "installed": True},
        {"id": "iterm2", "label": "iTerm2", "installed": False},
    ])
    app = FastAPI()
    runtime = attach_session_api(app,
        config_loader=lambda: {"features": {"session_host_v1": True, "session_host_background": False}},
        db_path=tmp_path / "domain.sqlite3", token_path=tmp_path / "token", log=lambda _: None)
    client = TestClient(app)
    token = client.get("/api/session-auth/bootstrap").json()["token"]
    return client, runtime, {"Authorization": "Bearer " + token}


def test_settings_require_auth_and_persist_across_restarts(tmp_path, monkeypatch):
    client, runtime, auth = application(tmp_path, monkeypatch)
    try:
        assert client.get("/api/terminal/settings").status_code == 401
        assert client.get("/api/terminal/settings", headers=auth).json()["app"] == "cmux"
        changed = client.put("/api/terminal/settings", headers=auth, json={"app": "terminal"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["app"] == "terminal"
        assert runtime.surface_coordinator._launcher.selected_app() == "terminal"
        assert TerminalPreferences(tmp_path / "terminal-preferences.json").selected() == "terminal"
        assert (tmp_path / "terminal-preferences.json").stat().st_mode & 0o777 == 0o600
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize("body", [{"app": "untrusted"}, {"app": "iterm2"}, {"app": "cmux", "command": "sh"}, {"app": []}])
def test_settings_accept_only_installed_allowlisted_launchers(tmp_path, monkeypatch, body):
    client, runtime, auth = application(tmp_path, monkeypatch)
    try:
        assert client.put("/api/terminal/settings", headers=auth, json=body).status_code == 400
        assert not (tmp_path / "terminal-preferences.json").exists()
        assert client.put("/api/terminal/settings", headers={**auth, "Origin": "https://untrusted.invalid"}, json={"app": "terminal"}).status_code == 403
    finally:
        asyncio.run(runtime.aclose())


def test_project_launch_uses_only_its_directory_and_creates_no_chat_or_lease(tmp_path, monkeypatch):
    client, runtime, auth = application(tmp_path, monkeypatch)
    calls = []
    async def launch(cwd, *, title):
        calls.append((cwd, title))
        return {"launcher": "cmux", "cwd": str(cwd), "launched": True, "reason": None}
    runtime.surface_coordinator._launcher.open_directory = launch
    try:
        work = tmp_path / "a project ' $(do-not-run)"
        work.mkdir()
        project = asyncio.run(runtime.repositories.projects.save(Project.create(slug="terminal-test", display_name="Project", workspace_root=str(work))))
        r = client.post(f"/api/projects/{project.id}/terminal", headers=auth, json={"cwd": "/", "command": "untrusted"})
        assert r.status_code == 200, r.text
        assert calls == [(work.resolve(), "Project")]
        assert r.json()["projectId"] == project.id
        assert not asyncio.run(runtime.repositories.conversations.list_recent())
        assert not asyncio.run(runtime.repositories.leases.list_all())
        assert client.post("/api/projects/default/terminal", headers=auth).status_code == 409
        assert len(calls) == 1
    finally:
        asyncio.run(runtime.aclose())


def test_workspace_trust_defaults_on_and_toggles_without_changing_launcher(tmp_path, monkeypatch):
    client, runtime, auth = application(tmp_path, monkeypatch)
    try:
        assert client.get("/api/terminal/settings", headers=auth).json()["trustWorkspace"] is True
        assert runtime.surface_coordinator._launcher._trust_provider() is True
        changed = client.put("/api/terminal/settings", headers=auth, json={"trustWorkspace": False})
        assert changed.status_code == 200, changed.text
        assert changed.json()["trustWorkspace"] is False and changed.json()["app"] == "cmux"
        assert runtime.surface_coordinator._launcher._trust_provider() is False
        preferences = TerminalPreferences(tmp_path / "terminal-preferences.json")
        assert preferences.selected() == "cmux" and preferences.trust_workspace() is False
        assert client.put("/api/terminal/settings", headers=auth, json={"trustWorkspace": "no"}).status_code == 400
        assert client.put("/api/terminal/settings", headers=auth, json={}).status_code == 400
    finally:
        asyncio.run(runtime.aclose())


def test_legacy_preference_file_keeps_trust_default(tmp_path):
    path = tmp_path / "terminal-preferences.json"
    path.write_text('{"version": 1, "app": "terminal"}\n')
    preferences = TerminalPreferences(path)
    assert preferences.selected() == "terminal" and preferences.trust_workspace() is True
    preferences.save(trust_workspace=False)
    assert preferences.selected() == "terminal" and preferences.trust_workspace() is False


def test_terminal_status_accepts_only_the_launch_token(tmp_path, monkeypatch):
    client, runtime, auth = application(tmp_path, monkeypatch)
    try:
        board = runtime.surface_coordinator._launcher.status_board
        token = board.register("terminal-launch:a", "conversation:a")
        url = "/api/terminal-status/terminal-launch:a"
        assert client.post(url, json={"event": "notification", "message": "x"}).status_code == 403
        assert client.post(url, headers={"X-Kaus-Status-Token": "wrong"}, json={"event": "stop"}).status_code == 403
        assert client.post(url, headers={"X-Kaus-Status-Token": token}, json={"event": "approve"}).status_code == 403
        # The session token is not a substitute for the launch token.
        assert client.post(url, headers=auth, json={"event": "stop"}).status_code == 403
        accepted = client.post(url, headers={"X-Kaus-Status-Token": token},
                               json={"event": "notification", "message": "Claude needs your permission to use Bash"})
        assert accepted.status_code == 200, accepted.text
        status = board.status("conversation:a")
        assert status.state == "waiting" and "permission" in status.message
        board.release("conversation:a")
        assert client.post(url, headers={"X-Kaus-Status-Token": token}, json={"event": "stop"}).status_code == 403
    finally:
        asyncio.run(runtime.aclose())
