"""Bookmark persistence and API behavior without starting an agent."""
import asyncio
from datetime import datetime, timedelta, timezone

from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository
from app.persistence.sqlite import SqliteUnitOfWork
from app.tests.test_session_router import harness, client  # noqa: F401
from runtime.event_envelope import ExtensionEvent


def test_api_bookmarks_are_scoped_idempotent_and_survive_runtime_updates(harness, client):
    cid = harness.new_conversation(client)
    other = harness.new_conversation(client)

    async def seed_message():
        conversation = await harness.repositories.conversations.get(cid)
        envelope = await harness.host.emit_conversation_event(conversation, ExtensionEvent(
            namespace="kaus", name="user.message", data={"text": "Which option should we choose?"},
        ))
        return f"message:{envelope.event_id}"

    mid = asyncio.run(seed_message())
    url = f"/api/conversations/{cid}/bookmarks"
    assert client.put(url, json={"messageId": mid, "saved": True}, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.put(url, json={"messageId": "missing", "saved": True}).status_code == 404
    assert client.put(f"/api/conversations/{other}/bookmarks", json={"messageId": mid, "saved": True}).status_code == 404
    for _ in range(2):
        response = client.put(url, json={"messageId": mid, "saved": True})
        assert response.status_code == 200, response.text
    assert client.get(f"/api/conversations/{cid}").json()["bookmarkedMessageIds"] == [mid]
    assert client.get(f"/api/conversations/{other}").json()["bookmarkedMessageIds"] == []
    assert not harness.host.is_active(cid)
    assert asyncio.run(harness.host.describe_runtime_owner(cid)) is None

    async def stale_runtime_save():
        repository = harness.repositories.conversations
        conversation = await repository.get(cid)
        await repository.set_bookmark(cid, "another", True)
        await repository.save(conversation.evolve(title="Updated title"))
    asyncio.run(stale_runtime_save())
    with SqliteUnitOfWork(harness.unit_of_work.database.path) as reopened:
        assert set(asyncio.run(reopened.repositories.conversations.bookmarks(cid))) == {mid, "another"}
    for _ in range(2):
        assert client.put(url, json={"messageId": mid, "saved": False}).status_code == 200
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert asyncio.run(harness.repositories.conversations.bookmarks(cid)) == ()


async def test_memory_bookmarks_do_not_cross_conversations_or_survive_deletion():
    conversation = Conversation.create(project_id="project:test", agent_binding_id="binding:test:mock", title="Test")
    repository = InMemoryConversationRepository([conversation])
    await repository.set_bookmark(conversation.id, "message:a", True)
    await repository.set_bookmark(conversation.id, "message:a", True)
    await repository.set_bookmark("conversation:missing", "message:b", True)
    assert await repository.bookmarks(conversation.id) == ("message:a",)
    assert await repository.bookmarked_conversations() == (conversation.id,)
    assert await repository.bookmarks("conversation:missing") == ()
    await repository.delete(conversation.id)
    assert await repository.bookmarks(conversation.id) == ()
    assert await repository.bookmarked_conversations() == ()


def test_expiry_preserves_bookmarked_history_until_the_last_bookmark_is_removed(harness, client):
    saved = harness.new_conversation(client)
    ordinary = harness.new_conversation(client)

    async def exercise():
        repo = harness.repositories.conversations
        for cid in (saved, ordinary):
            conversation = await repo.get(cid)
            envelope = await harness.host.emit_conversation_event(conversation, ExtensionEvent(
                namespace="kaus", name="user.message", data={"text": "The same question in separate conversations"},
            ))
            if cid == saved:
                await repo.set_bookmark(cid, f"message:{envelope.event_id}", True)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        assert await harness.host.purge_expired_events(now=later) == 1
        assert len(await harness.host.event_store.replay(saved)) == 1
        assert len(await harness.host.event_store.replay(ordinary)) == 0
        mid = (await repo.bookmarks(saved))[0]
        await repo.set_bookmark(saved, mid, False)
        assert await harness.host.purge_expired_events(now=later) == 1
        assert len(await harness.host.event_store.replay(saved)) == 0
    asyncio.run(exercise())
