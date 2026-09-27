from __future__ import annotations

import asyncio
import base64

import pytest

from drivers.acp.capabilities import capabilities_from_initialize
from drivers.acp.driver import _prompt_blocks
from drivers.acp.tests.test_driver import _driver_with_set_model, make_driver
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_translator import make_translator, update
from drivers.base import AttachmentRef, MessageInput, ModelRejectedError, UnsupportedCapabilityError
from runtime.event_reducer import MessageItem, TimelineState, reduce_events


def test_image_only_and_mixed_message_media_survive():
    translator = make_translator()
    events = list(translator.begin_run())
    content = [{"type": "text", "text": "Generated:"}, {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}]
    events.extend(translator.on_session_update(update(sessionUpdate="agent_message_chunk", content=content)))
    events.extend(translator.on_stop_reason("end_turn"))
    assert any(e.event.type == "message.delta" and e.event.text == "Generated:" for e in events)
    artifact = next(e.event.artifact for e in events if e.event.type == "artifact.created")
    assert artifact.uri == "data:image/png;base64,aW1hZ2U="
    assert artifact.mime_type == "image/png"
    state = reduce_events(TimelineState.initial(translator.context.conversation_id), events)
    assert any(item.kind == "artifact" for item in state.items)


def test_tool_mixed_content_is_not_replaced_by_text_and_media_is_deduplicated():
    translator = make_translator()
    translator.begin_run()
    content = [
        {"type": "content", "content": {"type": "text", "text": "result"}},
        {"type": "content", "content": {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}},
    ]
    start = translator.on_session_update(update(sessionUpdate="tool_call", toolCallId="t1", title="image tool", content=content))
    done = translator.on_session_update(update(sessionUpdate="tool_call_update", toolCallId="t1", status="completed", content=content))
    tool = next(e.event for e in done if e.event.type == "tool.completed")
    assert tool.output == content
    assert sum(e.event.type == "artifact.created" for e in (*start, *done)) == 1


def test_embedded_active_content_is_preserved_as_inert_text():
    translator = make_translator()
    translator.begin_run()
    source = b'<svg onload="throw 1"></svg>'
    events = translator.on_session_update(update(sessionUpdate="agent_message_chunk", content={"type": "resource", "resource": {"uri": "file:///workspace/image.svg", "mimeType": "image/svg+xml", "blob": base64.b64encode(source).decode()}}))
    artifact = events[0].event.artifact
    assert artifact.uri.startswith("data:text/plain;base64,")
    assert artifact.mime_type == "text/plain"


def test_invalid_media_is_a_notice_and_not_silent_loss():
    translator = make_translator()
    translator.begin_run()
    events = translator.on_session_update(update(sessionUpdate="agent_message_chunk", content={"type": "image", "mimeType": "image/png", "data": "***"}))
    assert [e.event.type for e in events] == ["diagnostic.notice"]


def test_phase_is_only_preserved_when_explicit_and_separates_messages():
    translator = make_translator()
    events = list(translator.begin_run())
    for text, phase in (("working", "commentary"), ("done", "final_answer")):
        events.extend(translator.on_session_update(update(sessionUpdate="agent_message_chunk", phase=phase, content={"type": "text", "text": text})))
    events.extend(translator.on_stop_reason("end_turn"))
    state = reduce_events(TimelineState.initial(translator.context.conversation_id), events)
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    assert [(m.text, m.phase) for m in messages] == [("working", "commentary"), ("done", "final_answer")]
    translator = make_translator()
    translator.begin_run()
    unknown = translator.on_session_update(update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "answer"}))
    assert all(getattr(e.event, "phase", None) is None for e in unknown)


def test_prepared_attachments_are_real_prompt_content_not_only_paths():
    message = MessageInput(text="", attachments=(
        AttachmentRef(kind="file", ref="file:///workspace/photo.png", mime_type="image/png", content_base64="aW1hZ2U="),
        AttachmentRef(kind="file", ref="file:///workspace/note.md", mime_type="text/plain", content_text="document body"),
        AttachmentRef(kind="file", ref="file:///workspace/report.pdf", mime_type="application/pdf", content_base64="cGRm"),
    ))
    assert _prompt_blocks(message) == [
        {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "file:///workspace/note.md", "mimeType": "text/plain", "text": "document body"}},
        {"type": "resource", "resource": {"uri": "file:///workspace/report.pdf", "mimeType": "application/pdf", "blob": "cGRm"}},
    ]
    assert "contentBase64" not in message.model_dump_json()
    assert "document body" not in message.model_dump_json()


@pytest.mark.parametrize("prompt, expected", [(None, "unknown"), ({}, "none"), ({"image": True}, "images"), ({"embeddedContext": True}, "files")])
def test_attachment_support_comes_from_protocol_declaration(prompt, expected):
    capabilities = {} if prompt is None else {"promptCapabilities": prompt}
    assert capabilities_from_initialize({"agentCapabilities": capabilities}).card.attachments.value == expected


async def test_cold_model_selection_reaches_engine_and_rejection_does_not_fallback(monkeypatch):
    harness = FakeAcpHarness()
    driver = _driver_with_set_model(True)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).snapshot_model(model_id="fake:large")
    from drivers.acp.client import AcpConnection
    original = AcpConnection.call
    calls = []

    async def capture(self, method, params, **kwargs):
        if method == "session/set_model":
            calls.append(params["modelId"])
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    runtime = await driver.start_runtime(conversation, "card")
    assert calls == ["fake:large"]
    updated = conversation.snapshot_model(model_id="fake:small", reasoning_mode="high")
    driver.adopt_conversation(runtime, updated)
    assert driver._state(runtime).conversation.reasoning_mode == "high"
    await driver.stop_runtime(runtime)
    with pytest.raises(ModelRejectedError):
        await driver.start_runtime(conversation.snapshot_model(model_id="fake:unknown"), "card")
    assert not driver._runtimes


async def test_cold_model_snapshot_without_selection_rpc_still_starts(monkeypatch):
    harness = FakeAcpHarness()
    driver = _driver_with_set_model(False)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding).snapshot_model(model_id="observed-default")

    async def unexpected_selection(*_args, **_kwargs):
        pytest.fail("An observed model snapshot must not call an unsupported selection RPC")

    monkeypatch.setattr(driver, "set_conversation_model", unexpected_selection)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="hello"))
        events = driver.events(runtime)
        while (await asyncio.wait_for(anext(events), 2)).event.type != "run.completed":
            pass
    finally:
        await driver.stop_runtime(runtime)


async def test_unconfirmed_cancel_does_not_emit_a_false_terminal(monkeypatch):
    harness = FakeAcpHarness()
    driver = make_driver("interrupt")
    driver._cancel_grace = 0.01
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    state = driver._state(runtime)
    try:
        await driver.send_message(runtime, MessageInput(text="work"))
        events = driver.events(runtime)
        while (await asyncio.wait_for(anext(events), 2)).event.type != "message.delta":
            pass
        notify = state.connection.notify
        monkeypatch.setattr(state.connection, "notify", lambda *_args, **_kwargs: None)
        await driver.interrupt(runtime)
        assert state.prompt_task is not None and not state.prompt_task.done()
        pending = []
        while not state.queue.empty():
            pending.append(state.queue.get_nowait())
        assert not any(e.event.type in {"run.completed", "run.interrupted", "run.failed"} for e in pending)
        monkeypatch.setattr(state.connection, "notify", notify)
        await driver.interrupt(runtime)
        terminal = None
        while terminal is None:
            event = await asyncio.wait_for(anext(events), 2)
            if event.event.type.startswith("run."):
                terminal = event.event.type
        assert terminal == "run.interrupted"
    finally:
        await driver.stop_runtime(runtime)


async def test_cancel_before_prompt_dispatch_never_starts_engine_work(monkeypatch):
    harness = FakeAcpHarness()
    driver = make_driver("interrupt")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    state = driver._state(runtime)
    original = state.connection.call
    prompts = []

    async def capture(method, params, **kwargs):
        if method == "session/prompt": prompts.append(params)
        return await original(method, params, **kwargs)

    monkeypatch.setattr(state.connection, "call", capture)
    try:
        await driver.send_message(runtime, MessageInput(text="do not start"))
        await driver.interrupt(runtime)
        assert not prompts
        pending = []
        while not state.queue.empty(): pending.append(state.queue.get_nowait())
        assert any(e.event.type == "run.interrupted" for e in pending)
    finally:
        await driver.stop_runtime(runtime)


async def test_driver_rejects_unsupported_file_content_before_starting_run():
    harness = FakeAcpHarness()
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        driver._state(runtime).prompt_capabilities = {"image": False, "embeddedContext": False}
        with pytest.raises(UnsupportedCapabilityError):
            await driver.send_message(runtime, MessageInput(text="", attachments=(AttachmentRef(kind="file", ref="file:///image.png", mime_type="image/png", content_base64="aW1hZ2U="),)))
        assert driver._state(runtime).prompt_task is None
    finally:
        await driver.stop_runtime(runtime)
