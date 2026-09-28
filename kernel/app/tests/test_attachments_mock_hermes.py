"""附件接通：Mock 与 Hermes 两台引擎收得下上传的文件（假数据，不调用模型）。"""

from __future__ import annotations

import asyncio
import base64
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.session_router import build_session_router
from app.tests.test_session_router import Harness
from drivers.base import AttachmentRef, MessageInput
from drivers.hermes.driver import input_with_attachments
from drivers.mock.driver import MockDriver


def _client(harness: Harness, tmp_path) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_session_router(
            session_host=harness.host,
            repositories=harness.repositories,
            registry=harness.registry,
            auth_policy=harness.auth_policy,
            attachment_root=tmp_path / "uploads",
            run_id_timeout=2,
        )
    )
    return TestClient(app, headers={"Authorization": f"Bearer {harness.token}"})


def test_mock_declares_file_attachments() -> None:
    caps = asyncio.run(MockDriver().get_capabilities())
    assert caps.card.attachments.value == "files"


def test_mock_reply_names_the_uploaded_files(tmp_path) -> None:
    harness = Harness(tmp_path)
    asyncio.run(harness.seed())
    try:
        with _client(harness, tmp_path) as client:
            cid = harness.new_conversation(client)
            uploaded = client.post(
                f"/api/conversations/{cid}/attachments",
                json={"name": "notes.md", "mimeType": "text/plain", "data": base64.b64encode(b"hello").decode()},
            )
            assert uploaded.status_code == 201, uploaded.text
            sent = client.post(
                f"/api/conversations/{cid}/messages",
                json={"text": "看一下这个文件", "attachments": [uploaded.json()]},
            )
            assert sent.status_code in (200, 202), sent.text

            texts: list[str] = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                events = client.get(f"/api/conversations/{cid}/events/snapshot").json()["events"]
                texts = [
                    event["event"].get("text") or ""
                    for event in events
                    if event["event"]["type"] == "message.completed"
                ]
                if any("notes.md" in text for text in texts):
                    break
                time.sleep(0.05)
            assert "收到 1 个附件：notes.md" in texts
    finally:
        harness.close()


def test_hermes_input_inlines_text_and_points_at_other_files() -> None:
    content = MessageInput(
        text="帮我看看",
        attachments=(
            AttachmentRef(kind="file", ref="file:///tmp/up/notes.md", name="notes.md", mime_type="text/plain", content_text="第一行"),
            AttachmentRef(kind="file", ref="file:///tmp/up/shot%201.png", name="shot 1.png", mime_type="image/png", content_base64="AAAA"),
        ),
    )
    text = input_with_attachments(content)
    assert text.startswith("帮我看看")
    assert "--- notes.md ---\n第一行\n--- notes.md 结束 ---" in text
    assert "- shot 1.png（image/png）：/tmp/up/shot 1.png" in text
    # 二进制内容不进请求体。
    assert "AAAA" not in text


def test_hermes_input_without_attachments_is_unchanged() -> None:
    assert input_with_attachments(MessageInput(text="你好")) == "你好"
