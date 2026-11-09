import asyncio

from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository
from app.persistence.sqlite import SqliteDatabase, SqliteUnitOfWork
from app.persistence.sqlite.migrations import migrate
from app.tests.test_session_router import harness, client  # noqa: F401


def _index_entry(client, cid):
    return next(e for e in client.get("/api/conversations").json()["conversations"] if e["id"] == cid)


def test_viewed_sequence_moves_forward_only_and_reaches_the_index(harness, client):
    cid = harness.new_conversation(client)
    other = harness.new_conversation(client)
    url = f"/api/conversations/{cid}/viewed"
    assert _index_entry(client, cid)["viewedSequence"] is None
    assert client.put(url, json={"sequence": 3}, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.put(url, json={"sequence": -1}).status_code in (400, 422)
    assert client.put("/api/conversations/conversation:missing/viewed", json={"sequence": 1}).status_code == 404
    assert client.put(url, json={"sequence": 5}).json() == {"viewedSequence": 5}
    assert client.put(url, json={"sequence": 2}).json() == {"viewedSequence": 5}
    assert _index_entry(client, cid)["viewedSequence"] == 5
    assert _index_entry(client, other)["viewedSequence"] is None
    assert not harness.host.is_active(cid)
    with SqliteUnitOfWork(harness.unit_of_work.database.path) as reopened:
        assert asyncio.run(reopened.repositories.conversations.viewed_sequences([cid, other])) == {cid: 5}
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert asyncio.run(harness.repositories.conversations.viewed_sequences([cid])) == {}


async def test_memory_views_ignore_unknown_conversations_and_deletion():
    conversation = Conversation.create(project_id="project:test", agent_binding_id="binding:test:mock", title="Test")
    repository = InMemoryConversationRepository([conversation])
    assert await repository.mark_viewed(conversation.id, 4) == 4
    assert await repository.mark_viewed(conversation.id, 1) == 4
    assert await repository.mark_viewed("conversation:missing", 9) == 9
    assert await repository.viewed_sequences([conversation.id, "conversation:missing"]) == {conversation.id: 4}
    await repository.delete(conversation.id)
    assert await repository.viewed_sequences([conversation.id]) == {}


def test_upgrade_counts_existing_conversations_as_viewed(tmp_path):
    database = SqliteDatabase(tmp_path / "domain.sqlite3", apply_migrations=False)
    migrate(database, target_version=21)
    database.run(
        "INSERT INTO event_store (event_id, conversation_id, sequence, envelope_json, expires_at, created_at)"
        " VALUES ('e1', 'conversation:a', 1, '{}', 'z', 'z'), ('e2', 'conversation:a', 7, '{}', 'z', 'z')"
    )
    migrate(database)
    rows = database.query_all("SELECT conversation_id, viewed_sequence FROM conversation_views")
    assert [(r["conversation_id"], r["viewed_sequence"]) for r in rows] == [("conversation:a", 7)]
    database.close()


def test_queue_is_stored_per_conversation_and_bounded(harness, client):
    cid = harness.new_conversation(client)
    other = harness.new_conversation(client)
    url = f"/api/conversations/{cid}/queue"
    assert client.get(url).json() == {"items": []}
    items = [{"id": "q1", "text": "然后跑一下测试"}, {"id": "q2", "text": "再提交"}]
    assert client.put(url, json={"items": items}).json() == {"items": items}
    assert client.get(url).json() == {"items": items}
    assert client.get(f"/api/conversations/{other}/queue").json() == {"items": []}
    assert client.put(url, json={"items": [{"id": "x", "text": ""}]}).status_code in (400, 422)
    assert client.put(url, json={"items": [{"id": str(n), "text": "t"} for n in range(11)]}).status_code in (400, 422)
    assert client.put("/api/conversations/conversation:missing/queue", json={"items": []}).status_code == 404
    assert not harness.host.is_active(cid)
