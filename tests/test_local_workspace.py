"""Real temporary filesystem navigation and authentication."""
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "kernel")]
from app.api.session_auth import SessionAuthPolicy
from local_workspace import build_workspace_router


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
