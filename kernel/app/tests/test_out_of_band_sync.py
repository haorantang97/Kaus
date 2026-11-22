"""Out-of-band detection (2026-11-22「带外检测」).

The user resumed this conversation's native session in their own terminal, outside
Kaus. Kaus folds those turns into the timeline and leaves its own turns alone.
Protocol fakes only: MockDriver with a patched ``load_native_history``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.tests.test_surface_handoff_api import _client, _harness  # noqa: E402
from drivers.base import NativeHistory, NativeHistoryEntry, TurnAlreadyRunningError, UnsupportedCapabilityError  # noqa: E402
from runtime.event_envelope import ExtensionEvent  # noqa: E402
from runtime.surface_handoff import HISTORY_CHECKPOINT_NAME, HISTORY_RECONCILED_NAME  # noqa: E402

NATIVE = "native-oob"
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


def entry(entry_id: str, role: str, text: str, minutes: int) -> NativeHistoryEntry:
    return NativeHistoryEntry(entry_id=entry_id, role=role, text=text, occurred_at=T0 + timedelta(minutes=minutes))


async def setup(harness, client, entries: list[NativeHistoryEntry]):
    conversation_id = harness.new_conversation(client)
    conversation = await harness.repositories.conversations.get(conversation_id)
    conversation = await harness.repositories.conversations.save(conversation.model_copy(update={"native_session_id": NATIVE}))

    async def history(binding, native_session_id):
        return NativeHistory(native_session_id=native_session_id, entries=tuple(entries))
    harness.driver.load_native_history = history
    return conversation


async def kaus_sent(harness, conversation, text: str) -> None:
    await harness.host.emit_conversation_event(conversation, ExtensionEvent(namespace="kaus", name="user.message", data={"text": text, "role": "user"}))


async def test_terminal_turns_are_merged_once_and_kaus_turns_are_left_alone(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            entries = [entry("u1", "user", "写个首页", 0), entry("a1", "assistant", "好的", 1)]
            conversation = await setup(harness, client, entries)
            await kaus_sent(harness, conversation, "写个首页")
            url = f"/api/conversations/{conversation.id}/native-sync"
            assert client.post(url).json()["status"] == "none"
            # The user then went on in their own terminal.
            entries += [entry("u2", "user", "改成蓝色", 90), entry("a2", "assistant", "已改", 91), entry("t2", "tool", "edit", 91)]
            body = client.post(url).json()
            assert body == {"conversationId": conversation.id, "status": "merged", "turns": 1}
            (merged,) = [event for event in await harness.extension_events(conversation.id, HISTORY_RECONCILED_NAME)]
            assert merged.data["outOfBand"] is True and merged.data["turns"] == 1
            assert [item["entryId"] for item in merged.data["entries"]] == ["u2", "a2", "t2"]
            assert merged.data["lastEntryId"] == "t2"
            assert client.post(url).json()["status"] == "none"
    finally:
        await harness.aclose()


async def test_without_a_marker_only_turns_kaus_did_not_send_count(tmp_path: Path) -> None:
    """An older conversation has no marker: Kaus's own turns match its messages or its run times."""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            now = datetime.now(timezone.utc)
            entries = [entry("u1", "user", "第一句", 0), entry("a1", "assistant", "回答", 1),
                       NativeHistoryEntry(entry_id="u-int", role="user", text="[engine note]", occurred_at=now),
                       entry("u2", "user", "终端里问的", 300)]
            conversation = await setup(harness, client, entries)
            await kaus_sent(harness, conversation, "第一句  ")  # whitespace differences still match
            body = client.post(f"/api/conversations/{conversation.id}/native-sync").json()
            # The note written while Kaus's (still open) turn ran is Kaus's; only the terminal question counts.
            assert body["status"] == "merged" and body["turns"] == 1
            (merged,) = await harness.extension_events(conversation.id, HISTORY_RECONCILED_NAME)
            assert [item["entryId"] for item in merged.data["entries"]] == ["u2"]
    finally:
        await harness.aclose()


async def test_a_running_kaus_turn_is_not_stopped(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation = await setup(harness, client, [entry("u9", "user", "终端里问的", 5)])

            @asynccontextmanager
            async def busy(_conversation_id):
                raise TurnAlreadyRunningError("running")
                yield  # pragma: no cover
            harness.host.conversation_control = busy
            body = client.post(f"/api/conversations/{conversation.id}/native-sync").json()
            assert body["status"] == "running" and body["turns"] == 1
            assert await harness.extension_events(conversation.id, HISTORY_RECONCILED_NAME) == []
    finally:
        await harness.aclose()


async def test_handed_to_the_terminal_through_kaus_or_unreadable_is_skipped(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation = await setup(harness, client, [entry("u9", "user", "终端里问的", 5)])

            async def unsupported(binding, native_session_id):
                raise UnsupportedCapabilityError("no history")
            original = harness.driver.load_native_history
            harness.driver.load_native_history = unsupported
            url = f"/api/conversations/{conversation.id}/native-sync"
            assert client.post(url).json()["status"] == "unsupported"
            harness.driver.load_native_history = original
            assert client.post(f"/api/conversations/{conversation.id}/surface/external").status_code == 200
            assert client.post(url).json()["status"] == "skipped"
    finally:
        await harness.aclose()


async def test_checkpoint_after_a_kaus_turn_moves_the_marker_only_over_kaus_turns(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            entries = [entry("u1", "user", "第一句", 0), entry("a1", "assistant", "回答", 1)]
            conversation = await setup(harness, client, entries)
            await kaus_sent(harness, conversation, "第一句")
            await harness.coordinator.checkpoint_after_turn(conversation.id)
            (mark,) = await harness.extension_events(conversation.id, HISTORY_CHECKPOINT_NAME)
            assert mark.data == {"lastEntryId": "a1", "nativeSessionId": NATIVE}
            entries.append(entry("u2", "user", "终端里问的", 300))
            await harness.coordinator.checkpoint_after_turn(conversation.id)
            assert len(await harness.extension_events(conversation.id, HISTORY_CHECKPOINT_NAME)) == 1
    finally:
        await harness.aclose()


async def test_a_send_merges_terminal_turns_before_its_own_message(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation = await setup(harness, client, [entry("u9", "user", "终端里问的", 5)])
            response = client.post(f"/api/conversations/{conversation.id}/messages", json={"text": "接着做"})
            assert response.status_code < 300, response.text
            events = await harness.events_of(conversation.id)
            names = [(envelope.event.name, envelope.sequence) for envelope in events if envelope.event.type == "extension.event"]
            merged = next(sequence for name, sequence in names if name == HISTORY_RECONCILED_NAME)
            sent = next(sequence for name, sequence in names if name == "user.message")
            assert merged < sent
    finally:
        await harness.aclose()
