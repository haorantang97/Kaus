"""Reopening a native session must survive the Host's durable event dedupe."""

from __future__ import annotations

import asyncio

from drivers.acp.translator import AcpTranslationContext, AcpTranslator
from runtime.event_reducer import MessageItem, TimelineState, reduce_events
from runtime.tests.test_session_host import make_world


async def test_short_reply_after_reattach_keeps_its_prefix_lifecycle_and_replay():
    world = make_world()
    _, conversation = await world.add_backend(with_native_session=True)
    host = world.host

    def translator(token):
        return AcpTranslator(AcpTranslationContext(
            project_id=conversation.project_id,
            conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id,
            backend_id="backend:mock",
            session_id=conversation.native_session_id,
            run_token=token,
        ))

    def reply(t, chunks, *, resumed):
        events = [*(t.session_resumed() if resumed else t.session_created()), *t.begin_run()]
        for text in chunks:
            events.extend(t.on_session_update({
                "sessionId": conversation.native_session_id,
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
            }))
        return [*events, *t.on_stop_reason("end_turn")]

    try:
        await host.start_runtime(conversation)
        await asyncio.sleep(0)  # Consume the fake transport's session frame.
        before = translator("first-attach")
        old_events = reply(before, [f"old chunk {n};" for n in range(20)], resumed=False)
        for event in old_events:
            await host._ingest(host._runtimes[conversation.id], event)
        await host.stop_runtime(conversation.id)
        saved = await world.conversations.get(conversation.id)
        await host.start_runtime(saved)
        await asyncio.sleep(0)

        after = translator("second-attach")
        chunks = ["RESUME_PREFIX_OK\n[中文 PDF", "](/workspace/中文验收.pdf)"]
        new_events = reply(after, chunks, resumed=True)
        assert len(old_events) > len(new_events)
        for event in new_events:
            await host._ingest(host._runtimes[conversation.id], event)

        stored = await world.event_store.replay(conversation.id)
        new_run = [e for e in stored if e.run_id == after.run_id]
        assert [e.event.type for e in new_run] == [
            "run.started", "message.started", "message.delta", "message.delta", "message.completed", "run.completed",
        ]
        expected = "".join(chunks)
        for timeline in (host.timeline(conversation.id), reduce_events(TimelineState.initial(conversation.id), stored)):
            messages = [item for item in timeline.items if isinstance(item, MessageItem) and item.text == expected]
            assert len(messages) == 1
            assert messages[0].is_terminal

        # Re-delivery inside the same attachment remains idempotent.
        for event in new_events:
            await host._ingest(host._runtimes[conversation.id], event)
        assert len(await world.event_store.replay(conversation.id)) == len(stored)
    finally:
        await host.aclose()
