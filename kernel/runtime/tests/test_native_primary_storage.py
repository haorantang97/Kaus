"""Native-primary storage uses only synthetic local histories and temp databases."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from app.events.repository import InMemoryEventStoreRepository
from app.persistence.sqlite.database import SqliteDatabase
from app.persistence.sqlite.events import SqliteEventStoreRepository
from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from runtime.event_envelope import AgentEvent, EventSource, make_envelope
from runtime.event_store import EventStore

CID = 'conversation:11111111-2222-3333-4444-555555555555'
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
EVENT = TypeAdapter(AgentEvent)


def event(payload, sequence, *, run_id='r1'):
    return make_envelope(event=EVENT.validate_python(payload), event_id=f'e{sequence}',
        sequence=sequence, project_id='project:test', conversation_id=CID,
        agent_binding_id='binding:test', backend_id='backend:test', native_session_id='native-1',
        run_id=run_id, source=EventSource(driver_kind='mock'), occurred_at=NOW)


def turn(start=0, question='question', answer='answer', client_ref='client-1'):
    return tuple(event(payload, index) for index, payload in enumerate([
        {'type':'extension.event','namespace':'kaus','name':'user.message',
         'data':{'text':question,'clientRef':client_ref,'attachmentCount':0}},
        {'type':'run.started','runId':'r1'},
        {'type':'message.started','messageId':f'm{start}'},
        {'type':'message.delta','messageId':f'm{start}','text':answer[:2]},
        {'type':'message.delta','messageId':f'm{start}','text':answer[2:]},
        {'type':'message.completed','messageId':f'm{start}','text':answer},
        {'type':'run.completed','runId':'r1'},
    ], start=start))


def history(question='question', answer='answer', **kwargs):
    return NativeHistory(native_session_id='native-1', entries=(
        NativeHistoryEntry(entry_id='native:q',role='user',text=question),
        NativeHistoryEntry(entry_id='native:a',role='assistant',text=answer)), **kwargs)


@pytest.fixture(params=['memory','sqlite'])
def repo(request, tmp_path):
    if request.param == 'memory':
        yield InMemoryEventStoreRepository()
    else:
        with SqliteDatabase(tmp_path/'events.db') as database:
            yield SqliteEventStoreRepository(database)


def source(store, value):
    box = SimpleNamespace(value=value, calls=[])
    async def load(conversation_id, native_session_id):
        box.calls.append((conversation_id,native_session_id))
        if isinstance(box.value, Exception):
            raise box.value
        return box.value
    store.native_storage.loader = load
    return box


async def test_complete_local_source_releases_only_new_text_and_replays_exactly(repo):
    store = EventStore(repo)
    old = event({'type':'message.completed','messageId':'old','text':'PREEXISTING'},0)
    await store.append(old)
    before = (await repo.get(old.event_id)).model_dump_json()
    await store.native_storage.begin(CID,'native-1')
    current = turn(start=1)
    await store.append_many(current)
    source(store,history())
    assert await store.native_storage.compact(CID,'native-1') == 4
    assert (await repo.get(old.event_id)).model_dump_json() == before
    raw = await repo.list_after(CID,after_sequence=0)
    assert sum(row.native_reference is not None for row in raw) == 4
    assert all(row.envelope.event.data['text'] == '' if row.envelope.event.type == 'extension.event'
               else row.envelope.event.text == '' for row in raw if row.native_reference)
    assert await store.replay(CID) == (old,*current)
    assert await store.replay(CID,after_sequence=3) == tuple(e for e in current if e.sequence>3)
    assert await store.next_sequence(CID) == 8
    assert (await store.append(current[3])).is_duplicate
    assert await store.native_storage.compact(CID,'native-1') == 0
    assert await store.message_receipt(CID,'client-1') == (current[0],)


@pytest.mark.parametrize('value',[
    history(complete=False,missing=('tool_records','non_text_content')),
    history(earlier_cursor='older'),
    FileNotFoundError('native cleanup'),
    UnsupportedCapabilityError('no local source'),
])
async def test_incomplete_remote_unsupported_or_missing_source_retains_every_payload(repo,value):
    store = EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    original = turn()
    await store.append_many(original)
    source(store,value)
    assert await store.native_storage.compact(CID,'native-1') == 0
    assert await store.replay(CID) == original
    assert not any(row.native_reference for row in await repo.list_after(CID))


async def test_native_cleanup_or_changed_content_is_not_resurrected_and_host_data_survives(repo):
    store = EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    original = turn()
    host_only = event({'type':'extension.event','namespace':'kaus','name':'model.adopted',
                      'data':{'to':'test-model'}},len(original))
    await store.append_many((*original,host_only))
    box = source(store,history())
    assert await store.native_storage.compact(CID,'native-1') == 4
    box.value = FileNotFoundError('native cleanup')
    missing = await store.replay(CID)
    assert [e.event_id for e in missing] == [e.event_id for e in (*original,host_only)]
    assert 'question' not in ''.join(e.model_dump_json() for e in missing)
    assert 'answer' not in ''.join(e.model_dump_json() for e in missing)
    assert missing[-1] == host_only
    assert (await store.native_storage.status(CID))['status'] == 'source_unavailable'
    box.value = history(answer='newer answer')
    restored = await store.replay(CID)
    assert restored[0] == original[0]
    assert restored[5].event.text == ''
    assert await store.next_sequence(CID) == len(missing)


async def test_host_only_rejected_message_and_inflight_turn_remain_local(repo):
    store = EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    original = turn()
    failed = event({'type':'extension.event','namespace':'kaus','name':'user.message.failed',
                    'data':{'clientRef':'client-1','code':'rejected'}},1)
    rows = (original[0],failed,*[event(e.event.model_dump(),i+2) for i,e in enumerate(original[1:])])
    await store.append_many(rows)
    box = source(store,history())
    await store.native_storage.compact(CID,'native-1')
    assert (await repo.get('e0')).native_reference is None
    trailing = event({'type':'extension.event','namespace':'kaus','name':'user.message',
                      'data':{'text':'future native input'}},len(rows))
    await store.append(trailing)
    box.value = NativeHistory(native_session_id='native-1',entries=(NativeHistoryEntry(
        entry_id='native:future',role='user',text='future native input'),))
    await store.native_storage.compact(CID,'native-1')
    assert (await repo.get(trailing.event_id)).native_reference is None


async def test_repeated_ambiguous_messages_are_kept_locally(repo):
    store = EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    original = turn()
    await store.append_many(original)
    duplicate = NativeHistoryEntry(entry_id='native:q2',role='user',text='question')
    source(store,history().model_copy(update={'entries':(*history().entries,duplicate)}))
    assert await store.native_storage.compact(CID,'native-1') == 3
    assert (await repo.get('e0')).native_reference is None
    assert await store.replay(CID) == original


async def test_sqlite_reopen_readthrough_and_explicit_conversation_delete(tmp_path):
    path=tmp_path/'events.db'
    with SqliteDatabase(path) as db:
        store=EventStore(SqliteEventStoreRepository(db))
        await store.native_storage.begin(CID,'native-1')
        await store.append_many(turn())
        source(store,history())
        await store.native_storage.compact(CID,'native-1')
    with SqliteDatabase(path) as db:
        repo=SqliteEventStoreRepository(db)
        store=EventStore(repo)
        source(store,history())
        assert await store.replay(CID) == turn()
        assert await store.next_sequence(CID) == 7
        assert await store.purge_conversation(CID) == 7
        assert await repo.get_native_storage(CID) is None


async def test_restored_native_source_recovers_status_without_losing_hybrid_coverage(repo):
    store=EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    await store.append_many(turn())
    await store.append(event({'type':'reasoning.delta','messageId':'m0','text':'host reasoning'},7))
    box=source(store,history())
    await store.native_storage.compact(CID,'native-1')
    # A retained older/local record remains part of coverage after restoration.
    status=await store.native_storage.status(CID)
    box.value=FileNotFoundError('offline')
    await store.replay(CID)
    assert (await store.native_storage.status(CID))['status']=='source_unavailable'
    box.value=history()
    await store.replay(CID)
    restored=await store.native_storage.status(CID)
    assert restored['status']==status['status']
    assert restored['missing']==status['missing']


async def test_host_only_unaccepted_message_does_not_match_an_unrelated_native_row(repo):
    store=EventStore(repo)
    await store.native_storage.begin(CID,'native-1')
    rows=(turn()[0],event({'type':'run.completed','runId':'r1'},1))
    await store.append_many(rows)
    source(store,history())
    assert await store.native_storage.compact(CID,'native-1')==0
    assert await store.replay(CID)==rows


async def test_session_host_terminal_compacts_and_reopens_without_sequence_duplication():
    import asyncio
    from runtime.tests.test_session_host import make_world
    from drivers.mock.driver import MockDriver
    from drivers.mock.fixtures import text_stream_script
    from runtime.event_reducer import TimelineState, reduce_events
    class LocalDriver(MockDriver):
        missing = False
        async def load_local_native_history(self,binding,native_session_id):
            if self.missing:
                raise FileNotFoundError("native cleanup")
            return history(answer='Hello').model_copy(update={'native_session_id':native_session_id})
    world=make_world()
    driver,conversation=await world.add_backend(driver=LocalDriver(),script=text_stream_script(chunks=('Hello',)),with_native_session=True)
    await world.host.start_runtime(conversation)
    await world.host.send_message(conversation.id,'question')
    for _ in range(100):
        await asyncio.sleep(0.01)
        rows=await world.event_store._repository.list_after(conversation.id)
        if any(r.native_reference for r in rows):
            break
    assert any(r.native_reference for r in rows)
    original=await world.event_store.replay(conversation.id)
    last=await world.event_store.next_sequence(conversation.id)
    # An idle but retained runtime must not leak stale body into continuation.
    driver.missing = True
    readthrough = await world.host.timeline_from_store(conversation.id)
    assert all(not item.text for item in readthrough.items_of('message'))
    assert (await world.host.native_storage_status(conversation))['status'] == 'source_unavailable'
    driver.missing = False
    assert any(item.text == 'Hello' for item in (await world.host.timeline_from_store(conversation.id)).items_of('message'))
    assert (await world.host.native_storage_status(conversation))['status'] != 'source_unavailable'
    await world.host.stop_runtime(conversation.id)
    assert await world.event_store.replay(conversation.id)==original
    state=reduce_events(TimelineState.initial(conversation.id),original)
    assert any(item.text=='Hello' for item in state.items_of('message'))
    assert await world.event_store.next_sequence(conversation.id)==last


async def test_terminal_reconciliation_refs_keep_original_metadata_and_follow_native_cleanup(repo):
    store=EventStore(repo)
    old=event({'type':'extension.event','namespace':'kaus','name':'history.reconciled',
        'data':{'nativeSessionId':'native-1','entries':[{'entryId':'older','role':'user','text':'OLD PRESERVED'}]}},0)
    await store.append(old)
    prior=(await repo.get(old.event_id)).model_dump_json()
    await store.native_storage.begin(CID,'native-1')
    native=history()
    payload={'type':'extension.event','namespace':'kaus','name':'history.reconciled','data':{
        'nativeSessionId':'native-1','entries':[entry.model_dump(mode='json',by_alias=True) for entry in native.entries],
        'complete':True,'gaps':[],'lastEntryId':'native:a'}}
    current=event(payload,1)
    await store.append(current)
    box=source(store,native)
    assert await store.native_storage.compact(CID,'native-1')==1
    assert (await repo.get(old.event_id)).model_dump_json()==prior
    raw=await repo.get(current.event_id)
    assert [entry['text'] for entry in raw.envelope.event.data['entries']]==['','']
    assert len(raw.native_reference['parts'])==2
    assert await store.replay(CID)==(old,current)
    assert (await store.append(current)).is_duplicate
    box.value=FileNotFoundError('native cleanup')
    replay=await store.replay(CID)
    assert replay[0]==old
    assert replay[1].event_id==current.event_id and replay[1].sequence==current.sequence
    assert [item['text'] for item in replay[1].event.data['entries']]==['','']
    assert replay[1].event.data['lastEntryId']=='native:a'
    assert (await store.native_storage.status(CID))['status']=='source_unavailable'
    box.value=native
    assert await store.replay(CID)==(old,current)
    assert (await store.native_storage.status(CID))['status']=='hybrid'
