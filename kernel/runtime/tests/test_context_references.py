from runtime.context_references import normalize_history, projection
from runtime.event_store import EventStore
from runtime.tests.test_native_primary_storage import CID, history, repo, turn  # noqa: F401


async def test_projection_survives_native_compaction_and_preserves_user_authored_paths(repo):
    visible = "Read file:///user-authored/path.txt then continue"
    dispatched = visible + "\n\nHost references: file:///private/kaus/handoff.md"
    store = EventStore(repo)
    await store.native_storage.begin(CID, "native-1")
    events = list(turn(question=visible))
    first = events[0].event
    events[0] = events[0].model_copy(update={"event": first.model_copy(update={"data": {
        **first.data, "nativeTextProjection": projection(visible, dispatched)}})})
    await store.append_many(events)
    original = history(question=dispatched)
    async def load(*_):
        return normalize_history(original, await repo.list_after(CID))
    store.native_storage.loader = load
    assert await store.native_storage.compact(CID, "native-1") == 4
    raw = await repo.get(events[0].event_id)
    assert raw.envelope.event.data["text"] == ""
    assert raw.envelope.event.data["nativeTextProjection"] == projection(visible, dispatched)
    assert await store.replay(CID) == tuple(events)
    assert original.entries[0].text == dispatched
    # A similar prefix or suffix written in the native terminal is user text.
    changed = history(question=dispatched + "\nA terminal edit")
    assert normalize_history(changed, await repo.list_after(CID)).entries[0].text == changed.entries[0].text
