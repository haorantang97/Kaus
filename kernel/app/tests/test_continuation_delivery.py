"""Handoff reference delivery through the real API -> host -> fake driver path."""
import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest

from app.tests.test_chat_files import setup, upload  # noqa: F401
from app.tests.test_session_router import wait_for
from drivers.mock.fixtures import text_stream_script
from drivers.base import NativeHistory, NativeHistoryEntry
from runtime.surface_handoff import SurfaceCoordinator


def save_handoff(harness, cid, files):
    asyncio.run(harness.repositories.settings.save(f"continuation:{cid}", {
        "contextFilesVersion": 1, "files": files, "draft": "Reviewed task",
        "source": {"kind": "conversation", "id": "source", "projectId": harness.project_id}}))


def test_reference_reaches_driver_but_visible_message_stays_clean_and_next_turn_is_normal(setup):
    harness, client, cid, captured, _ = setup
    harness.driver.set_script(cid, text_stream_script())
    reference = upload(client, cid, "handoff.md", b"Prior accepted decisions").json()
    save_handoff(harness, cid, [reference])
    first = client.post(f"/api/conversations/{cid}/messages", json={"text": "Reviewed task", "clientRef": "handoff-first"})
    assert first.status_code == 202, first.text
    assert reference["ref"] in captured[0].text
    assert "当前项目的工具、技能和权限" in captured[0].text
    assert captured[0].attachments == () and captured[0].context_files == ()
    events = asyncio.run(harness.host.event_store.replay(cid))
    visible = next(e.event.data for e in events if e.event.type == "extension.event" and e.event.name == "user.message")
    assert visible["text"] == "Reviewed task" and visible["clientRef"] == "handoff-first"
    assert reference["ref"] not in str(visible)
    assert visible["nativeTextProjection"]["length"] == len("Reviewed task")
    conversation = asyncio.run(harness.repositories.conversations.get(cid)).evolve(native_session_id="fixture-native")
    raw_history = NativeHistory(native_session_id=conversation.native_session_id, entries=(
        NativeHistoryEntry(entry_id="native:handoff", role="user", text=captured[0].text),))
    harness.driver.load_local_native_history = AsyncMock(return_value=raw_history)
    harness.driver.load_native_history = AsyncMock(return_value=raw_history)
    local = asyncio.run(harness.host._load_local_native_history(cid, conversation.native_session_id))
    assert local.entries[0].text == "Reviewed task"
    surfaces = SurfaceCoordinator(session_host=harness.host, repositories=harness.repositories, registry=harness.registry, launcher=object())
    terminal_readback = asyncio.run(surfaces._load_history(conversation))
    assert terminal_readback.entries[0].text == "Reviewed task"
    wait_for(lambda: harness.host.timeline(cid).is_run_terminal)
    next_turn = client.post(f"/api/conversations/{cid}/messages", json={"text": "Next action", "clientRef": "next"})
    assert next_turn.status_code == 202, next_turn.text
    assert captured[-1].text == "Next action"


def test_client_cannot_forge_context_files_and_foreign_references_are_not_forwarded(setup):
    harness, client, cid, captured, _ = setup
    foreign = harness.new_conversation(client)
    other = upload(client, foreign, "private.md", b"Other conversation").json()
    save_handoff(harness, cid, [other])
    forged = client.post(f"/api/conversations/{cid}/messages", json={"text": "Hi", "contextFiles": [other]})
    assert forged.status_code == 422
    result = client.post(f"/api/conversations/{cid}/messages", json={"text": "Hi", "metadata": {"contextFiles": [other]}})
    assert result.status_code == 202, result.text
    assert captured[0].text == "Hi" and captured[0].context_files == ()


def test_removed_optional_transcript_does_not_block_reviewed_message(setup):
    harness, client, cid, captured, _ = setup
    reference = upload(client, cid, "handoff.md", b"Optional history").json()
    save_handoff(harness, cid, [reference])
    Path(unquote(urlparse(reference["ref"]).path)).unlink()
    response = client.post(f"/api/conversations/{cid}/messages", json={"text": "Reviewed task"})
    assert response.status_code == 202, response.text
    assert captured[0].text == "Reviewed task"


def test_created_handoff_uses_target_project_skills_at_actual_driver_start(setup):
    from app.capabilities.effective import resolve_effective_for_binding
    from app.capabilities.models import ProjectCapability
    from app.conversations.continuation import ContinuationService
    from app.projects.models import AgentBinding, Backend
    from drivers.mock.driver import MockDriver
    from runtime.event_envelope import ExtensionEvent
    harness, client, source_id, _, tmp = setup
    target_driver = MockDriver(backend_key="beta", script_factory=text_stream_script)
    harness.registry.register(target_driver)
    received_config, received_messages = [], []
    original_projection = target_driver.session_options_for
    original_send = target_driver.send_message
    async def project(conversation, effective):
        received_config.extend((entry.capability_type, entry.capability_id) for entry in effective.entries)
        return await original_projection(conversation, effective)
    async def send(runtime, content):
        received_messages.append(content)
        return await original_send(runtime, content)
    target_driver.session_options_for = project
    target_driver.send_message = send
    harness.host._capability_projector = lambda binding: resolve_effective_for_binding(harness.repositories, binding)
    async def create():
        project = await harness.repositories.projects.get(harness.project_id)
        await harness.repositories.backends.save(Backend.create(key="beta", display_name="Beta", driver_kind="mock"))
        target = AgentBinding.create(project=project, backend="beta")
        await harness.repositories.bindings.save(target)
        for kind, identity in (("skills", "shared-writing"), ("mock:skills", "source-only"), ("beta:skills", "target-only")):
            await harness.repositories.capabilities.save(ProjectCapability.create(project_id=project.id, capability_type=kind, capability_id=identity))
        source = await harness.repositories.conversations.get(source_id)
        await harness.host.emit_conversation_event(source, ExtensionEvent(namespace="kaus", name="user.message", data={"text": "Keep the accepted design", "role": "user"}))
        service = ContinuationService(harness.repositories, harness.host, harness.registry, tmp / "continuations", tmp / "uploads")
        preview = await service.preview({"kind": "conversation", "id": source_id, "projectId": project.id}, target.id)
        created = await service.create(preview["token"], preview["context"])
        return created["conversationId"], await service.detail(created["conversationId"])
    target_id, record = asyncio.run(create())
    assert received_config == [] and received_messages == []  # Opening does not run.
    result = client.post(f"/api/conversations/{target_id}/messages", json={"text": record["draft"]})
    assert result.status_code == 202, result.text
    assert ("skills", "shared-writing") in received_config
    assert ("beta:skills", "target-only") in received_config
    assert ("mock:skills", "source-only") not in received_config
    assert "Keep the accepted design" in received_messages[0].text
    assert record["files"][0]["ref"] in received_messages[0].text
    assert "file://" not in record["draft"]
    assert asyncio.run(harness.host.event_store.latest_sequence(source_id)) == 0


@pytest.mark.parametrize("source_kind", ["conversation", "archive"])
def test_transcript_links_open_only_selected_copies_after_original_deletion(setup, source_kind):
    from app.conversations.continuation import ContinuationService
    from runtime.event_envelope import ExtensionEvent
    harness, client, source_id, _, tmp = setup
    original = upload(client, source_id, "note.txt", b"Selected document").json()
    unselected = upload(client, source_id, "other.txt", b"Do not transfer").json()
    path = Path(unquote(urlparse(original["ref"]).path))
    text = f"Read [note.txt]({path}) and [relative](note.txt); [other]({unselected['ref']})"
    async def create():
        service = ContinuationService(harness.repositories, harness.host, harness.registry, tmp / "handoff", tmp / "uploads")
        if source_kind == "conversation":
            source = await harness.repositories.conversations.get(source_id)
            await harness.host.emit_conversation_event(source, ExtensionEvent(namespace="kaus", name="user.message", data={
                "text": text, "role": "user", "attachments": [original, unselected]}))
        else:
            assets = [{"hash": "one", "sourcePath": str(path), "name": "note.txt", "mime": "text/plain"},
                      {"hash": "two", "sourcePath": unquote(urlparse(unselected["ref"]).path), "name": "other.txt", "mime": "text/plain"}]
            service.archives = SimpleNamespace(detail=AsyncMock(return_value={"archive": {"title": "History", "revision": "r"}, "assets": assets}),
                store=SimpleNamespace(visible_messages=lambda *args: iter([{"role": "user", "text": text}]),
                    asset_bytes=lambda _id, _project, key, _revision: (b"Selected document" if key == "one" else b"Do not transfer", assets[0 if key == "one" else 1])))
        preview = await service.preview({"kind": source_kind, "id": source_id, "projectId": harness.project_id}, harness.binding.id)
        assert "sourceRefs" not in str(preview) and "sourcePath" not in str(preview)
        selected = next(item["id"] for item in preview["assets"] if item["name"] == "note.txt")
        target = await service.create(preview["token"], preview["context"], [selected])
        return target["conversationId"], await service.detail(target["conversationId"])
    target_id, record = asyncio.run(create())
    path.unlink()
    for original_link in (str(path), original["ref"], "note.txt"):
        opened = client.get(f"/api/conversations/{target_id}/files", params={"path": original_link})
        assert opened.status_code == 200, opened.text
        assert opened.content == b"Selected document"
    for forbidden in (unselected["ref"], str(tmp / "private.txt"), "../note.txt"):
        assert client.get(f"/api/conversations/{target_id}/files", params={"path": forbidden}).status_code == 403
    transcript = client.get(f"/api/conversations/{target_id}/files", params={"path": record["files"][0]["ref"]})
    assert text in transcript.text  # Exact original text is not rewritten.
    assert "fileAliases" not in client.get(f"/api/conversations/{target_id}").json()["continuation"]
