"""Durable send receipts: synthetic events only, no Agent/runtime startup."""
from __future__ import annotations

import pytest

httpx = pytest.importorskip('httpx')
pytest.importorskip('fastapi')

from app.conversations.models import Conversation
from app.events.repository import InMemoryEventStoreRepository
from app.persistence.sqlite import SqliteUnitOfWork
from app.tests.test_session_router import Harness, TEST_TOKEN, _seeded
from runtime.event_envelope import EventSource, ExtensionEvent, make_envelope
from runtime.event_store import EventStore


CONVERSATION = 'conversation:11111111-1111-4111-8111-111111111111'
OTHER = 'conversation:22222222-2222-4222-8222-222222222222'


def client(app, token=TEST_TOKEN):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
        headers={'Authorization':f'Bearer {token}'} if token else {})


async def append(store, sequence, ref='same-ref', *, conversation_id=CONVERSATION,
                 name='user.message', namespace='kaus', data=None):
    payload = {'clientRef':ref, 'text':f'synthetic {sequence}'} if data is None else data
    envelope=make_envelope(event=ExtensionEvent(namespace=namespace,name=name,data=payload),
        project_id='project:workbench', conversation_id=conversation_id,
        agent_binding_id='binding:workbench:mock', backend_id='backend:mock',
        sequence=sequence, source=EventSource(driver_kind='mock'))
    await store.append(envelope)
    return envelope


async def conversation(built, *, other=False):
    return await built.repositories.conversations.save(Conversation.create(
        project_id=built.other_project_id if other else built.project_id,
        agent_binding_id=built.other_binding.id if other else built.binding.id, title='Receipt fixture'))


@pytest.fixture(params=['memory','sqlite'])
def store(request,tmp_path):
    unit=SqliteUnitOfWork(tmp_path/'events.sqlite3') if request.param=='sqlite' else None
    try:
        yield EventStore(unit.repositories.events if unit else InMemoryEventStoreRepository())
    finally:
        if unit:unit.close()


async def test_latest_receipt_and_only_its_latest_failure_are_returned(store):
    assert await store.message_receipt(CONVERSATION,'same-ref') == ()
    first=await append(store,0)
    assert await store.message_receipt(CONVERSATION,'same-ref') == (first,)
    await append(store,1,name='user.message.failed')
    retry=await append(store,2)
    # An older failure must not mark the subsequent retry as failed.
    assert await store.message_receipt(CONVERSATION,'same-ref') == (retry,)
    await append(store,3,name='user.message.failed')
    final_failure=await append(store,4,name='user.message.failed')
    assert await store.message_receipt(CONVERSATION,'same-ref') == (retry,final_failure)


async def test_receipts_are_exactly_scoped_and_ignore_non_product_data(store):
    wanted=await append(store,0)
    await append(store,1,name='user.message.failed',namespace='third-party')
    await append(store,2,name='unrelated.event')
    await append(store,3,ref='same-ref-suffix',name='user.message.failed')
    await append(store,4,data={'nested':{'clientRef':'same-ref'}})
    await append(store,5,data='same-ref')
    await append(store,0,conversation_id=OTHER)
    await append(store,1,conversation_id=OTHER,name='user.message.failed')
    assert await store.message_receipt(CONVERSATION,'same-ref') == (wanted,)
    assert await store.message_receipt(CONVERSATION,'missing') == ()
    # A standalone failed event does not prove that this request was recorded.
    await append(store,6,ref='orphan',name='user.message.failed')
    assert await store.message_receipt(CONVERSATION,'orphan') == ()


async def test_empty_snapshot_cursor_preserves_first_sequence_zero(tmp_path):
    built=await _seeded(tmp_path)
    try:
        current=await conversation(built)
        async with client(built.app) as api:
            path=f'/api/conversations/{current.id}/events/snapshot'
            initial=(await api.get(path)).json()
            assert initial['events']==[] and initial['lastSequence']==-1
            first=await append(built.host.event_store,0,conversation_id=current.id)
            response=await api.get(path,params={'since':initial['lastSequence']})
            assert response.status_code==200,response.text
            events=response.json()['events']
            assert [e['sequence'] for e in events]==[0]
            assert events[0]['eventId']==first.event_id
            caught_up=(await api.get(path,params={'since':0})).json()
            assert caught_up['events']==[] and caught_up['lastSequence']==0
    finally:
        await built.aclose()


async def test_receipt_api_authentication_and_conversation_scope(tmp_path):
    built=await _seeded(tmp_path)
    try:
        current=await conversation(built)
        other=await conversation(built,other=True)
        first=await append(built.host.event_store,0,conversation_id=current.id)
        await append(built.host.event_store,0,conversation_id=other.id)
        await append(built.host.event_store,1,conversation_id=other.id,name='user.message.failed')
        path=f'/api/conversations/{current.id}/messages/receipt'
        async with client(built.app,token=None) as anonymous:
            assert (await anonymous.get(path,params={'clientRef':'same-ref'})).status_code==401
            assert (await anonymous.get(path,params={'clientRef':'same-ref','token':TEST_TOKEN})).status_code==401
        async with client(built.app,token='wrong-token') as wrong:
            assert (await wrong.get(path,params={'clientRef':'same-ref'})).status_code==401
        async with client(built.app) as api:
            response=await api.get(path,params={'clientRef':'same-ref'})
            assert response.status_code==200,response.text
            payload=response.json()
            assert payload['status']=='recorded' and payload['conversationId']==current.id
            assert payload['clientRef']=='same-ref'
            assert [e['eventId'] for e in payload['events']]==[first.event_id]
            unknown=(await api.get(path,params={'clientRef':'missing'})).json()
            assert unknown['status']=='unknown' and unknown['events']==[]
            assert (await api.get('/api/conversations/'+CONVERSATION+'/messages/receipt',
                params={'clientRef':'same-ref'})).status_code==404
            assert (await api.get(path)).status_code==422
            for invalid in ('','x'*65):
                assert (await api.get(path,params={'clientRef':invalid})).status_code==422
    finally:
        await built.aclose()


async def test_receipt_survives_restart_and_latest_same_reference_retry(tmp_path):
    built=await _seeded(tmp_path)
    current=await conversation(built)
    await append(built.host.event_store,0,conversation_id=current.id)
    failure=await append(built.host.event_store,1,conversation_id=current.id,name='user.message.failed')
    await built.aclose()
    restarted=Harness(tmp_path)
    try:
        path=f'/api/conversations/{current.id}/messages/receipt'
        async with client(restarted.app) as api:
            failed=(await api.get(path,params={'clientRef':'same-ref'})).json()
            assert failed['status']=='failed'
            assert len(failed['events'])==2 and failed['events'][-1]['eventId']==failure.event_id
            retried=await append(restarted.host.event_store,2,conversation_id=current.id)
            recorded=(await api.get(path,params={'clientRef':'same-ref'})).json()
            assert recorded['status']=='recorded'
            assert [e['eventId'] for e in recorded['events']]==[retried.event_id]
    finally:
        await restarted.aclose()
