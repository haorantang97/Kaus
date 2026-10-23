"""Archive only selected, stable files through the descriptor that was checked."""
import os
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'kernel'))
from app.api import conversation_files
import native_archives
from native_archives import NativeArchiveStore


def extract(tmp_path, path, *, roots=None):
    store=NativeArchiveStore(tmp_path/'archive')
    source=tmp_path/'native'; source.mkdir(exist_ok=True)
    stage=store.staging/'synthetic'; stage.mkdir(exist_ok=True)
    objects={};assets=[];warnings=set()
    store._extract({'file_path':str(path)},stage,objects,assets,warnings,source,roots or [source])
    return assets,warnings,objects,stage


def test_regular_file_in_selected_root_is_owned(tmp_path):
    root=tmp_path/'native';root.mkdir()
    file=root/'report.txt';file.write_bytes(b'owned content')
    assets,warnings,objects,stage=extract(tmp_path,file)
    assert not warnings and len(assets)==1
    assert (stage/assets[0]['hash']).read_bytes()==b'owned content'
    assert assets[0]['sourceStamp']==native_archives.stamp(file)


@pytest.mark.parametrize('name',['auth.json','config.toml','.env','token.json','credentials.yml','private.key','.hidden/data.txt','../outside.txt'])
def test_sensitive_or_traversing_path_never_copied(tmp_path,name):
    root=tmp_path/'native';root.mkdir()
    file=root/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('private synthetic data')
    assets,warnings,objects,_=extract(tmp_path,file)
    assert not assets and not objects
    assert 'attachment_sensitive_or_hidden' in warnings


@pytest.mark.parametrize('kind',['leaf','directory','hardlink'])
def test_links_never_copied_even_with_in_scope_name(tmp_path,kind):
    root=tmp_path/'native';root.mkdir()
    private=tmp_path/'outside';private.mkdir()
    secret=private/'source.txt';secret.write_text('outside data')
    target=root/'safe.txt'
    if kind=='leaf':target.symlink_to(secret)
    elif kind=='directory':
        (root/'directory').symlink_to(private,target_is_directory=True)
        target=root/'directory'/'source.txt'
    else:os.link(secret,target)
    assets,warnings,objects,_=extract(tmp_path,target)
    assert not assets and not objects and 'attachment_unavailable' in warnings


def test_replace_between_selection_and_open_cannot_redirect_read(tmp_path,monkeypatch):
    root=tmp_path/'native';root.mkdir()
    file=root/'report.txt';file.write_text('normal')
    secret=tmp_path/'outside.txt';secret.write_text('private')
    original=conversation_files._open_no_links
    def switched(path,**kwargs):
        file.unlink();file.symlink_to(secret)
        return original(path,**kwargs)
    monkeypatch.setattr(conversation_files,'_open_no_links',switched)
    assets,warnings,objects,_=extract(tmp_path,file)
    assert not assets and not objects and 'attachment_unavailable' in warnings


def test_replaced_selected_root_cannot_redirect_read(tmp_path):
    root=tmp_path/'native';root.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    (outside/'report.txt').write_text('outside')
    root.rmdir();root.symlink_to(outside,target_is_directory=True)
    assets,warnings,objects,_=extract(tmp_path,root/'report.txt')
    assert not assets and not objects and 'attachment_unavailable' in warnings


def test_mutation_during_read_is_not_published(tmp_path,monkeypatch):
    root=tmp_path/'native';root.mkdir()
    file=root/'report.txt';file.write_text('normal')
    original=native_archives.os.fstat
    calls=0
    def changed(fd):
        nonlocal calls
        calls+=1
        if calls==2:file.write_text('edited content')
        return original(fd)
    monkeypatch.setattr(native_archives.os,'fstat',changed)
    assets,warnings,objects,_=extract(tmp_path,file)
    assert not assets and not objects and 'attachment_changed' in warnings


def test_size_limit_checked_before_read(tmp_path,monkeypatch):
    assert native_archives.ASSET_LIMIT==128*1024*1024
    monkeypatch.setattr(native_archives,'ASSET_LIMIT',8)
    root=tmp_path/'native';root.mkdir()
    file=root/'report.txt';file.write_bytes(b'x'*9)
    assets,warnings,objects,_=extract(tmp_path,file)
    assert not assets and not objects and 'attachment_too_large' in warnings


def test_project_scope_required_and_remote_file_authority_rejected(tmp_path):
    root=tmp_path/'work';root.mkdir()
    file=root/'report.txt';file.write_text('report')
    assets,warnings,_,_=extract(tmp_path,file)
    assert not assets and 'attachment_outside_selected_roots' in warnings
    assets,warnings,_,_=extract(tmp_path,file,roots=[root])
    assert len(assets)==1 and not warnings
    assets,warnings,_,_=extract(tmp_path,'file://other-host'+str(file),roots=[root])
    assert not assets and 'attachment_outside_selected_roots' in warnings


def test_native_root_itself_can_be_hidden(tmp_path):
    root=tmp_path/'.agent'/'sessions';root.mkdir(parents=True)
    file=root/'result.png';file.write_bytes(b'image bytes')
    assets,warnings,_,_=extract(tmp_path,file,roots=[root])
    assert len(assets)==1 and not warnings


def attachment_preview(tmp_path):
    from drivers.archive_sources import scan
    root=tmp_path/'native';root.mkdir()
    attachment=root/'result.txt';attachment.write_bytes(b'original attachment')
    source=root/'session.jsonl'
    source.write_text(json.dumps({'type':'user','sessionId':'synthetic',
        'message':{'role':'user','content':'[Result](result.txt)'}})+'\n')
    store=NativeArchiveStore(tmp_path/'archive')
    store.remember_sources(scan('claude-code',root))
    source_id=scan('claude-code',root)[0]['sourceId']
    preview=store.preview([source_id],'project:test')
    assert preview['assetCount']==1
    return store,preview['previewToken'],attachment


@pytest.mark.parametrize('kind',['fifo','symlink','hardlink','grown'])
def test_import_rechecks_same_descriptor_after_path_swap(tmp_path,monkeypatch,kind):
    store,token,attachment=attachment_preview(tmp_path)
    outside=tmp_path/'outside.txt';outside.write_bytes(b'original attachment')
    original=conversation_files._open_no_links
    def swapped(path,**kwargs):
        assert path==attachment
        attachment.unlink()
        if kind=='fifo':os.mkfifo(attachment)
        elif kind=='symlink':attachment.symlink_to(outside)
        elif kind=='hardlink':os.link(outside,attachment)
        else:attachment.write_bytes(b'a'*100)
        return original(path,**kwargs)
    monkeypatch.setattr(conversation_files,'_open_no_links',swapped)
    with pytest.raises(native_archives.ArchiveError,match='archive_source_changed'):
        store.import_preview(token,'project:test')
    assert store.list()==[]


def test_import_detects_mutation_during_descriptor_read(tmp_path,monkeypatch):
    store,token,attachment=attachment_preview(tmp_path)
    original=os.fstat
    calls=0
    def changed(fd):
        nonlocal calls
        calls+=1
        if calls==2:attachment.write_bytes(b'edited during read')
        return original(fd)
    monkeypatch.setattr(native_archives.os,'fstat',changed)
    with pytest.raises(native_archives.ArchiveError,match='archive_source_changed'):
        store.import_preview(token,'project:test')
    assert store.list()==[]


def test_import_validates_hash_even_with_matching_size_and_stamp(tmp_path,monkeypatch):
    store,token,attachment=attachment_preview(tmp_path)
    original_stamp=native_archives.stamp(attachment)
    attachment.write_bytes(b'x'*attachment.stat().st_size)
    original=native_archives.stamp
    monkeypatch.setattr(native_archives,'stamp',lambda p: original_stamp if Path(p)==attachment else original(p))
    with pytest.raises(native_archives.ArchiveError,match='archive_source_changed'):
        store.import_preview(token,'project:test')
    assert store.list()==[]


def test_import_accepts_unchanged_attachment(tmp_path):
    store,token,attachment=attachment_preview(tmp_path)
    result=store.import_preview(token,'project:test')
    assert result['count']==1 and attachment.read_bytes()==b'original attachment'
