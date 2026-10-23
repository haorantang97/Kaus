"""Local history survives retention, migration and restart independently of a driver.

Only temporary databases and synthetic payloads are used. Native account/session
files and real model processes are not required to read the archived timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter

from app.events.models import RETAIN_UNTIL_CONVERSATION_GONE, StoredEvent
from app.events.repository import InMemoryEventStoreRepository
from app.persistence.sqlite.database import SqliteDatabase
from app.persistence.sqlite.events import SqliteEventStoreRepository
from app.persistence.sqlite.migrations import current_version, migrate
from runtime.event_envelope import (
    AGENT_EVENT_TYPES,
    AgentEvent,
    AgentEventEnvelope,
    EventSource,
    make_envelope,
)
from runtime.event_reducer import TimelineState, reduce_events
from runtime.event_store import EventStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
AFTER_MONTH = NOW + timedelta(days=45)
CONVERSATION = "conversation:11111111-1111-1111-1111-111111111111"
SOURCE = EventSource(driver_kind="mock", driver_version="1.0")
IMAGE_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
FILE_URI = "data:text/plain;base64,aGVsbG8="
EVENT_ADAPTER = TypeAdapter(AgentEvent)


def visible_payloads() -> list[dict]:
    return [
        {"type": "session.created", "sessionId": "native-1"},
        {"type": "session.resumed", "sessionId": "native-1"},
        {"type": "session.state", "state": "ready"},
        {"type": "extension.event", "namespace": "kaus", "name": "user.message",
         "data": {"text": "Read this attachment", "clientRef": "client-1",
                  "attachments": [{"name": "input.txt", "uri": FILE_URI}]}},
        {"type": "extension.event", "namespace": "kaus", "name": "model.adopted",
         "data": {"modelId": "model-test"}},
        {"type": "run.started", "runId": "run-1"},
        {"type": "message.started", "messageId": "answer-1"},
        {"type": "message.delta", "messageId": "answer-1", "text": "Done."},
        {"type": "reasoning.delta", "messageId": "answer-1", "text": "Checking."},
        {"type": "reasoning.status", "status": "completed", "summary": "Checked."},
        {"type": "plan.updated", "planId": "plan-1",
         "entries": [{"content": "Read attachment", "status": "completed"}]},
        {"type": "tool.started", "callId": "tool-1", "name": "read_file"},
        {"type": "tool.updated", "callId": "tool-1", "output": "hello"},
        {"type": "tool.completed", "callId": "tool-1", "output": "hello"},
        {"type": "terminal.started", "terminalId": "terminal-1", "command": "echo hello"},
        {"type": "terminal.updated", "terminalId": "terminal-1", "output": "hello\n"},
        {"type": "terminal.completed", "terminalId": "terminal-1", "exitCode": 0},
        {"type": "file.changed", "path": "output.txt", "diff": "+hello", "operation": "created"},
        {"type": "artifact.created", "artifact": {
            "artifactId": "image-1", "kind": "image", "uri": IMAGE_URI, "mimeType": "image/png"}},
        {"type": "artifact.created", "artifact": {
            "artifactId": "file-1", "kind": "file", "uri": FILE_URI, "mimeType": "text/plain"}},
        {"type": "permission.requested", "request": {"requestId": "permission-1", "title": "Write file"}},
        {"type": "permission.resolved", "requestId": "permission-1", "decision": "allow"},
        {"type": "question.requested", "request": {"requestId": "question-1", "prompt": "Continue?"}},
        {"type": "question.resolved", "requestId": "question-1"},
        {"type": "usage.updated", "usage": {"inputTokens": 30, "outputTokens": 10}},
        {"type": "message.completed", "messageId": "answer-1", "text": "Done."},
        {"type": "run.completed", "runId": "run-1"},
        {"type": "run.interrupted", "runId": "run-2", "reason": "cancelled"},
        {"type": "run.failed", "runId": "run-3", "error": {"code": "test", "message": "Stopped"}},
        {"type": "extension.event", "namespace": "kaus", "name": "user.message.failed",
         "data": {"clientRef": "client-2", "code": "test"}},
    ]


def temporary_payloads() -> list[dict]:
    return [
        {"type": "diagnostic.notice", "level": "info", "message": "Temporary diagnostic"},
        {"type": "extension.event", "namespace": "native", "name": "debug", "data": {}},
        {"type": "extension.event", "namespace": "kaus", "name": "unrecognized", "data": {}},
        {"type": "authentication.requested", "request": {
            "requestId": "auth-1", "message": "Sign in", "methods": ["device-code"]}},
        {"type": "authentication.resolved", "requestId": "auth-1", "outcome": "authenticated"},
        {"type": "authentication.requested", "request": {
            "requestId": "auth-expired", "message": "Expired sign-in", "methods": ["device-code"]}},
    ]


def envelopes(payloads: list[dict], *, start: int = 0) -> tuple[AgentEventEnvelope, ...]:
    return tuple(make_envelope(
        event=EVENT_ADAPTER.validate_python(payload),
        event_id=f"event-{sequence}", sequence=sequence,
        project_id="project:test", conversation_id=CONVERSATION,
        agent_binding_id="binding:test:mock", backend_id="backend:mock",
        source=SOURCE, occurred_at=NOW, run_id="run-1",
        native_session_id="native-1",
        native_event_id=f"native-event-{sequence}" if sequence % 2 else None,
    ) for sequence, payload in enumerate(payloads, start=start))


def assert_replay_is_complete(history: tuple[AgentEventEnvelope, ...]) -> None:
    state = reduce_events(TimelineState.initial(CONVERSATION), history)
    assert state.message_text("answer-1") == "Done."
    assert [item.artifact.uri for item in state.items_of("artifact")] == [IMAGE_URI, FILE_URI]
    user = next(item for item in state.items_of("message") if item.role == "user")
    assert user.attachments == ({"name": "input.txt", "uri": FILE_URI},)
    assert state.usage.input_tokens == 30
    assert state.items_of("terminal")[0].output == "hello\n"
    interactions = state.items_of("interaction")
    assert {item.interaction_kind for item in interactions} == {"permission", "question"}
    assert all(item.status == "resolved" and item.is_terminal for item in interactions)


@pytest.mark.parametrize("history_recoverable", [True, False, None])
async def test_all_visible_records_outlive_native_history_capability(history_recoverable) -> None:
    visible = envelopes(visible_payloads())
    temporary = envelopes(temporary_payloads(), start=len(visible))
    # Keep this fixture complete as the public event protocol grows.
    assert {event.event.type for event in (*visible, *temporary)} == set(AGENT_EVENT_TYPES)
    store = EventStore(InMemoryEventStoreRepository(), clock=lambda: NOW)
    outcomes = await store.append_many((*visible, *temporary), history_recoverable=history_recoverable)
    assert all(item.stored.expires_at == RETAIN_UNTIL_CONVERSATION_GONE for item in outcomes[:len(visible)])
    assert await store.purge_expired(now=AFTER_MONTH) == len(temporary)
    assert await store.replay(CONVERSATION) == visible
    assert_replay_is_complete(await store.replay(CONVERSATION))


async def test_sqlite_reopen_preserves_inline_files_ids_and_resolved_interactions(tmp_path) -> None:
    path = tmp_path / "history.db"
    visible = envelopes(visible_payloads())
    temporary = envelopes(temporary_payloads(), start=len(visible))
    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database), clock=lambda: NOW)
        await store.append_many((*visible, *temporary), history_recoverable=True)

    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database), clock=lambda: AFTER_MONTH)
        assert await store.purge_expired() == len(temporary)
        assert await store.replay(CONVERSATION) == visible
        assert_replay_is_complete(await store.replay(CONVERSATION))
        # Fresh content appends without reusing expired diagnostic sequence IDs.
        newer = envelopes([{"type": "message.completed", "messageId": "answer-2", "text": "New."}],
                          start=len(visible) + len(temporary))
        await store.append_many(newer)

    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database))
        assert await store.replay(CONVERSATION) == (*visible, *newer)
        assert await store.purge_conversation(CONVERSATION) == len(visible) + len(newer)
        assert await store.replay(CONVERSATION) == ()


async def test_v16_preserves_existing_payloads_and_expires_only_temporary_rows(tmp_path) -> None:
    path = tmp_path / "upgrade.db"
    visible = envelopes(visible_payloads())
    temporary = envelopes(temporary_payloads(), start=len(visible))
    all_events = (*visible, *temporary)
    old_expiry = NOW + timedelta(days=7)
    with SqliteDatabase(path, apply_migrations=False) as database:
        migrate(database, target_version=15)
        repository = SqliteEventStoreRepository(database)
        await repository.append_many(tuple(
            StoredEvent.from_envelope(event, now=NOW, expires_at=old_expiry)
            for event in all_events
        ))
        before = {row["event_id"]: row["envelope_json"] for row in database.query_all("SELECT * FROM event_store")}
        assert migrate(database, target_version=16) == (16,)
        assert current_version(database) == 16
        assert migrate(database, target_version=16) == ()
        after = {row["event_id"]: row["envelope_json"] for row in database.query_all("SELECT * FROM event_store")}
        assert before == after
        assert all("expiresAt" not in raw and "expires_at" not in raw for raw in after.values())
        for event in visible:
            assert (await repository.get(event.event_id)).expires_at == RETAIN_UNTIL_CONVERSATION_GONE
        for event in temporary:
            assert (await repository.get(event.event_id)).expires_at == old_expiry

    # The upgrade and a subsequent append survive another database restart.
    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database), clock=lambda: AFTER_MONTH)
        assert await store.purge_expired() == len(temporary)
        assert await store.replay(CONVERSATION) == visible
        assert_replay_is_complete(await store.replay(CONVERSATION))
        newer = envelopes([{"type": "message.completed", "messageId": "answer-2", "text": "New."}],
                          start=len(all_events))
        await store.append_many(newer, history_recoverable=False)

    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database))
        assert await store.purge_expired(now=AFTER_MONTH + timedelta(days=45)) == 0
        assert await store.replay(CONVERSATION) == (*visible, *newer)


def test_v16_skips_malformed_legacy_envelopes_without_blocking_upgrade(tmp_path) -> None:
    with SqliteDatabase(tmp_path / "malformed.db", apply_migrations=False) as database:
        migrate(database, target_version=15)
        database.run(
            "INSERT INTO event_store (event_id, conversation_id, sequence, envelope_json, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("malformed", CONVERSATION, 0, "{invalid", NOW.isoformat(), NOW.isoformat()),
        )
        assert migrate(database) == (16, 17, 18)
        row = database.query_one("SELECT envelope_json, expires_at FROM event_store WHERE event_id = 'malformed'")
        assert row["envelope_json"] == "{invalid"
        assert row["expires_at"] == NOW.isoformat()


async def test_terminal_history_and_pending_cursor_survive_upgrade_purge_and_reopen(tmp_path) -> None:
    path = tmp_path / "terminal-history.db"
    terminal = envelopes([
        {"type": "extension.event", "namespace": "kaus", "name": "history.checkpoint",
         "data": {"nativeSessionId": "native-1", "lastEntryId": "before-terminal"}},
        {"type": "extension.event", "namespace": "kaus", "name": "history.reconciled",
         "data": {"nativeSessionId": "native-1", "lastEntryId": "terminal-answer",
                  "complete": True, "entries": [{"entryId": "terminal-answer",
                  "role": "assistant", "kind": "message", "text": "Terminal answer"}]}},
        {"type": "extension.event", "namespace": "kaus", "name": "history.reconciled",
         "data": {"nativeSessionId": "native-1", "lastEntryId": "terminal-answer",
                  "complete": False, "pending": True, "entries": []}},
    ])
    transient = envelopes([{"type": "extension.event", "namespace": "vendor",
                           "name": "history.reconciled", "data": {}}], start=3)
    with SqliteDatabase(path, apply_migrations=False) as database:
        migrate(database, target_version=16)
        repository = SqliteEventStoreRepository(database)
        await repository.append_many(tuple(StoredEvent.from_envelope(event, now=NOW,
            expires_at=NOW + timedelta(days=1)) for event in (*terminal, *transient)))
        before = {row["event_id"]: row["envelope_json"] for row in database.query_all("SELECT * FROM event_store")}
        assert migrate(database) == (17, 18)
        assert before == {row["event_id"]: row["envelope_json"] for row in database.query_all("SELECT * FROM event_store")}
        assert migrate(database) == ()

    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database), clock=lambda: AFTER_MONTH)
        assert await store.purge_expired() == 1
        assert await store.replay(CONVERSATION) == terminal
        appended = envelopes([{"type": "extension.event", "namespace": "kaus",
            "name": "history.reconciled", "data": {"complete": True, "entries": []}}], start=4)
        result = await store.append_many(appended)
        assert result[0].stored.expires_at == RETAIN_UNTIL_CONVERSATION_GONE

    with SqliteDatabase(path) as database:
        store = EventStore(SqliteEventStoreRepository(database), clock=lambda: AFTER_MONTH + timedelta(days=45))
        assert await store.purge_expired() == 0
        assert await store.replay(CONVERSATION) == (*terminal, *appended)
