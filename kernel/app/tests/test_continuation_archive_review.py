"""Independent synthetic archive -> handoff -> model-input boundary checks."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlparse

from app.tests.test_continuations import setup  # noqa: F401 — shared isolated sqlite fixture
from drivers.base import MessageInput
from drivers.acp.driver import _prompt_blocks
from drivers.archive_sources import scan
from native_archives import NativeArchiveService, NativeArchiveStore

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=')


def test_archive_preview_freezes_body_and_image_then_survives_source_removal(setup):
    async def check():
        service,uow,source,target,_=setup
        folder=service.root.parent/'native-fixture';folder.mkdir()
        path=folder/'native-session.jsonl'
        rows=[{'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Original archived constraint'}]}},
              {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Original archived answer'},{'type':'output_image','image_url':'data:image/png;base64,'+base64.b64encode(PNG).decode()}]}}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        store=NativeArchiveStore(service.root.parent/'owned-archive');items=scan('codex',folder);store.remember_sources(items)
        estimate=store.preview([items[0]['sourceId']],source.project_id)
        archive=store.import_preview(estimate['previewToken'],source.project_id)['archiveIds'][0]
        service.archives=NativeArchiveService(store,uow.repositories)
        driver=SimpleNamespace(get_capabilities=AsyncMock(return_value=SimpleNamespace(card=SimpleNamespace(attachments=SimpleNamespace(value='images')))))
        service.registry.try_get=lambda key:driver
        preview=await service.preview({'kind':'archive','id':archive,'projectId':source.project_id},target.id)
        assert preview['assets'][0]['available'] and preview['assets'][0]['compatible']
        # The reviewed handoff is independent of the source after snapshotting.
        path.unlink();store.trash(archive,source.project_id)
        created=await service.create(preview['token'],'Reviewed handoff',[preview['assets'][0]['id']])
        record=await service.detail(created['conversationId'])
        transcript=''.join(Path(unquote(urlparse(f['ref']).path)).read_text() for f in record['files'])
        assert 'Original archived constraint' in transcript and 'Original archived answer' in transcript
        prepared=service.attachments.prepare(created['conversationId'],None,record['attachments'])
        blocks=_prompt_blocks(MessageInput(text=record['draft'],attachments=prepared),{'image':True,'embeddedContext':False})
        image=next(block for block in blocks if block['type']=='image')
        assert base64.b64decode(image['data'])==PNG and image['mimeType']=='image/png'
        assert await service.create(preview['token'],'Reviewed handoff',[preview['assets'][0]['id']])==created
    asyncio.run(check())


def test_continuation_api_requires_auth_and_keeps_creation_idempotent(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.continuations import build_continuation_router
    from app.api.session_auth import SessionAuthPolicy
    service,_,source,target,_=setup
    app=FastAPI();router=build_continuation_router(service,auth_policy=SessionAuthPolicy(lambda:'review-token'))
    app.router.routes.extend(router.routes)
    client=TestClient(app,base_url='http://127.0.0.1:8877')
    body={'source':{'kind':'conversation','id':source.id,'projectId':source.project_id},'bindingId':target.id}
    assert client.post('/api/continuations/preview',json=body).status_code==401
    headers={'Authorization':'Bearer review-token'}
    preview=client.post('/api/continuations/preview',json=body,headers=headers)
    assert preview.status_code==200,preview.text
    request={'token':preview.json()['token'],'context':'Accepted review','assetIds':[]}
    created=client.post('/api/continuations',json=request,headers=headers)
    assert created.status_code==200,created.text
    again=client.post('/api/continuations',json=request,headers=headers)
    assert again.json()==created.json()
    detail=client.get('/api/continuations/'+created.json()['conversationId'],headers=headers)
    assert detail.status_code==200 and 'Accepted review' in detail.json()['continuation']['draft']


def test_preview_uses_upload_contract_for_archive_assets(setup):
    """Files accepted by archive storage need not be transferable prompt inputs."""
    async def check():
        service,_,source,target,_=setup
        samples=[('unsupported.bin',b'opaque'),('empty.txt',b''),('文'*61+'.txt',b'long name'),('invalid.txt',b'\xff'),('valid.md',b'valid')]
        assets=[{'hash':str(i),'name':name,'mime':'text/plain','size':len(data)} for i,(name,data) in enumerate(samples)]
        service.archives=SimpleNamespace(detail=AsyncMock(return_value={'archive':{'title':'Archive','revision':'r'},'assets':assets}),
            store=SimpleNamespace(visible_messages=lambda *args:iter([{'role':'user','text':'Continue'}]),
                asset_bytes=lambda _id,_project,key,_revision:(samples[int(key)][1],assets[int(key)])))
        service.registry.try_get=lambda _:SimpleNamespace(get_capabilities=AsyncMock(return_value=SimpleNamespace(card=SimpleNamespace(attachments=SimpleNamespace(value='files')))))
        preview=await service.preview({'kind':'archive','id':'fixture','projectId':source.project_id},target.id)
        assert [a['compatible'] for a in preview['assets']]==[False,False,False,False,True]
        assert all(a['available'] for a in preview['assets'])
        created=await service.create(preview['token'],preview['context'],[preview['assets'][-1]['id']])
        assert (await service.detail(created['conversationId']))['attachments'][0]['name']=='valid.md'
    asyncio.run(check())


def test_authorized_inline_image_becomes_owned_model_input(setup):
    from runtime.event_reducer import ArtifactItem, TimelineState
    from runtime.event_envelope import ArtifactRef
    async def check():
        service,_,source,target,state=setup
        artifact=ArtifactItem(item_id='image:1',order=8,last_sequence=8,artifact=ArtifactRef(
            artifact_id='image:1',kind='image',title='Generated reference',
            uri='data:image/png;base64,'+base64.b64encode(PNG).decode(),mime_type='image/png'))
        service.host.timeline_from_store=AsyncMock(side_effect=lambda identity:state.model_copy(update={'items':(*state.items,artifact)}) if identity==source.id else TimelineState(conversation_id=identity))
        service.registry.try_get=lambda _:SimpleNamespace(get_capabilities=AsyncMock(return_value=SimpleNamespace(card=SimpleNamespace(attachments=SimpleNamespace(value='images')))))
        preview=await service.preview({'kind':'conversation','id':source.id,'projectId':source.project_id},target.id)
        assert preview['assets'][0]['compatible'] and preview['assets'][0]['name']=='Generated reference.png'
        created=await service.create(preview['token'],preview['context'],[preview['assets'][0]['id']])
        record=await service.detail(created['conversationId'])
        prepared=service.attachments.prepare(created['conversationId'],None,record['attachments'])
        blocks=_prompt_blocks(MessageInput(text=record['draft'],attachments=prepared),{'image':True})
        assert base64.b64decode(next(b for b in blocks if b['type']=='image')['data'])==PNG
    asyncio.run(check())


def test_inline_image_size_checked_before_decoding(monkeypatch):
    import pytest
    from app.api.api_errors import ApiError
    import app.conversations.continuation as module
    monkeypatch.setattr(module,'MAX_UPLOAD_BYTES',3)
    decode=__import__('unittest.mock',fromlist=['Mock']).Mock(side_effect=AssertionError('must not decode'))
    monkeypatch.setattr(module.base64,'b64decode',decode)
    with pytest.raises(ApiError,match='10 MB'):
        module._inline_image('data:image/png;base64,'+'a'*8,'image')
    decode.assert_not_called()


def test_create_failed_save_cleans_uploads_and_retry_is_not_duplicated(setup,monkeypatch):
    import pytest
    import uuid
    async def check():
        service,uow,source,target,_=setup
        preview=await service.preview({'kind':'conversation','id':source.id,'projectId':source.project_id},target.id)
        identity='conversation:'+str(uuid.uuid5(uuid.NAMESPACE_URL,'kaus:continuation:'+preview['token']))
        folder=service.attachments.conversation_dir(identity)
        folder.mkdir(parents=True);(folder/'interrupted-attempt.txt').write_text('stale')
        save=uow.repositories.conversations.save
        async def fail(conversation):
            assert len(list(folder.iterdir()))==1
            raise OSError('simulated sqlite write failure')
        monkeypatch.setattr(uow.repositories.conversations,'save',fail)
        with pytest.raises(OSError,match='simulated'):
            await service.create(preview['token'],'Reviewed')
        assert await uow.repositories.conversations.get(identity) is None
        assert not folder.exists()
        assert (service.root/(preview['token']+'.json')).exists()
        monkeypatch.setattr(uow.repositories.conversations,'save',save)
        created=await service.create(preview['token'],'Reviewed')
        assert created['conversationId']==identity
        record=await service.detail(identity)
        assert len(record['files'])==1 and len(list(folder.iterdir()))==1
        assert await service.create(preview['token'],'Reviewed')==created
        assert len(list(folder.iterdir()))==1
    asyncio.run(check())


def test_committed_creation_survives_cleanup_or_lost_save_response(setup,monkeypatch):
    async def check():
        service,uow,source,target,_=setup
        preview=await service.preview({'kind':'conversation','id':source.id,'projectId':source.project_id},target.id)
        save=uow.repositories.conversations.save
        async def committed_then_failed(conversation):
            await save(conversation)
            raise OSError('response lost after commit')
        monkeypatch.setattr(uow.repositories.conversations,'save',committed_then_failed)
        unlink=Path.unlink
        def cleanup_failed(path,*args,**kwargs):
            if path==service.root/(preview['token']+'.json'):raise OSError('cleanup unavailable')
            return unlink(path,*args,**kwargs)
        monkeypatch.setattr(Path,'unlink',cleanup_failed)
        created=await service.create(preview['token'],'Reviewed')
        record=await service.detail(created['conversationId'])
        assert service.attachments.prepare(created['conversationId'],None,record['files'])
        assert await service.create(preview['token'],'Reviewed')==created
    asyncio.run(check())
