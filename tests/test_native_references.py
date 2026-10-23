"""Synthetic reference lifecycle; native data and ownership stay outside Kaus."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'kernel'))
from native_archives import NativeArchiveStore, NativeArchiveService, ArchiveError
from drivers.archive_sources import scan
from test_native_archives import setup, jsonl, codex


def reference(tmp_path, rows=None, source='codex', workspace=None):
    store,path,key=setup(tmp_path,source,rows)
    preview=store.preview([key],'project:first',workspace,storage='reference')
    identity=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    return store,path,identity,preview


def test_reference_stores_only_mapping_even_after_all_read_paths(tmp_path):
    body='SYNTHETIC-PRIVATE-BODY-NEVER-PERSIST'
    store,path,identity,preview=reference(tmp_path,codex(body))
    assert preview['newBytes']==preview['rawBytes']==preview['compressedBytes']==0
    page=store.detail(identity,'project:first')
    assert page['messages'][0]['text']==body
    assert page['archive']['storage']=='reference' and page['archive']['availability']=='available'
    assert store.raw_page(identity,'project:first')['total']==4
    assert list(store.records(identity))==codex(body)
    assert store.decode_record(list(store.records(identity))[-1])==codex(body)[-1]
    assert store.asset_bytes(identity,'project:first',page['assets'][0]['hash'])[0]==b'fixture-image'
    assert store.latest(identity)['messageCount']==2
    assert store.list()[0]['sourcePath']==str(path)
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM objects').fetchone()[0]==0
        payload=json.loads(db.execute('SELECT payload FROM revisions').fetchone()[0])
        assert payload['records']==payload['assets']==[]
    assert not any(p.is_file() for p in store.objects.rglob('*'))
    for file in store.root.rglob('*'):
        if file.is_file():
            data=file.read_bytes()
            assert body.encode() not in data and b'fixture-image' not in data
            assert base64.b64encode(b'fixture-image') not in data


def test_append_rewrite_and_missing_source_follow_native_without_unlinking(tmp_path):
    store,path,identity,_=reference(tmp_path)
    old=store.detail(identity,'project:first')['archive']['revision']
    extra={'type':'response_item','payload':{'type':'message','role':'assistant','content':'appended'}}
    with path.open('a') as output: output.write(json.dumps(extra)+'\n')
    assert [m['text'] for m in store.detail(identity,'project:first')['messages']]==['question','answer','appended']
    with pytest.raises(ArchiveError,match='source_changed'):list(store.records(identity,old))
    jsonl(path,codex('rewritten'))
    assert store.detail(identity,'project:first')['messages'][0]['text']=='rewritten'
    store.link(identity,'project:second')
    path.unlink()
    assert store.list('project:second')[0]['availability']=='missing'
    missing=store.detail(identity,'project:first')
    assert missing['archive']['availability']=='missing' and missing['messages']==missing['assets']==[]
    assert set(store.list()[0]['projectIds'])=={'project:first','project:second'}
    with pytest.raises(ArchiveError,match='source_missing'):list(store.records(identity))
    with pytest.raises(ArchiveError,match='source_missing'):store.raw_page(identity,'project:first')
    with pytest.raises(ArchiveError,match='source_missing'):store.archive(identity,'project:first')


def test_reference_relink_across_projects_has_one_identity_and_no_payload(tmp_path):
    store,path,identity,_=reference(tmp_path)
    source=scan('codex',path.parent)[0]
    store.remember_sources([source])
    preview=store.preview([source['sourceId']],'project:second',storage='reference')
    assert store.import_preview(preview['previewToken'],'project:second')['archiveIds']==[identity]
    assert len(store.list())==1 and store.detail(identity,'project:second')['messages']
    with pytest.raises(ArchiveError,match='not_found'):store.detail(identity,'project:other')
    key=store.detail(identity,'project:first')['assets'][0]['hash']
    with pytest.raises(ArchiveError,match='not_found'):store.asset_bytes(identity,'project:other',key)
    with pytest.raises(ArchiveError,match='invalid_hash'):store.asset_bytes(identity,'project:first','../private')


def test_list_only_probes_mapping_metadata_without_opening_transcripts(tmp_path,monkeypatch):
    import native_archives
    store,path,identity,_=reference(tmp_path)
    def unexpected(*args,**kwargs):raise AssertionError('listing must not read transcript bodies or assets')
    monkeypatch.setattr(store,'_read_reference',unexpected)
    monkeypatch.setattr(native_archives,'records',unexpected)
    initial=store.list()[0]
    assert initial['id']==identity and initial['messageCount']==2 and initial['assetCount']==1
    assert initial['countsCurrent'] is True
    with path.open('a') as output:output.write(json.dumps({'type':'metadata','value':'new'})+'\n')
    assert store.list()[0]['countsCurrent'] is False


def test_bounded_probe_does_not_mark_large_valid_native_header_missing(tmp_path):
    rows=codex()
    rows[0]['payload']['base_instructions']='synthetic instructions '*6000
    store,path,identity,_=reference(tmp_path,rows)
    assert len(path.read_bytes().splitlines()[0])>64*1024
    assert store.list()[0]['availability']=='available'
    assert store.detail(identity,'project:first')['messages'][0]['text']=='question'


def test_detail_raw_and_assets_pin_one_native_revision(tmp_path):
    store,path,identity,_=reference(tmp_path)
    first=store.detail(identity,'project:first',0,1)
    revision=first['archive']['revision']
    assert store.detail(identity,'project:first',1,1,revision)['messages'][0]['text']=='answer'
    assert store.raw_page(identity,'project:first',0,2,revision)['revision']==revision
    jsonl(path,codex('changed between pages'))
    with pytest.raises(ArchiveError,match='source_changed'):store.detail(identity,'project:first',1,1,revision)
    with pytest.raises(ArchiveError,match='source_changed'):store.raw_page(identity,'project:first',2,2,revision)
    with pytest.raises(ArchiveError,match='source_changed'):store.asset_bytes(identity,'project:first',first['assets'][0]['hash'],revision)
    assert store.detail(identity,'project:first')['messages'][0]['text']=='changed between pages'


def test_explicit_snapshot_is_independent_and_preserves_reference(tmp_path):
    store,path,identity,_=reference(tmp_path)
    with pytest.raises(ArchiveError,match='reference_requires_archive'):
        store.backup([identity],'project:first',tmp_path/'reference.zip')
    result=store.archive(identity,'project:first')
    snapshot=result['archiveIds'][0]
    assert snapshot!=identity and len(store.list())==2
    backup=tmp_path/'snapshot.zip'
    store.backup([snapshot],'project:first',backup)
    path.unlink()
    assert store.detail(identity,'project:first')['archive']['availability']=='missing'
    page=store.detail(snapshot,'project:first')
    assert page['archive']['storage']=='archive' and page['messages'][0]['text']=='question'
    assert store.asset_bytes(snapshot,'project:first',page['assets'][0]['hash'])[0]==b'fixture-image'
    restored=NativeArchiveStore(tmp_path/'restored')
    restored.recover(backup,'project:new')
    assert restored.detail(snapshot,'project:new')['messages'][0]['text']=='question'


def test_old_archive_metadata_remains_owned_and_backup_cannot_inject_reference(tmp_path):
    store,path,key=setup(tmp_path)
    preview=store.preview([key],'project:first')
    identity=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
    with store.db() as db:
        row=db.execute('SELECT payload FROM revisions').fetchone()
        plan=json.loads(row[0]);plan.pop('storage');plan.pop('availability')
        db.execute('UPDATE revisions SET payload=?',(json.dumps(plan),))
    path.unlink()
    assert store.list()[0]['storage']=='archive' and store.detail(identity,'project:first')['messages']
    backup=tmp_path/'backup.zip';store.backup([identity],'project:first',backup)
    with zipfile.ZipFile(backup) as bundle: files={name:bundle.read(name) for name in bundle.namelist()}
    manifest_name=next(name for name in files if name.endswith('.json'))
    manifest=json.loads(files[manifest_name]);manifest['archives'][0]['storage']='reference'
    manifest['archives'][0]['nativeSource']={'path':'/synthetic-not-allowed'}
    files[manifest_name]=json.dumps(manifest).encode()
    forged=tmp_path/'forged.zip'
    with zipfile.ZipFile(forged,'w') as bundle:
        for name,data in files.items():bundle.writestr(name,data)
    restored=NativeArchiveStore(tmp_path/'restored')
    with pytest.raises(ArchiveError,match='backup_invalid'):restored.recover(forged,'project:new')
    assert restored.list()==[]


def test_reference_resolves_native_move_within_selected_root(tmp_path):
    store,path,identity,_=reference(tmp_path)
    moved=path.parent/'archived'/'session.jsonl';moved.parent.mkdir()
    path.rename(moved)
    page=store.detail(identity,'project:first')
    assert page['archive']['id']==identity and page['archive']['sourcePath']==str(moved)
    assert page['messages'][0]['text']=='question'


def test_replaced_native_session_identity_is_missing(tmp_path):
    store,path,identity,_=reference(tmp_path)
    rows=codex('unrelated')
    rows[0]['payload']['id']='different-native-id'
    jsonl(path,rows)
    assert store.list()[0]['availability']=='missing'
    assert store.detail(identity,'project:first')['archive']['availability']=='missing'
    with pytest.raises(ArchiveError,match='source_missing'):store.archive(identity,'project:first')


@pytest.mark.parametrize('kind',['leaf','directory','hardlink','fifo'])
def test_replaced_native_source_never_follows_links_or_special_files(tmp_path,kind):
    store,path,identity,_=reference(tmp_path)
    outside=tmp_path/'outside';outside.mkdir()
    private=jsonl(outside/path.name,codex('outside synthetic body'))
    path.unlink()
    if kind=='leaf':path.symlink_to(private)
    elif kind=='directory':path.parent.rmdir();path.parent.symlink_to(outside,target_is_directory=True)
    elif kind=='hardlink':os.link(private,path)
    else:os.mkfifo(path)
    assert store.detail(identity,'project:first')['messages']==[]
    assert store.list()[0]['availability']=='missing'
    with pytest.raises(ArchiveError,match='source_missing'):list(store.records(identity))


def test_native_assets_rechecked_and_credentials_and_outside_paths_denied(tmp_path):
    native=tmp_path/'native';native.mkdir()
    asset=native/'report.txt';asset.write_text('first asset')
    secret=native/'auth.json';secret.write_text('synthetic secret')
    outside=tmp_path/'other.txt';outside.write_text('outside')
    text=f'[file]({asset}) [credential]({secret}) [outside]({outside})'
    store,path,identity,_=reference(tmp_path,codex(text))
    page=store.detail(identity,'project:first')
    local=next(a for a in page['assets'] if a['name']=='report.txt')
    assert len(page['assets'])==2
    assert 'attachment_sensitive_or_hidden' in page['warnings']
    assert 'attachment_outside_selected_roots' in page['warnings']
    assert store.asset_bytes(identity,'project:first',local['hash'])[0]==b'first asset'
    asset.write_text('changed asset')
    with pytest.raises(ArchiveError,match='not_found'):store.asset_bytes(identity,'project:first',local['hash'])
    with pytest.raises(ArchiveError,match='source_changed'):
        store.asset_bytes(identity,'project:first',local['hash'],page['archive']['revision'])
    asset.unlink();asset.symlink_to(outside)
    assert all(a['name']!='report.txt' for a in store.detail(identity,'project:first')['assets'])


def test_sqlite_reference_tracks_selected_session_and_deletion(tmp_path):
    root=tmp_path/'hermes';root.mkdir();path=root/'state.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT,title TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT);')
        db.executemany('INSERT INTO sessions VALUES (?,?)',[('one','Selected'),('two','Other')])
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[(1,'one','user','first'),(2,'two','user','unrelated')])
    store=NativeArchiveStore(tmp_path/'archive');found=scan('hermes',root);store.remember_sources(found)
    selected=next(s for s in found if s['nativeId']=='one')
    p=store.preview([selected['sourceId']],'project:first',storage='reference')
    identity=store.import_preview(p['previewToken'],'project:first')['archiveIds'][0]
    with sqlite3.connect(path) as db: db.execute('INSERT INTO messages VALUES (3,"one","assistant","second")')
    assert [m['text'] for m in store.detail(identity,'project:first')['messages']]==['first','second']
    with sqlite3.connect(path) as db:db.execute('DELETE FROM sessions WHERE id="one"')
    assert store.detail(identity,'project:first')['archive']['availability']=='missing'
    assert not any(p.is_file() for p in store.objects.rglob('*'))


def test_sqlite_reference_reads_live_wal_without_copying_database(tmp_path):
    root=tmp_path/'hermes';root.mkdir();path=root/'state.db'
    writer=sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.executescript('CREATE TABLE sessions(id TEXT,title TEXT); CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT);')
        writer.execute('INSERT INTO sessions VALUES ("live","Live")')
        writer.execute('INSERT INTO messages VALUES (1,"live","user","first")');writer.commit()
        store=NativeArchiveStore(tmp_path/'archive');found=scan('hermes',root);store.remember_sources(found)
        preview=store.preview([found[0]['sourceId']],'project:first',storage='reference')
        identity=store.import_preview(preview['previewToken'],'project:first')['archiveIds'][0]
        writer.execute('INSERT INTO messages VALUES (2,"live","assistant","from WAL")');writer.commit()
        before={file.name:file.read_bytes() for file in (path,Path(str(path)+'-wal'))}
        assert [m['text'] for m in store.detail(identity,'project:first')['messages']]==['first','from WAL']
        assert before=={file.name:file.read_bytes() for file in (path,Path(str(path)+'-wal'))}
        assert store.list()[0]['countsCurrent'] is False
        assert not any(p.is_file() for p in store.objects.rglob('*'))
    finally:writer.close()


def test_api_defaults_to_reference_and_explicit_archive_download_survives(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.native_archives import build_native_archive_router
    from app.api.session_auth import SessionAuthPolicy
    from app.persistence.memory import in_memory_repository_set
    from app.projects.models import Project
    repos=in_memory_repository_set();project=Project.create(slug='reference',display_name='Reference')
    asyncio.run(repos.projects.save(project))
    store,path,key=setup(tmp_path)
    app=FastAPI();service=NativeArchiveService(store,repos)
    app.router.routes.extend(build_native_archive_router(service,auth_policy=SessionAuthPolicy(lambda:'fixture')).routes)
    client=TestClient(app,base_url='http://127.0.0.1:8877');headers={'Authorization':'Bearer fixture'}
    preview=client.post('/api/native-archives/preview',headers=headers,json={'projectId':project.id,'sourceIds':[key]}).json()
    assert preview['storage']=='reference'
    result=client.post('/api/native-archives/import',headers=headers,json={'projectId':project.id,'previewToken':preview['previewToken']})
    identity=result.json()['archiveIds'][0]
    page=client.get(f'/api/native-archives/{identity}',headers=headers,params={'projectId':project.id}).json()
    asset=client.get(f'/api/native-archives/{identity}/assets/'+page['assets'][0]['hash'],headers=headers,params={'projectId':project.id})
    assert asset.status_code==200 and asset.content==b'fixture-image'
    revision=page['archive']['revision']
    jsonl(path,codex('changed through API'))
    for endpoint in ('','/records','/assets/'+page['assets'][0]['hash']):
        stale=client.get(f'/api/native-archives/{identity}'+endpoint,headers=headers,params={'projectId':project.id,'revision':revision})
        assert stale.status_code==409 and stale.json()['error']['code']=='archive_source_changed'
    snapshot=client.post(f'/api/native-archives/{identity}/archive',headers=headers,json={'projectId':project.id})
    assert snapshot.status_code==200
    path.unlink()
    item=client.get('/api/native-archives/'+snapshot.json()['archiveIds'][0],headers=headers,params={'projectId':project.id}).json()
    assert item['archive']['storage']=='archive' and item['messages']
