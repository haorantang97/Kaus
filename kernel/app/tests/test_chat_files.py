"""Uploads, durable message references and local preview security, using fake data."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.conversation_files import ConversationFiles, MAX_UPLOAD_BYTES
from app.api.session_router import build_session_router
from app.tests.test_session_router import Harness
from runtime.event_envelope import ExtensionEvent
from runtime.event_reducer import MessageItem, TimelineState, reduce_event


@pytest.fixture
def setup(tmp_path):
    harness = Harness(tmp_path)
    asyncio.run(harness.seed())
    captured = []
    original_send = harness.driver.send_message
    original_caps = harness.driver.get_capabilities

    async def capture(runtime, content):
        captured.append(content)
        return await original_send(runtime, content)

    async def capabilities():
        caps = await original_caps()
        from runtime.capability_matrix import AttachmentCapability
        return caps.model_copy(update={"card": caps.card.model_copy(update={"attachments": AttachmentCapability(value="files")})})

    harness.driver.send_message = capture
    harness.driver.get_capabilities = capabilities
    app = FastAPI()
    app.include_router(build_session_router(
        session_host=harness.host, repositories=harness.repositories,
        registry=harness.registry, auth_policy=harness.auth_policy,
        attachment_root=tmp_path / "uploads", run_id_timeout=2,
    ))
    with TestClient(app, headers={"Authorization": f"Bearer {harness.token}"}) as client:
        cid = harness.new_conversation(client)
        yield harness, client, cid, captured, tmp_path
    harness.close()


def upload(client, cid, name="notes.md", content=b"attachment content"):
    return client.post(f"/api/conversations/{cid}/attachments", json={"name": name, "mimeType": "text/plain", "data": base64.b64encode(content).decode()})


def snapshot(harness, cid, path):
    async def save():
        conversation = await harness.repositories.conversations.get(cid)
        await harness.host.emit_conversation_event(conversation, ExtensionEvent(namespace="kaus", name="runtime.workspace", data={"workspaceRoot": str(path)}))
    asyncio.run(save())


def test_upload_send_attachment_only_and_replay_references(setup):
    harness, client, cid, captured, _ = setup
    response = upload(client, cid)
    assert response.status_code == 201, response.text
    attachment = response.json()
    assert attachment["name"] == "notes.md"
    sent = client.post(f"/api/conversations/{cid}/messages", json={"text": "", "attachments": [attachment], "clientRef": "with-file"})
    assert sent.status_code == 202, sent.text
    assert captured[0].attachments[0].content_text == "attachment content"
    events = asyncio.run(harness.host.event_store.replay(cid))
    user = next(e for e in events if e.event.type == "extension.event" and e.event.name == "user.message")
    assert user.event.data["attachments"] == [attachment]
    assert "contentText" not in user.model_dump_json()
    assert "attachment content" not in user.model_dump_json()
    state = TimelineState.initial(cid)
    for event in events:
        state = reduce_event(state, event)
    message = next(item for item in state.items if isinstance(item, MessageItem) and item.role == "user")
    assert message.attachments[0]["ref"] == attachment["ref"]
    downloaded = client.get(f"/api/conversations/{cid}/files", params={"path": attachment["ref"], "download": "true"})
    assert downloaded.content == b"attachment content"
    assert downloaded.headers["content-disposition"].startswith("attachment;")


def test_attachment_and_file_routes_require_header_auth_and_origin(setup):
    _, client, cid, _, _ = setup
    path = upload(client, cid).json()["ref"]
    base = f"/api/conversations/{cid}"
    for suffix in ("/attachments", "/files"):
        method = client.post if suffix == "/attachments" else client.get
        response = method(base + suffix, headers={"Authorization": ""}, params={"path": path, "token": "test-token-0123456789"})
        assert response.status_code == 401
        response = method(base + suffix, headers={"Origin": "https://untrusted.example"}, params={"path": path})
        assert response.status_code == 403


def test_no_cross_conversation_upload_access_even_inside_workspace(setup):
    harness, client, cid, _, tmp = setup
    attachment = upload(client, cid).json()
    other = harness.new_conversation(client)
    snapshot(harness, other, tmp)
    for method, suffix, kwargs in (
        (client.get, "/files", {"params": {"path": attachment["ref"]}}),
        (client.post, "/messages", {"json": {"attachments": [attachment]}}),
    ):
        response = method(f"/api/conversations/{other}" + suffix, **kwargs)
        assert response.status_code == 403, response.text


def test_workspace_preview_is_safe_text_and_does_not_follow_project_changes(setup):
    harness, client, cid, _, tmp = setup
    workspace = tmp / "project"
    workspace.mkdir()
    html = workspace / "preview.html"
    html.write_text('<script>throw "must not execute"</script>', encoding="utf-8")
    # Without a runtime-observed snapshot, a project's current setting is not enough.
    asyncio.run(harness.repositories.projects.save(asyncio.run(harness.repositories.projects.get(harness.project_id)).evolve(workspace_root=str(workspace))))
    base = f"/api/conversations/{cid}/files"
    assert client.get(base, params={"path": str(html)}).status_code == 403
    snapshot(harness, cid, workspace)
    response = client.get(base, params={"path": "preview.html"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    new_workspace = tmp / "new-project"
    new_workspace.mkdir()
    (new_workspace / "other.txt").write_text("other project", encoding="utf-8")
    project = asyncio.run(harness.repositories.projects.get(harness.project_id))
    asyncio.run(harness.repositories.projects.save(project.evolve(workspace_root=str(new_workspace))))
    assert client.get(base, params={"path": str(new_workspace / "other.txt")}).status_code == 403
    assert client.get(base, params={"path": str(html)}).status_code == 200


def test_traversal_symlinks_sensitive_paths_and_remote_urls_are_rejected(setup):
    harness, client, cid, _, tmp = setup
    workspace = tmp / "project"
    workspace.mkdir()
    outside = tmp / "outside.txt"
    outside.write_text("outside fixture", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside)
    (workspace / "config.json").write_text("{}", encoding="utf-8")
    (workspace / ".private").mkdir()
    (workspace / ".private" / "secret.txt").write_text("test fixture", encoding="utf-8")
    snapshot(harness, cid, workspace)
    for path in ("../outside.txt", str(outside), "escape.txt", "config.json", ".private/secret.txt", "http://127.0.0.1:8877/api/health", "file://localhost/etc/passwd", "file:///etc/passwd", "https://example.com/picture.png"):
        response = client.get(f"/api/conversations/{cid}/files", params={"path": path})
        assert response.status_code == 403, (path, response.text)


def test_invalid_uploads_and_filename_sanitization(setup):
    _, client, cid, _, _ = setup
    assert upload(client, cid, "malware.exe").status_code == 415
    assert upload(client, cid, "config.json").status_code == 400
    assert upload(client, cid, "empty.txt", b"").status_code == 413
    assert upload(client, cid, "huge.txt", b"a" * (MAX_UPLOAD_BYTES + 1)).status_code == 413
    invalid = client.post(f"/api/conversations/{cid}/attachments", json={"name": "image.png", "data": "invalid***"})
    assert invalid.status_code == 400
    safe = upload(client, cid, "../../nested\\file.txt").json()
    assert "/" not in safe["name"] and "\\" not in safe["name"]
    assert Path(unquote(urlsplit(safe["ref"]).path)).is_file()


def test_special_files_and_hardlink_aliases_are_rejected(setup):
    harness, client, cid, _, tmp = setup
    workspace = tmp / "project"
    workspace.mkdir()
    outside = tmp / "outside.txt"
    outside.write_text("synthetic outside content", encoding="utf-8")
    os.link(outside, workspace / "alias.txt")
    os.mkfifo(workspace / "pipe.txt")
    snapshot(harness, cid, workspace)
    for path in ("alias.txt", "pipe.txt"):
        response = client.get(f"/api/conversations/{cid}/files", params={"path": path})
        assert response.status_code == 403


def test_empty_message_and_unsupported_engine_are_explicit(setup):
    harness, client, cid, captured, _ = setup
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": " "}).status_code == 400
    attachment = upload(client, cid).json()
    from drivers.mock.driver import MockDriver
    harness.driver.get_capabilities = MockDriver().get_capabilities
    response = client.post(f"/api/conversations/{cid}/messages", json={"attachments": [attachment]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "attachments_unsupported"
    assert not captured


def test_symlinked_upload_directory_cannot_write_outside(tmp_path):
    store = ConversationFiles(tmp_path / "uploads")
    store.upload_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store.conversation_dir("c").symlink_to(outside, target_is_directory=True)
    from app.api.api_errors import ApiError
    with pytest.raises(ApiError, match="范围"):
        store.upload("c", name="note.txt", data="b2s=")
    assert list(outside.iterdir()) == []


def test_api_stale_interrupt_does_not_cancel_current_run(setup):
    harness, client, cid, _, _ = setup
    from drivers.mock.fixtures import hold_script
    harness.driver.set_script(cid, hold_script(run_id="current-run"))
    sent = client.post(f"/api/conversations/{cid}/messages", json={"text": "work"})
    assert sent.status_code == 202
    current = sent.json()["runId"]
    stale = client.post(f"/api/conversations/{cid}/interrupt", json={"expectedRunId": "old-run"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_run"
    assert client.get(f"/api/conversations/{cid}").json()["runState"] == "running"
    stopped = client.post(f"/api/conversations/{cid}/interrupt", json={"expectedRunId": current})
    assert stopped.status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["runState"] == "idle"
