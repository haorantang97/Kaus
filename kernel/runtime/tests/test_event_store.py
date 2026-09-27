"""Event Store 服务的单元测试（v1.0 §8.5 / §11.1）。

覆盖任务书要求的四件事：追加、按 ``(conversationId, sequence)`` 范围读取、
``expiresAt`` 清理（保留期可配）、按 ``eventId`` 去重。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.events.repository import InMemoryEventStoreRepository
from runtime.event_envelope import (
    AgentEventEnvelope,
    DiagnosticNotice,
    EventSource,
    MessageDelta,
    make_envelope,
)
from runtime.event_store import DEFAULT_RETENTION, EventStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
SOURCE = EventSource(driver_kind="mock", driver_version="0.1.0")


def envelope(
    sequence: int,
    *,
    conversation: str = "conversation:11111111-1111-1111-1111-111111111111",
    event_id: str | None = None,
    diagnostic: bool = False,
) -> AgentEventEnvelope:
    event = (
        DiagnosticNotice(level="warning", message="注意")
        if diagnostic
        else MessageDelta(message_id="msg-1", text=f"块{sequence}")
    )
    return make_envelope(
        event=event,
        project_id="project:demo",
        conversation_id=conversation,
        agent_binding_id="binding:demo:mock",
        backend_id="backend:mock",
        sequence=sequence,
        source=SOURCE,
        occurred_at=NOW,
        event_id=event_id or f"evt-{conversation[-4:]}-{sequence}",
    )


def make_store(**kwargs) -> EventStore:
    return EventStore(InMemoryEventStoreRepository(), clock=lambda: NOW, **kwargs)


async def test_append_is_idempotent_by_event_id() -> None:
    """同一 ``eventId`` 重复投递不产生第二行（N §7.3 规则 9）。"""
    store = make_store()
    first = await store.append(envelope(0))
    second = await store.append(envelope(0))

    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.stored.event_id == first.stored.event_id
    assert len(await store.read_range(first.stored.conversation_id)) == 1


async def test_range_read_is_open_below_and_closed_above() -> None:
    """``after_sequence`` 不含自身，``until_sequence`` 含自身（``?after=`` 的语义）。"""
    store = make_store()
    for sequence in range(5):
        await store.append(envelope(sequence))
    conversation = envelope(0).conversation_id

    tail = await store.read_range(conversation, after_sequence=2)
    assert [row.sequence for row in tail] == [3, 4]

    window = await store.read_range(conversation, after_sequence=0, until_sequence=3)
    assert [row.sequence for row in window] == [1, 2, 3]

    limited = await store.read_range(conversation, limit=2)
    assert [row.sequence for row in limited] == [0, 1]


async def test_next_sequence_continues_after_the_buffer() -> None:
    store = make_store()
    conversation = envelope(0).conversation_id
    assert await store.next_sequence(conversation) == 0
    await store.append(envelope(0))
    await store.append(envelope(1))
    assert await store.next_sequence(conversation) == 2


async def test_retention_is_configurable_and_defaults_to_7d() -> None:
    """保留期可配；默认 7 天（AD-39），诊断类不会比普通事件留得更久（v1.0 §8.5）。"""
    assert DEFAULT_RETENTION == timedelta(days=7)

    store = make_store()
    outcome = await store.append(envelope(0))
    assert outcome.stored.expires_at == NOW + timedelta(days=7)

    diagnostic = await store.append(envelope(1, diagnostic=True))
    assert diagnostic.stored.expires_at <= outcome.stored.expires_at

    roomy = make_store(retention=timedelta(days=7))
    assert roomy.retention_for("message.delta") == timedelta(days=7)
    assert roomy.retention_for("diagnostic.notice") == timedelta(hours=24)


async def test_retention_must_be_positive() -> None:
    """v1.0 §8.5 不接受「无保留策略」，因此没有零/负保留期这种取值。"""
    with pytest.raises(ValueError):
        make_store(retention=timedelta(0))


async def test_purge_expired_drops_only_expired_rows() -> None:
    store = make_store(retention=timedelta(hours=1))
    conversation = envelope(0).conversation_id
    await store.append(envelope(0), now=NOW - timedelta(hours=2))
    await store.append(envelope(1), now=NOW)

    removed = await store.purge_expired(now=NOW)
    assert removed == 1
    assert [row.sequence for row in await store.read_range(conversation)] == [1]


async def test_purge_conversation_empties_the_buffer() -> None:
    """v1.0 §11.3：整段缓冲可丢弃，内容的可恢复性不依赖它。"""
    store = make_store()
    conversation = envelope(0).conversation_id
    await store.append(envelope(0))
    await store.append(envelope(1))

    assert await store.purge_conversation(conversation) == 2
    assert await store.read_range(conversation) == ()
    assert await store.latest_sequence(conversation) is None


async def test_conversations_do_not_share_a_sequence_space() -> None:
    store = make_store()
    other = "conversation:22222222-2222-2222-2222-222222222222"
    await store.append(envelope(0))
    await store.append(envelope(0, conversation=other))

    assert await store.latest_sequence(other) == 0
    assert len(await store.read_range(other)) == 1
