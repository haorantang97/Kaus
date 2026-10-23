"""Synthetic native formats; never read real account histories."""
import asyncio
import base64
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'kernel'))
from native_archives import NativeArchiveStore, NativeArchiveService, ArchiveError, MARKER
from drivers.archive_sources import scan
from app.persistence.memory import in_memory_repository_set
from app.projects.models import Project


def jsonl(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path


def codex(text='question'):
    return [{'type':'session_meta','payload':{'id':'session-native'}},
        {'type':'event_msg','payload':{'type':'user_message','message':text}},
        {'type':'response_item','timestamp':'2026-10-16T01:00:00Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'},{'type':'output_image','image_url':'data:image/png;base64,'+base64.b64encode(b'fixture-image').decode()}]}}]


def setup(tmp_path, source='codex', rows=None):
    store=NativeArchiveStore(tmp_path/'archive')
    path=jsonl(tmp_path/'native'/'session.jsonl',rows or codex())
    found=scan(source,path.parent)
    store.remember_sources(found)
    return store,path,found[0]['sourceId']


def test_owned_copy_survives_source_removal_and_is_project_scoped(tmp_path):
    store,path,key=setup(tmp_path)
    before=path.read_bytes()
    preview=store.preview([key],'project:first')
    assert preview['messageCount']==2 and preview['assetCount']==1
    assert preview['newBytes']>0 and preview['rawBytes']>0
    result=store.import_preview(preview['previewToken'],'project:first')
    archive=result['archiveIds'][0]
    assert path.read_bytes()==before
    path.unlink()
    detail=store.detail(archive,'project:first')
    assert [m['text'] for m in detail['messages']]==['question','answer']
    asset=detail['assets'][0]
    assert store.asset(archive,'project:first',asset['hash'])[0].read_bytes()==b'fixture-image'
    with pytest.raises(ArchiveError,match='not_found'): store.asset(archive,'project:other',asset['hash'])
    stored=list(store.records(archive))[-1]
    assert MARKER in stored['payload']['content'][1]['image_url']
    assert store.decode_record(stored)==codex()[-1]


def test_repeat_and_multiple_project_links_add_no_blob_bytes(tmp_path):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    size=sum(p.stat().st_size for p in store.objects.rglob('*') if p.is_file())
    again=store.preview([key],'project:second')
    assert again['newBytes']==0 and again['dedupBytes']>0
    store.import_preview(again['previewToken'],'project:second')
    assert sum(p.stat().st_size for p in store.objects.rglob('*') if p.is_file())==size
    assert store.list()[0]['projectIds']==['project:first','project:second']
    assert store.detail(archive,'project:second')['messages']
    assert not [p for p in store.staging.rglob('*') if p.is_file() and p.name!='plan.json']


def test_append_and_edited_prefix_create_revisions_without_duplicate_objects(tmp_path):
    store,path,key=setup(tmp_path)
    first=store.preview([key],'project:first')
    archive=store.import_preview(first['previewToken'],'project:first')['archiveIds'][0]
    appended={'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'next'}]}}
    with path.open('a') as f: f.write(json.dumps(appended)+'\n')
    store.remember_sources(scan('codex',path.parent))
    second=store.preview([key],'project:first')
    assert second['messageCount']==3 and second['dedupBytes']>second['newBytes']
    store.import_preview(second['previewToken'],'project:first')
    assert len(list(store.records(archive,first['archives'][0]['revision'])))==4
    jsonl(path,codex('edited'))
    store.remember_sources(scan('codex',path.parent))
    third=store.preview([key],'project:first')
    store.import_preview(third['previewToken'],'project:first')
    assert store.detail(archive,'project:first')['messages'][0]['text']=='edited'
    with store.db() as db: assert db.execute('SELECT count(*) FROM revisions').fetchone()[0]==3


def test_changed_source_and_corrupt_line_do_not_publish_partial_import(tmp_path):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    with path.open('a') as f:f.write('{"unfinished":')
    with pytest.raises(ArchiveError,match='source_changed'):store.import_preview(preview['previewToken'],'project:first')
    assert store.list()==[]
    store.remember_sources(scan('codex',path.parent))
    with pytest.raises(ValueError,match='record_corrupt'):store.preview([key],'project:first')
    assert store.list()==[]


def test_claude_text_image_and_local_attachment_are_owned(tmp_path):
    root=tmp_path/'native';root.mkdir()
    (root/'result.txt').write_text('file-output')
    rows=[{'type':'user','sessionId':'cc-1','message':{'role':'user','content':'hello'}},
        {'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'[Result](result.txt)'},
            {'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(b'fixture-image').decode()}}]}}]
    store,path,key=setup(tmp_path,'claude-code',rows)
    preview=store.preview([key],'project:first')
    assert preview['messageCount']==2 and preview['assetCount']==2
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    (root/'result.txt').unlink();path.unlink()
    blobs=[store.asset(archive,'project:first',a['hash'])[0].read_bytes() for a in store.detail(archive,'project:first')['assets']]
    assert b'file-output' in blobs and b'fixture-image' in blobs


def test_hermes_sqlite_is_opened_readonly_and_selected_session_only(tmp_path):
    root=tmp_path/'hermes';root.mkdir();path=root/'state.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT,title TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.executemany('INSERT INTO sessions VALUES (?,?)',[('one','Chosen'),('two','Other')])
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?)',[(1,'one','user','hello',None),(2,'one','assistant','done','[]'),(3,'two','user','not-selected',None)])
    before=path.read_bytes()
    found=scan('hermes',root)
    store=NativeArchiveStore(tmp_path/'archive');store.remember_sources(found)
    chosen=next(row for row in found if row['nativeId']=='one')
    p=store.preview([chosen['sourceId']],'project:first')
    archive=store.import_preview(p['previewToken'],'project:first')['archiveIds'][0]
    assert path.read_bytes()==before
    assert [m['text'] for m in store.detail(archive,'project:first')['messages']]==['hello','done']
    assert b'not-selected' not in b''.join(p.read_bytes() for p in store.objects.rglob('*') if p.is_file())


def test_backup_restore_merges_without_overwriting_current_revision(tmp_path):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    backup=tmp_path/'backup.zip';store.backup([archive],'project:first',backup)
    jsonl(path,codex('newer'));store.remember_sources(scan('codex',path.parent))
    p=store.preview([key],'project:first');store.import_preview(p['previewToken'],'project:first')
    store.recover(backup,'project:second')
    assert store.detail(archive,'project:first')['messages'][0]['text']=='newer'
    restored=NativeArchiveStore(tmp_path/'restored')
    restored.recover(backup,'project:new')
    path.unlink()
    assert restored.detail(archive,'project:new')['messages'][0]['text']=='question'
    assets=restored.detail(archive,'project:new')['assets']
    assert restored.asset(archive,'project:new',assets[0]['hash'])[0].read_bytes()==b'fixture-image'
    restored.recover(backup,'project:new')
    assert len(restored.list())==1


def test_recovery_rejects_zip_slip_corruption_and_symlinks_before_publish(tmp_path):
    store,path,key=setup(tmp_path)
    p=store.preview([key],'project:first');archive=store.import_preview(p['previewToken'],'project:first')['archiveIds'][0]
    good=tmp_path/'good.zip';store.backup([archive],'project:first',good)
    with zipfile.ZipFile(good) as original: contents={n:original.read(n) for n in original.namelist()}
    destination=NativeArchiveStore(tmp_path/'restored')
    for kind in ('traversal','corrupt','symlink'):
        target=tmp_path/(kind+'.zip')
        with zipfile.ZipFile(target,'w') as bundle:
            for name,data in contents.items():
                bundle.writestr(name,b'broken' if kind=='corrupt' and name.startswith('objects/') else data)
            if kind=='traversal':bundle.writestr('../escaped','bad')
            if kind=='symlink':
                entry=zipfile.ZipInfo('objects/'+'f'*64);entry.external_attr=0o120777<<16;bundle.writestr(entry,'/tmp/target')
        with pytest.raises(ArchiveError): destination.recover(target,'project:new')
        assert destination.list()==[]
    assert not (tmp_path/'escaped').exists()


def test_interrupted_commit_retry_and_trash_restore(tmp_path,monkeypatch):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    original=store._validate_object;calls=0
    def fail(data,desc):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('simulated disk interruption')
        return original(data,desc)
    monkeypatch.setattr(store,'_validate_object',fail)
    with pytest.raises(OSError):store.import_preview(preview['previewToken'],'project:first')
    assert store.list()==[]
    monkeypatch.setattr(store,'_validate_object',original)
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    store.trash(archive,'project:first')
    assert store.list()==[] and len(store.list(trash=True))==1
    store.trash(archive,'project:first',restore=True)
    assert store.detail(archive,'project:first')['messages']


def test_api_requires_token_and_project_and_scanned_ids(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.native_archives import build_native_archive_router
    from app.api.session_auth import SessionAuthPolicy
    repos=in_memory_repository_set()
    project=Project.create(slug='archive-test',display_name='Archive test')
    asyncio.run(repos.projects.save(project))
    store,path,key=setup(tmp_path)
    service=NativeArchiveService(store,repos,[{'id':'codex','label':'Codex','path':str(path.parent)}])
    app=FastAPI();router=build_native_archive_router(service,auth_policy=SessionAuthPolicy(lambda:'fixture-token'))
    app.router.routes.extend(router.routes)
    client=TestClient(app,base_url='http://127.0.0.1:8877')
    assert client.get('/api/native-archives').status_code==401
    headers={'Authorization':'Bearer fixture-token'}
    assert client.get('/api/native-archives/sources',headers=headers).status_code==200
    assert client.post('/api/native-archives/preview',headers=headers,json={'projectId':project.id,'sourceIds':['not-scanned']}).status_code==400
    assert client.post('/api/native-archives/preview',headers=headers,json={'projectId':'project:missing','sourceIds':[key]}).status_code==404
    response=client.post('/api/native-archives/preview',headers=headers,json={'projectId':project.id,'sourceIds':[key]})
    assert response.status_code==200,response.text
    result=client.post('/api/native-archives/import',headers=headers,json={'projectId':project.id,'previewToken':response.json()['previewToken']})
    assert result.status_code==200,result.text


def test_backup_restores_current_revision_and_all_earlier_records(tmp_path):
    store,path,key=setup(tmp_path)
    first=store.preview([key],'project:first')
    archive=store.import_preview(first['previewToken'],'project:first')['archiveIds'][0]
    jsonl(path,codex('edited'))
    store.remember_sources(scan('codex',path.parent))
    latest=store.preview([key],'project:first')
    store.import_preview(latest['previewToken'],'project:first')
    backup=tmp_path/'revisions.zip';store.backup([archive],'project:first',backup)
    restored=NativeArchiveStore(tmp_path/'fresh')
    result=restored.recover(backup,'project:new')
    assert result['count']==1
    assert restored.detail(archive,'project:new')['messages'][0]['text']=='edited'
    assert len(list(restored.records(archive,first['archives'][0]['revision'])))==4
    assert restored.latest(archive)['revision']==latest['archives'][0]['revision']


def test_single_pass_visible_records_and_verified_frozen_assets(tmp_path):
    store,path,key=setup(tmp_path)
    p=store.preview([key],'project:first')
    archive=store.import_preview(p['previewToken'],'project:first')['archiveIds'][0]
    revision=p['archives'][0]['revision'];asset=store.latest(archive)['assets'][0]
    assert [m['text'] for m in store.visible_messages(archive,'project:first',revision)]==['question','answer']
    raw=store.raw_page(archive,'project:first',1,2)
    assert raw['records'][0]['type']=='event_msg' and raw['nextOffset']==3
    assert store.asset_bytes(archive,'project:first',asset['hash'],revision)[0]==b'fixture-image'
    store.object_path(asset['hash']).write_bytes(b'corrupt')
    with pytest.raises(ArchiveError,match='object_corrupt'):store.asset_bytes(archive,'project:first',asset['hash'],revision)


def test_append_keeps_archived_asset_when_original_was_removed(tmp_path):
    file=tmp_path/'native'/'attachment.txt';file.parent.mkdir();file.write_text('owned contents')
    store,path,key=setup(tmp_path,rows=codex(f'[local]({file})'))
    p=store.preview([key],'project:first');archive=store.import_preview(p['previewToken'],'project:first')['archiveIds'][0]
    asset=next(a for a in store.latest(archive)['assets'] if a['name']=='attachment.txt')
    file.unlink()
    with path.open('a') as stream:stream.write(json.dumps(codex('next')[2])+'\n')
    store.remember_sources(scan('codex',path.parent))
    p=store.preview([key],'project:first');store.import_preview(p['previewToken'],'project:first')
    assert store.asset_bytes(archive,'project:first',asset['hash'])[0]==b'owned contents'
    assert len(store.detail(archive,'project:first')['messages'])==3


def test_metadata_discovery_does_not_read_bodies_and_uses_native_titles(tmp_path):
    native=tmp_path/'codex';source=jsonl(native/'sessions'/'rollout-00000000-0000-0000-0000-000000000001.jsonl',[{'opaque':'not read'}])
    with sqlite3.connect(native/'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT,title TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)',('00000000-0000-0000-0000-000000000001','Indexed title'))
    source.write_text('invalid transcript body')
    assert scan('codex',native)[0]['title']=='Indexed title'
    other=tmp_path/'claude';jsonl(other/'p'/'00000000-0000-0000-0000-000000000002.jsonl',[])
    (other/'p'/'sessions-index.json').write_text(json.dumps({'entries':[{'sessionId':'00000000-0000-0000-0000-000000000002','summary':'Indexed summary'}]}))
    assert scan('claude-code',other)[0]['title']=='Indexed summary'


def test_unchanged_preview_has_zero_incremental_space_and_record_cap_is_explicit(tmp_path,monkeypatch):
    store,path,key=setup(tmp_path)
    first=store.preview([key],'project:first');store.import_preview(first['previewToken'],'project:first')
    again=store.preview([key],'project:first')
    assert again['newBytes']==0 and again['indexEstimateBytes']==0
    from drivers.archive_sources import jsonl as parser
    monkeypatch.setattr(parser,'MAX_RECORD_BYTES',32)
    store2=NativeArchiveStore(tmp_path/'limited');store2.remember_sources(scan('codex',path.parent))
    with pytest.raises(ValueError,match='too_large'):store2.preview([key],'project:first')
    assert store2.list()==[] and not list(store2.staging.iterdir())


def test_project_unlink_never_deletes_owned_content_and_can_reassociate(tmp_path):
    store,path,key=setup(tmp_path)
    first=store.preview([key],'project:first');archive=store.import_preview(first['previewToken'],'project:first')['archiveIds'][0]
    store.unlink_project('project:first')
    assert store.list()[0]['projectIds']==[]
    assert list(store.records(archive))
    with pytest.raises(ArchiveError,match='not_found'):store.detail(archive,'project:first')
    store.link(archive,'project:new')
    assert store.detail(archive,'project:new')['messages'][0]['text']=='question'


def test_deleted_project_is_reconciled_when_opening_library(tmp_path):
    async def check():
        repos=in_memory_repository_set();project=Project.create(slug='old',display_name='Old')
        await repos.projects.save(project)
        store,path,key=setup(tmp_path)
        preview=store.preview([key],project.id)
        archive=store.import_preview(preview['previewToken'],project.id)['archiveIds'][0]
        service=NativeArchiveService(store,repos)
        assert (await service.list())[0]['projectIds']==[project.id]
        await repos.projects.delete(project.id)
        assert (await service.list())[0]['projectIds']==[]
        assert list(store.records(archive))
    asyncio.run(check())


def test_orphaned_trash_restores_to_valid_project_with_owned_content_and_backup(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.native_archives import build_native_archive_router
    from app.api.session_auth import SessionAuthPolicy
    repos=in_memory_repository_set()
    old=Project.create(slug='old',display_name='Old')
    new=Project.create(slug='new',display_name='New')
    for project in (old,new): asyncio.run(repos.projects.save(project))
    store,path,key=setup(tmp_path)
    preview=store.preview([key],old.id)
    archive=store.import_preview(preview['previewToken'],old.id)['archiveIds'][0]
    service=NativeArchiveService(store,repos,[])
    app=FastAPI()
    app.router.routes.extend(build_native_archive_router(service,auth_policy=SessionAuthPolicy(lambda:'fixture-token')).routes)
    client=TestClient(app,base_url='http://127.0.0.1:8877')
    headers={'Authorization':'Bearer fixture-token'}
    base='/api/native-archives/'+archive
    assert client.post(base+'/trash',headers=headers,json={'projectId':old.id}).status_code==200
    asyncio.run(repos.projects.delete(old.id))
    assert client.get('/api/native-archives?trash=true',headers=headers).json()['archives'][0]['projectIds']==[]
    path.unlink()
    # Native account/config changes cannot affect owned archive reads or recovery.
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'another-account'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(tmp_path/'another-claude-account'))
    assert client.post(base+'/restore',json={'projectId':new.id}).status_code==401
    assert client.post(base+'/restore',headers={'Authorization':'Bearer wrong-token'},json={'projectId':new.id}).status_code==401
    assert client.post(base+'/restore',headers=headers,json={'projectId':old.id}).status_code==404
    assert client.post(base+'/restore',headers=headers,json={'projectId':'project:missing'}).status_code==404
    assert store.list(trash=True)[0]['projectIds']==[]
    assert client.post('/api/native-archives/missing/restore',headers=headers,json={'projectId':new.id}).status_code==404
    for _ in range(2):
        assert client.post(base+'/restore',headers=headers,json={'projectId':new.id}).status_code==200
    assert store.list()[0]['projectIds']==[new.id]
    detail=client.get(base,headers=headers,params={'projectId':new.id}).json()
    assert [m['text'] for m in detail['messages']]==['question','answer']
    asset=detail['assets'][0]
    assert client.get(base+'/assets/'+asset['hash'],headers=headers,params={'projectId':new.id}).content==b'fixture-image'
    stranger=Project.create(slug='stranger',display_name='Unlinked')
    asyncio.run(repos.projects.save(stranger))
    assert client.get(base,headers=headers,params={'projectId':stranger.id}).status_code==404
    backup=tmp_path/'owned.zip'
    store.backup([archive],new.id,backup)
    restored=NativeArchiveStore(tmp_path/'separate-installation')
    restored.recover(backup,stranger.id)
    assert [m['text'] for m in restored.detail(archive,stranger.id)['messages']]==['question','answer']
    assert restored.asset_bytes(archive,stranger.id,asset['hash'])[0]==b'fixture-image'


def test_restore_link_and_visibility_are_atomic_and_preserve_other_projects(tmp_path):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    store.link(archive,'project:second')
    store.trash(archive,'project:first')
    with store.db() as db:
        db.execute("CREATE TRIGGER reject_restore BEFORE UPDATE OF deleted ON archives WHEN NEW.deleted IS NULL BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
    with pytest.raises(sqlite3.IntegrityError,match='simulated interruption'):
        store.restore_to_project(archive,'project:third')
    assert store.list()==[]
    assert set(store.list(trash=True)[0]['projectIds'])=={'project:first','project:second'}
    with store.db() as db: db.execute('DROP TRIGGER reject_restore')
    store.restore_to_project(archive,'project:third')
    assert set(store.list()[0]['projectIds'])=={'project:first','project:second','project:third'}
    for project in ('project:first','project:second','project:third'):
        assert store.detail(archive,project)['messages'][0]['text']=='question'


def test_hermes_serialized_multimodal_content_is_lossless_owned_and_deduplicated(tmp_path):
    from drivers.archive_sources.serialized_json import JSON_MARKER
    root=tmp_path/'native';root.mkdir()
    local=root/'local.png';local.write_bytes(b'local image contents')
    image=b'inline picture\xff\xff\xff'
    data=base64.b64encode(image).decode()
    content=' \n'+json.dumps([
        {'type':'text','text':'照片与附件'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+data}},
        {'type':'image_url','image_url':{'url':str(local)}},
    ],ensure_ascii=False,indent=2).replace('/',r'\/')+'\n'
    path=root/'state.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT,title TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)',('multimodal','Selected'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?)',(1,'multimodal','user',content))
    original=path.read_bytes()
    store=NativeArchiveStore(tmp_path/'archive')
    rows=scan('hermes',root);store.remember_sources(rows)
    preview=store.preview([rows[0]['sourceId']],'project:first')
    assert preview['assetCount']==2 and not preview['warnings']
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    stored=list(store.records(archive))[1]
    assert JSON_MARKER in stored['native']['content']
    assert data not in json.dumps(stored)
    assert store.decode_record(stored)['native']['content']==content
    assert path.read_bytes()==original
    size=sum(p.stat().st_size for p in store.objects.rglob('*') if p.is_file())
    again=store.preview([rows[0]['sourceId']],'project:second')
    assert again['newBytes']==0
    store.import_preview(again['previewToken'],'project:second')
    assert sum(p.stat().st_size for p in store.objects.rglob('*') if p.is_file())==size
    path.unlink();local.unlink()
    detail=store.detail(archive,'project:second')
    assert detail['messages'][0]['text']=='照片与附件'
    assert list(store.visible_messages(archive,'project:second'))[0]['text']=='照片与附件'
    assert {store.asset_bytes(archive,'project:second',a['hash'])[0] for a in detail['assets']}=={image,b'local image contents'}
    backup=tmp_path/'archive.zip';store.backup([archive],'project:second',backup)
    restored=NativeArchiveStore(tmp_path/'restored');restored.recover(backup,'project:new')
    assert restored.decode_record(list(restored.records(archive))[1])['native']['content']==content
    assert restored.detail(archive,'project:new')['messages'][0]['text']=='照片与附件'
    assert {restored.asset_bytes(archive,'project:new',a['hash'])[0] for a in restored.detail(archive,'project:new')['assets']}=={image,b'local image contents'}


def test_hermes_serialized_local_reference_without_inline_data_stays_native(tmp_path):
    root=tmp_path/'native';root.mkdir()
    local=root/'report.txt';local.write_text('local contents')
    content='  '+json.dumps([{'type':'text','text':'[Report](report.txt)'}])
    path=root/'state.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT,title TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)',('local','Selected'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?)',(1,'local','user',content))
    store=NativeArchiveStore(tmp_path/'archive');rows=scan('hermes',root);store.remember_sources(rows)
    preview=store.preview([rows[0]['sourceId']],'project:first')
    assert preview['assetCount']==1
    archive=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    assert list(store.records(archive))[1]['native']['content']==content
    path.unlink();local.unlink()
    detail=store.detail(archive,'project:first')
    assert detail['messages'][0]['text']=='[Report](report.txt)'
    assert store.asset_bytes(archive,'project:first',detail['assets'][0]['hash'])[0]==b'local contents'
