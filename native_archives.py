"""Native history references and independent, content-addressed archives.

Native directories are read-only. Compressed records and asset bytes are owned
copies. Import visibility is a SQLite transaction; interrupted object writes can
be retried without replacing existing content.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile

from drivers.archive_sources import ADAPTERS, MAX_RECORD_BYTES, defaults, message, records, scan, session_metadata, serialized_fields, stamp, probe
from drivers.archive_sources.serialized_json import JSON_MARKER, pack as pack_json_string, materialize, restore as restore_json_string

HASH = re.compile(r'^[a-f0-9]{64}$')
ASSET_LIMIT = 128 * 1024 * 1024
BACKUP_LIMIT = 2 * 1024 * 1024 * 1024
MARKER = '__kaus_archive_asset_v1__'


class ArchiveError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()


def file_hash(path, end=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        remaining = end
        while remaining is None or remaining > 0:
            chunk = stream.read(1024*1024 if remaining is None else min(remaining,1024*1024))
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.part-')
    try:
        with os.fdopen(descriptor,'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unpack(data):
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        raw = stream.read(MAX_RECORD_BYTES+1)
    if len(raw)>MAX_RECORD_BYTES:
        raise ArchiveError('archive_record_too_large')
    return raw


class NativeArchiveStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.objects = self.root/'objects'
        self.staging = self.root/'staging'
        self.objects.mkdir(exist_ok=True, mode=0o700)
        self.staging.mkdir(exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, source TEXT NOT NULL, native_id TEXT NOT NULL, title TEXT NOT NULL,
                    current_revision TEXT NOT NULL, created REAL NOT NULL, deleted REAL);
                CREATE TABLE IF NOT EXISTS revisions (archive_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(archive_id,id));
                CREATE TABLE IF NOT EXISTS links (archive_id TEXT NOT NULL, project_id TEXT NOT NULL, PRIMARY KEY(archive_id,project_id));
                CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, encoding TEXT NOT NULL, size INTEGER NOT NULL, stored_size INTEGER NOT NULL);
            ''')
        # Expired previews are disposable, never linked archive data.
        for item in self.staging.iterdir():
            if item.is_dir() and item.stat().st_mtime < time.time()-86400:
                shutil.rmtree(item)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root/'archive.sqlite3', timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def object_path(self, key):
        if not isinstance(key,str) or not HASH.fullmatch(key):
            raise ArchiveError('archive_invalid_hash')
        return self.objects/key[:2]/key

    def remember_sources(self, rows):
        with self.db() as db:
            db.executemany('INSERT OR REPLACE INTO sources VALUES (?,?)', [(r['sourceId'],json.dumps(r)) for r in rows])

    def sources(self, ids):
        if not ids or len(ids)>100 or len(set(ids))!=len(ids):
            raise ArchiveError('archive_selection_invalid')
        with self.db() as db:
            result=[]
            for key in ids:
                row=db.execute('SELECT payload FROM sources WHERE id=?',(key,)).fetchone()
                if row is None:
                    raise ArchiveError('archive_source_not_scanned')
                result.append(json.loads(row['payload']))
            return result

    def latest(self, archive_id):
        with self.db() as db:
            row=db.execute('SELECT r.payload FROM revisions r JOIN archives a ON a.id=r.archive_id AND a.current_revision=r.id WHERE a.id=?',(archive_id,)).fetchone()
            plan=json.loads(row['payload']) if row else None
        return self._read_reference(plan)[0] if plan and plan.get('storage')=='reference' else plan

    def _stored_plan(self, archive_id, revision=None):
        with self.db() as db:
            if revision is None:
                row=db.execute('SELECT current_revision FROM archives WHERE id=?',(archive_id,)).fetchone()
                if row is None: raise ArchiveError('archive_not_found')
                revision=row[0]
            row=db.execute('SELECT payload FROM revisions WHERE archive_id=? AND id=?',(archive_id,revision)).fetchone()
        if row is None: raise ArchiveError('archive_not_found')
        return json.loads(row[0])

    def _reference_source(self, plan):
        """Resolve a native identity only inside its explicitly selected source root."""
        from drivers.archive_sources.io import validate_native_file
        source=dict(plan['nativeSource'])
        try:
            validate_native_file(source['path'])
            return source
        except FileNotFoundError:
            root=source.get('scanRoot')
            if root:
                try:
                    validate_native_file(root,directory=Path(root).is_dir())
                    candidates=[r for r in scan(source['source'],root) if r['nativeId']==source['nativeId']]
                    validate_native_file(root,directory=Path(root).is_dir())
                    if len(candidates)==1:
                        validate_native_file(candidates[0]['path'])
                        return candidates[0]
                except (OSError,ValueError,sqlite3.Error):
                    pass
        except (OSError,ValueError):
            pass
        raise ArchiveError('archive_source_missing')

    def _read_reference(self, plan, *, expected_revision=None):
        """Read on demand into memory. No record, asset or parsed body is persisted."""
        source=self._reference_source(plan)
        before=stamp(source['path'])
        values=[]; assets=[]; warnings=set(); payloads={}; hashes=[]; count=0; size=0; native_identity=None
        title=source.get('title') or plan['title']
        def sink(raw, encoding):
            nonlocal size
            key=digest(raw)
            if key not in payloads:
                size+=len(raw)
                if size>BACKUP_LIMIT: raise ArchiveError('archive_batch_too_large')
                payloads[key]=raw
            return key
        root=Path(source['path']).parent
        allowed=[root]
        if plan.get('workspaceRoot'): allowed.append(Path(plan['workspaceRoot']))
        try:
            for record,_ in records(source):
                size+=len(encoded(record))
                if size>BACKUP_LIMIT or len(values)>=100000: raise ArchiveError('archive_index_too_large')
                meta=session_metadata(source['source'],record)
                if native_identity is None and meta.get('id'):
                    native_identity=str(meta['id'])
                    if plan.get('nativeIdentity') and native_identity!=plan['nativeIdentity']:
                        raise ArchiveError('archive_source_missing')
                if meta.get('title'): title=str(meta['title'])[:240]
                if message(source['source'],record): count+=1
                self._extract(record,None,{},assets,warnings,root,allowed,
                    serialized_fields(source['source'],record),object_sink=sink)
                hashes.append(digest(encoded(record)))
                values.append(record)
        except (FileNotFoundError,sqlite3.Error):
            raise ArchiveError('archive_source_missing') from None
        except ValueError as exc:
            if str(exc)=='archive_source_changed': raise ArchiveError('archive_source_missing') from None
            raise
        if before!=stamp(source['path']): raise ArchiveError('archive_source_changed')
        if not values: raise ArchiveError('archive_source_missing')
        if plan.get('nativeIdentity') and native_identity!=plan['nativeIdentity']:
            raise ArchiveError('archive_source_missing')
        revision=digest(encoded({'records':hashes,'assets':assets,'format':source['format']}))
        # Only the returned live revision can pin a subsequent read. The mapping
        # revision in SQLite is not a snapshot of the native transcript.
        if expected_revision and expected_revision!=revision:
            raise ArchiveError('archive_source_changed')
        live={**plan,'title':title,'sourcePath':source['path'],'sourceStamp':before,
            'revision':revision,'records':hashes,'assets':assets,'messageCount':count,
            'warnings':sorted(warnings),'availability':'available','nativeIdentity':native_identity}
        return live,values,payloads

    def _reference_status(self, plan):
        try:
            source=self._reference_source(plan)
            if not probe(source,plan.get('nativeIdentity')): raise ArchiveError('archive_source_missing')
            return {**plan,'sourcePath':source['path'],'availability':'available',
                'countsCurrent':stamp(source['path'])==plan.get('sourceStamp')}
        except (OSError,ValueError,sqlite3.Error) as exc:
            return self._missing_reference(plan,exc)

    @staticmethod
    def _missing_reference(plan, exc):
        code=getattr(exc,'code',None) or str(exc)
        return {**plan,'records':[],'assets':[],'messageCount':0,'assetCount':0,'countsCurrent':False,
            'availability':'missing','warnings':[code if code.startswith('archive_') else 'archive_source_missing']}

    def list(self, project_id=None, trash=False):
        with self.db() as db:
            sql='SELECT a.* FROM archives a WHERE a.deleted IS '+('NOT NULL' if trash else 'NULL')
            params=[]
            if project_id:
                sql+=' AND EXISTS (SELECT 1 FROM links l WHERE l.archive_id=a.id AND l.project_id=?)';params.append(project_id)
            result=[]
            for row in db.execute(sql+' ORDER BY a.created DESC', params):
                value=dict(row)
                value['projectIds']=[r[0] for r in db.execute('SELECT project_id FROM links WHERE archive_id=?',(row['id'],))]
                revision=db.execute('SELECT payload FROM revisions WHERE archive_id=? AND id=?',(row['id'],row['current_revision'])).fetchone()
                info=json.loads(revision[0])
                if info.get('storage')=='reference': info=self._reference_status(info)
                result.append({'id':row['id'],'source':row['source'],'nativeId':row['native_id'],'title':info['title'],
                    'revision':info['revision'],'created':row['created'],'deleted':row['deleted'],
                    'projectIds':value['projectIds'],'messageCount':info['messageCount'],'assetCount':info.get('assetCount',len(info['assets'])),
                    'warnings':info['warnings'],'storage':info.get('storage','archive'),
                    'availability':info.get('availability','available'),'sourcePath':info.get('sourcePath'),
                    'countsCurrent':info.get('countsCurrent',True)})
            return result

    def authorize(self, archive_id, project_id, *, trash=False):
        with self.db() as db:
            row=db.execute('SELECT a.* FROM archives a JOIN links l ON l.archive_id=a.id WHERE a.id=? AND l.project_id=?',(archive_id,project_id)).fetchone()
        if row is None or (row['deleted'] is not None and not trash):
            raise ArchiveError('archive_not_found')
        return dict(row)

    def revision(self, archive_id, revision):
        current=self._stored_plan(archive_id)
        if current.get('storage')=='reference':
            return self._read_reference(current,expected_revision=revision)[0]
        with self.db() as db:
            row=db.execute("SELECT payload FROM revisions WHERE archive_id=? AND id=?",(archive_id,revision)).fetchone()
        if row is None: raise ArchiveError("archive_not_found")
        return json.loads(row[0])

    def records(self, archive_id, revision=None):
        current=self._stored_plan(archive_id)
        if current.get('storage')=='reference':
            yield from self._read_reference(current,expected_revision=revision)[1]
            return
        with self.db() as db:
            if revision is None:
                row=db.execute('SELECT current_revision FROM archives WHERE id=?',(archive_id,)).fetchone()
                if row is None: raise ArchiveError('archive_not_found')
                revision=row[0]
            row=db.execute('SELECT payload FROM revisions WHERE archive_id=? AND id=?',(archive_id,revision)).fetchone()
            if row is None: raise ArchiveError('archive_not_found')
            plan=json.loads(row[0])
        for key in plan['records']:
            yield json.loads(unpack(self.object_path(key).read_bytes()))

    def decode_record(self, record):
        def expand(value):
            if isinstance(value,dict) and JSON_MARKER in value:
                return restore_json_string(value,expand)
            if isinstance(value,dict) and MARKER in value:
                raw=self.object_path(value[MARKER]).read_bytes()
                text=base64.b64encode(raw).decode()
                return value.get('prefix','')+text
            if isinstance(value,dict): return {key:expand(item) for key,item in value.items()}
            if isinstance(value,list): return [expand(item) for item in value]
            return value
        return expand(record)

    def link(self, archive_id, project_id):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM archives WHERE id=? AND deleted IS NULL',(archive_id,)).fetchone():
                raise ArchiveError('archive_not_found')
            db.execute('INSERT OR IGNORE INTO links VALUES (?,?)',(archive_id,project_id))

    def unlink(self, archive_id, project_id):
        self.authorize(archive_id,project_id)
        with self.db() as db:
            db.execute('DELETE FROM links WHERE archive_id=? AND project_id=?',(archive_id,project_id))

    def unlink_project(self, project_id):
        """Project deletion only removes associations; archive content stays owned."""
        with self.db() as db:
            db.execute("DELETE FROM links WHERE project_id=?",(project_id,))

    def reconcile_projects(self, project_ids):
        """Remove stale associations against the authoritative Project repository."""
        valid=set(project_ids)
        with self.db() as db:
            stale=[(row[0],) for row in db.execute('SELECT DISTINCT project_id FROM links') if row[0] not in valid]
            db.executemany('DELETE FROM links WHERE project_id=?',stale)

    def trash(self, archive_id, project_id, restore=False):
        if restore:
            return self.restore_to_project(archive_id,project_id)
        self.authorize(archive_id,project_id,trash=restore)
        with self.db() as db:
            db.execute('UPDATE archives SET deleted=? WHERE id=?',(None if restore else time.time(),archive_id))

    def restore_to_project(self, archive_id, project_id):
        """Restore an owned archive and add its validated destination atomically."""
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM archives WHERE id=?',(archive_id,)).fetchone():
                raise ArchiveError('archive_not_found')
            db.execute('INSERT OR IGNORE INTO links VALUES (?,?)',(archive_id,project_id))
            db.execute('UPDATE archives SET deleted=NULL WHERE id=?',(archive_id,))

    def _stage_object(self, stage, raw, encoding, objects):
        key=digest(b'record\0'+raw) if encoding=='gzip' else digest(raw)
        payload=gzip.compress(raw,compresslevel=6,mtime=0) if encoding=='gzip' else raw
        descriptor={'id':key,'encoding':encoding,'size':len(raw),'storedSize':len(payload)}
        if key not in objects:
            objects[key]=descriptor
            if not self.object_path(key).is_file():
                atomic(stage/key,payload)
        return key

    def _extract(self, record, stage, objects, assets, warnings, base, allowed, serialized=(), *, object_sink=None):
        def asset(raw, name, mime, origin=None, origin_stamp=None):
            if len(raw)>ASSET_LIMIT: raise ArchiveError('archive_asset_too_large')
            key=object_sink(raw,'raw') if object_sink else self._stage_object(stage,raw,'raw',objects)
            descriptor={'hash':key,'name':Path(name).name[:240] or 'attachment','mime':mime or 'application/octet-stream','size':len(raw)}
            if origin:
                descriptor['sourcePath']=str(origin); descriptor['sourceStamp']=origin_stamp or stamp(origin)
            if descriptor not in assets: assets.append(descriptor)
            return key
        def local(path,name=None,mime=None):
            from app.api.conversation_files import _open_no_links, _safe_parts
            import stat
            from urllib.parse import unquote, urlparse
            if path.startswith('file://'):
                parsed=urlparse(path)
                if parsed.netloc or parsed.query or parsed.fragment:
                    warnings.add('attachment_outside_selected_roots');return
                path=unquote(parsed.path)
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',path):
                warnings.add('remote_attachment_not_copied');return
            if not path or '\0' in path or '\\' in path:
                warnings.add('attachment_unavailable');return
            candidate=Path(path).expanduser()
            if not candidate.is_absolute(): candidate=base/candidate
            # Scan/preview already supplies canonical roots. Do not resolve them
            # again: a replaced parent directory must not redirect this read.
            # The descriptor traversal below rejects every symlink.
            resolved=None
            for root in allowed:
                original=Path(root).expanduser().absolute()
                if candidate.is_relative_to(original):
                    relative=candidate.relative_to(original)
                    if not _safe_parts(relative):
                        warnings.add('attachment_sensitive_or_hidden');return
                    resolved=original/relative
                    break
            if resolved is None:
                warnings.add('attachment_outside_selected_roots');return
            try:
                before=stamp(resolved)
                with os.fdopen(_open_no_links(resolved),'rb') as handle:
                    initial=os.fstat(handle.fileno())
                    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink!=1:
                        warnings.add('attachment_unavailable');return
                    if initial.st_size>ASSET_LIMIT:
                        warnings.add('attachment_too_large');return
                    raw=handle.read(ASSET_LIMIT+1)
                    final=os.fstat(handle.fileno())
                    fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink')
                    if any(getattr(initial,key)!=getattr(final,key) for key in fields) or stamp(resolved)!=before:
                        warnings.add('attachment_changed');return
                if len(raw)>ASSET_LIMIT:
                    warnings.add('attachment_too_large');return
            except FileNotFoundError:
                warnings.add('attachment_missing');return
            except (OSError,ValueError):
                warnings.add('attachment_unavailable');return
            asset(raw, resolved.name, mime or mimetypes.guess_type(resolved.name)[0],resolved,before)
        def walk(value, field='', parent=None, path=()):
            if path in serialized and isinstance(value,str):
                parsed=json.loads(value)
                extracted=walk(parsed,field,parent)
                return pack_json_string(value,parsed,extracted)
            if isinstance(value,str):
                match=re.fullmatch(r'(data:([^;,]+);base64,)([A-Za-z0-9+/=\s]+)',value)
                if match:
                    try: raw=base64.b64decode(match[3],validate=True)
                    except ValueError: warnings.add('attachment_invalid_base64');return value
                    key=asset(raw,'image'+(mimetypes.guess_extension(match[2]) or ''),match[2])
                    return {MARKER:key,'prefix':match[1]}
                if field=='data' and isinstance(parent,dict) and parent.get('type')=='base64':
                    try: raw=base64.b64decode(value,validate=True)
                    except ValueError: warnings.add('attachment_invalid_base64');return value
                    mime=parent.get('media_type') or parent.get('mime_type')
                    key=asset(raw,'attachment'+(mimetypes.guess_extension(mime or '') or ''),mime)
                    return {MARKER:key,'prefix':''}
                if field in {'image_url','file_path','image_path','filePath'} or (field=='url' and len(path)>1 and path[-2]=='image_url'):
                    local(value)
                if field in {'text','content','output'}:
                    for match in re.finditer(r'!?\[([^\]]*)\]\(([^\s)]+)\)',value):
                        url=match[2]
                        if url.startswith(('http://','https://')):
                            if match[0].startswith('!'): warnings.add('remote_attachment_not_copied')
                            continue
                        local(url,match[1])
                return value
            if isinstance(value,dict):
                if MARKER in value or JSON_MARKER in value: raise ArchiveError('archive_reserved_record_key')
                return {key:walk(item,key,value,(*path,key)) for key,item in value.items()}
            if isinstance(value,list): return [walk(item,field,parent,(*path,index)) for index,item in enumerate(value)]
            return value
        return walk(record)

    def _preview_references(self, rows, project_id, workspace):
        token=uuid.uuid4().hex
        stage=self.staging/token
        stage.mkdir(mode=0o700)
        plans=[]; summaries=[]
        try:
            for source in rows:
                if stamp(source['path'])!=source['stamp']: raise ArchiveError('archive_source_changed')
                archive_id=digest(('reference\0'+source['source']+'\0'+source['nativeId']).encode())
                # Only discovery metadata and explicitly selected roots are saved.
                native={key:source[key] for key in ('source','nativeId','path','title','format','scanRoot') if key in source}
                plan={'id':archive_id,'source':source['source'],'nativeId':source['nativeId'],
                    'title':source['title'],'storage':'reference','availability':'available',
                    'sourcePath':source['path'],'sourceStamp':source['stamp'],
                    'format':source['format'],'nativeSource':native,
                    'workspaceRoot':str(Path(workspace).expanduser().resolve()) if workspace else None,
                    'records':[],'assets':[],'warnings':[],'messageCount':0}
                plan['revision']=digest(encoded({'nativeSource':native,'workspaceRoot':plan['workspaceRoot']}))
                live,_,_=self._read_reference(plan)
                plan['messageCount']=live['messageCount']
                plan['assetCount']=len(live['assets'])
                plan['nativeIdentity']=live['nativeIdentity']
                plan['warnings']=live['warnings']
                plans.append(plan);summaries.append(live)
            plan={'version':1,'token':token,'projectId':project_id,'created':time.time(),
                'storage':'reference','archives':plans,'objects':{}}
            atomic(stage/'plan.json',encoded(plan))
            return {'previewToken':token,'storage':'reference','newBytes':0,'dedupBytes':0,
                'compressedBytes':0,'rawBytes':0,'indexEstimateBytes':len(encoded(plan)),
                'messageCount':sum(p['messageCount'] for p in summaries),
                'assetCount':sum(len(p['assets']) for p in summaries),
                'warnings':sorted({w for p in summaries for w in p['warnings']}),
                'archives':[self._summary(p) for p in summaries]}
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True)
            raise

    def preview(self, source_ids, project_id, workspace=None, storage='archive'):
        if storage not in {'reference','archive'}: raise ArchiveError('archive_storage_invalid')
        rows=self.sources(source_ids)
        if storage=='reference': return self._preview_references(rows,project_id,workspace)
        token=uuid.uuid4().hex
        stage=self.staging/token
        stage.mkdir(mode=0o700)
        objects={}; plans=[]
        try:
            for source in rows:
                before=stamp(source['path'])
                if before!=source['stamp']: raise ArchiveError('archive_source_changed')
                archive_id=digest((source['source']+'\0'+source['nativeId']).encode())
                previous=self.latest(archive_id)
                ids=[]; assets=[]; warnings=set(); end=0; offset=0
                # Append-only JSONL reuses already archived complete records after
                # checking the byte prefix. Mutations fall back to reparsing.
                if previous and source.get('appendOnly',False) and previous.get('sourcePath')==source['path']:
                    end=previous.get('endOffset') or 0
                    if end and Path(source['path']).stat().st_size>=end and file_hash(source['path'],end)==previous.get('prefixHash'):
                        ids=list(previous['records']);assets=list(previous['assets']);warnings=set(previous['warnings']);offset=end
                        for key in ids+[a['hash'] for a in assets]:
                            with self.db() as db:
                                item=db.execute('SELECT * FROM objects WHERE id=?',(key,)).fetchone()
                            if item is None: raise ArchiveError('archive_object_missing')
                            objects[key]={'id':key,'encoding':item['encoding'],'size':item['size'],'storedSize':item['stored_size']}
                title=previous['title'] if previous else source['title']
                count=previous['messageCount'] if offset else 0
                retained_assets=len(assets)
                root=Path(source['path']).parent
                allowed=[root]
                if workspace: allowed.append(Path(workspace).expanduser().resolve())
                for record, boundary in records(source,offset=offset):
                    meta=session_metadata(source['source'],record)
                    if meta.get('title'): title=str(meta['title'])[:240]
                    view=message(source['source'],record)
                    if view:
                        count+=1
                        if title==source['title'] and view['role']=='user':
                            title=plain_content(view['content'])[:100] or title
                    stored=self._extract(record,stage,objects,assets,warnings,root,allowed,serialized_fields(source['source'],record))
                    raw=encoded(stored)
                    if len(raw)>MAX_RECORD_BYTES: raise ArchiveError('archive_record_too_large')
                    ids.append(self._stage_object(stage,raw,'gzip',objects))
                    if len(ids)>100000: raise ArchiveError('archive_index_too_large')
                    end=boundary or 0
                if before!=stamp(source['path']): raise ArchiveError('archive_source_changed')
                if not ids: raise ArchiveError('archive_empty')
                if count==0: warnings.add('no_supported_message_records')
                revision=digest(encoded({'records':ids,'assets':assets,'format':source['format']}))
                plans.append({'id':archive_id,'source':source['source'],'nativeId':source['nativeId'],'title':title,
                    'storage':'archive','availability':'available',
                    'revision':revision,'records':ids,'assets':assets,'warnings':sorted(warnings),'messageCount':count,
                    'format':source['format'],'sourcePath':source['path'],'sourceStamp':before,'endOffset':end,
                    'prefixHash':file_hash(source['path'],end) if end else None,
                    'sourceDependencies':[a for a in assets[retained_assets:] if a.get('sourcePath')]})
            plan={'version':1,'token':token,'projectId':project_id,'created':time.time(),'archives':plans,'objects':objects}
            plan_bytes=encoded(plan)
            if len(plan_bytes)>32*1024*1024: raise ArchiveError('archive_index_too_large')
            if sum(o['size'] for o in objects.values())>BACKUP_LIMIT: raise ArchiveError('archive_batch_too_large')
            atomic(stage/'plan.json',plan_bytes)
            index_estimate=0
            with self.db() as db:
                for entry in plans:
                    if not db.execute('SELECT 1 FROM revisions WHERE archive_id=? AND id=?',(entry['id'],entry['revision'])).fetchone(): index_estimate+=len(encoded(entry))
                    if not db.execute('SELECT 1 FROM links WHERE archive_id=? AND project_id=?',(entry['id'],project_id)).fetchone(): index_estimate+=128
            total=sum(o['storedSize'] for o in objects.values())
            added=sum(o['storedSize'] for key,o in objects.items() if not self.object_path(key).is_file())
            return {'previewToken':token,'storage':'archive','newBytes':added,'dedupBytes':total-added,'compressedBytes':sum(o['storedSize'] for o in objects.values() if o['encoding']=='gzip'),
                'rawBytes':sum(o['size'] for o in objects.values() if o['encoding']=='gzip'),'indexEstimateBytes':index_estimate,
                'messageCount':sum(p['messageCount'] for p in plans),'assetCount':sum(len(p['assets']) for p in plans),
                'warnings':sorted({w for p in plans for w in p['warnings']}),'archives':[self._summary(p) for p in plans]}
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True)
            raise

    @staticmethod
    def _summary(plan):
        return {**{key:plan[key] for key in ('id','source','nativeId','title','revision','messageCount','warnings')},
            'storage':plan.get('storage','archive'),'availability':plan.get('availability','available'),
            'sourcePath':plan.get('sourcePath')}

    def archive(self, archive_id, project_id):
        """Explicitly save an independent snapshot; keep the reference and its links."""
        self.authorize(archive_id,project_id)
        plan=self._stored_plan(archive_id)
        if plan.get('storage')!='reference': return {'archiveIds':[archive_id],'count':1}
        source=self._reference_source(plan)
        self._read_reference(plan)  # Confirm the path still contains the linked native session.
        source['stamp']=stamp(source['path'])
        source['sourceId']=digest((source['source']+'\0'+source['path']+'\0'+source['nativeId']).encode())
        self.remember_sources([source])
        preview=self.preview([source['sourceId']],project_id,plan.get('workspaceRoot'),storage='archive')
        return self.import_preview(preview['previewToken'],project_id)

    def _validate_object(self, data, descriptor):
        raw=unpack(data) if descriptor['encoding']=='gzip' else data
        if len(raw)!=descriptor['size'] or (digest(b'record\0'+raw) if descriptor['encoding']=='gzip' else digest(raw))!=descriptor['id']:
            raise ArchiveError('archive_object_corrupt')
        return raw

    def _commit(self, plan, stage, project_id, *, restoring=False):
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for key,item in plan['objects'].items():
                target=self.object_path(key)
                if target.is_file():
                    self._validate_object(target.read_bytes(),item)
                else:
                    payload=(stage/key).read_bytes()
                    self._validate_object(payload,item)
                    atomic(target,payload)
                db.execute('INSERT OR IGNORE INTO objects VALUES (?,?,?,?)',(key,item['encoding'],item['size'],item['storedSize']))
            imported=[]
            fresh=set()
            for row in plan['archives']:
                existing=db.execute('SELECT * FROM archives WHERE id=?',(row['id'],)).fetchone()
                db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)',(row['id'],row['revision'],json.dumps(row),time.time()))
                if existing is None:
                    fresh.add(row['id'])
                    db.execute('INSERT INTO archives VALUES (?,?,?,?,?,?,NULL)',(row['id'],row['source'],row['nativeId'],row['title'],row['revision'],time.time()))
                elif not restoring:
                    db.execute('UPDATE archives SET current_revision=?,title=?,deleted=NULL WHERE id=?',(row['revision'],row['title'],row['id']))
                db.execute('INSERT OR IGNORE INTO links VALUES (?,?)',(row['id'],project_id))
                imported.append(row['id'])
            if restoring:
                for key, revision in plan.get('currentRevisions',{}).items():
                    if key in fresh:
                        row=next(item for item in plan['archives'] if item['id']==key and item['revision']==revision)
                        db.execute('UPDATE archives SET current_revision=?,title=? WHERE id=?',(revision,row['title'],key))
            imported=list(dict.fromkeys(imported))
            return {'archiveIds':imported,'count':len(imported)}

    def import_preview(self, token, project_id):
        if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token): raise ArchiveError('archive_preview_expired')
        stage=self.staging/token
        try: plan=json.loads((stage/'plan.json').read_text())
        except (OSError,ValueError): raise ArchiveError('archive_preview_expired') from None
        if plan['projectId']!=project_id or plan['created']<time.time()-3600: raise ArchiveError('archive_preview_expired')
        for row in plan['archives']:
            if stamp(row['sourcePath'])!=row['sourceStamp']: raise ArchiveError('archive_source_changed')
            if row.get('endOffset') and file_hash(row['sourcePath'],row['endOffset'])!=row['prefixHash']:
                raise ArchiveError('archive_source_changed')
            for asset in row.get('sourceDependencies',[]):
                from app.api.conversation_files import _open_no_links
                import stat
                # Verify the exact previously authorized path through one
                # non-blocking descriptor. Never reopen it after checking.
                path=Path(asset['sourcePath'])
                try:
                    before=stamp(path)
                    if before!=asset['sourceStamp']:
                        raise ArchiveError('archive_source_changed')
                    with os.fdopen(_open_no_links(path),'rb') as handle:
                        initial=os.fstat(handle.fileno())
                        if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink!=1
                                or initial.st_size>ASSET_LIMIT or initial.st_size!=asset['size']):
                            raise ArchiveError('archive_source_changed')
                        content_hash=hashlib.sha256()
                        remaining=ASSET_LIMIT+1
                        while remaining:
                            chunk=handle.read(min(1024*1024,remaining))
                            if not chunk: break
                            remaining-=len(chunk)
                            content_hash.update(chunk)
                        final=os.fstat(handle.fileno())
                        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink')
                        if (not remaining or any(getattr(initial,key)!=getattr(final,key) for key in fields)
                                or stamp(path)!=before or content_hash.hexdigest()!=asset['hash']):
                            raise ArchiveError('archive_source_changed')
                except (OSError,ValueError):
                    raise ArchiveError('archive_source_changed') from None
        result=self._commit(plan,stage,project_id)
        for key in plan['objects']:
            (stage/key).unlink(missing_ok=True)
        # Keep plan briefly for idempotent request retries; source remains unchanged.
        return result

    def visible_messages(self, archive_id, project_id, revision=None):
        """Single pass over an immutable revision; excludes private reasoning fields."""
        header=self.authorize(archive_id,project_id)
        index=0
        for record in self.records(archive_id,revision):
            row=message(header['source'],record)
            if row:
                yield {'role':row['role'],'text':plain_content(row['content']),
                    'timestamp':row.get('timestamp'),'index':index}
                index+=1

    def raw_page(self, archive_id, project_id, offset=0, limit=25, revision=None):
        header=self.authorize(archive_id,project_id)
        stored=self._stored_plan(archive_id)
        if stored.get('storage')=='reference':
            plan,records,_=self._read_reference(stored,expected_revision=revision)
            values=records[offset:offset+limit]
            end=offset+len(values)
            return {'records':values,'offset':offset,'total':len(records),'revision':plan['revision'],
                'nextOffset':end if end<len(records) else None}
        plan=self.revision(archive_id,revision or header['current_revision'])
        keys=plan['records'][offset:offset+limit]
        values=[json.loads(unpack(self.object_path(key).read_bytes())) for key in keys]
        end=offset+len(values)
        return {'records':values,'offset':offset,'total':len(plan['records']),'revision':plan['revision'],
            'nextOffset':end if end<len(plan['records']) else None}

    def detail(self, archive_id, project_id, offset=0, limit=200, revision=None):
        header=self.authorize(archive_id,project_id)
        stored=self._stored_plan(archive_id)
        if stored.get('storage')=='reference':
            try:
                plan,values,_=self._read_reference(stored,expected_revision=revision)
            except (OSError,ValueError,sqlite3.Error) as exc:
                if revision: raise
                plan=self._missing_reference(stored,exc)
                values=[]
        else:
            plan=self.revision(archive_id,revision or header['current_revision'])
            values=self.records(archive_id,revision or header['current_revision'])
        found=[]; index=0
        for record in values:
            row=message(header['source'],record)
            if row:
                if index>=offset and len(found)<limit:
                    found.append({**row,'index':index,'text':plain_content(row['content'])})
                index+=1
                if index>=offset+limit: break
        return {'archive':{**self._summary(plan),'projectId':project_id},'messages':found,'assets':plan['assets'],
            'offset':offset,'nextOffset':offset+len(found) if offset+len(found)<plan['messageCount'] else None,
            'warnings':plan['warnings'],'recordCount':len(plan['records'])}

    def asset(self, archive_id, project_id, key, revision=None):
        header=self.authorize(archive_id,project_id)
        if self._stored_plan(archive_id).get('storage')=='reference':
            raise ArchiveError('archive_reference_requires_stream')
        plan=self.revision(archive_id,revision or header["current_revision"])
        asset=next((a for a in plan['assets'] if a['hash']==key),None)
        if asset is None: raise ArchiveError('archive_not_found')
        path=self.object_path(key)
        if path.stat().st_size!=asset['size'] or file_hash(path)!=key: raise ArchiveError('archive_object_corrupt')
        return path,asset

    def asset_bytes(self, archive_id, project_id, key, revision=None):
        self.authorize(archive_id,project_id)
        stored=self._stored_plan(archive_id)
        if stored.get('storage')=='reference':
            if not isinstance(key,str) or not HASH.fullmatch(key): raise ArchiveError('archive_invalid_hash')
            plan,_,payloads=self._read_reference(stored,expected_revision=revision)
            info=next((a for a in plan['assets'] if a['hash']==key),None)
            if info is None or key not in payloads: raise ArchiveError('archive_not_found')
            return payloads[key],info
        path,info=self.asset(archive_id,project_id,key,revision)
        raw=path.read_bytes()
        if digest(raw)!=key or len(raw)!=info["size"]: raise ArchiveError("archive_object_corrupt")
        return raw,info

    def backup(self, archive_ids, project_id, output):
        plans=[];objects={};current={}
        for key in archive_ids:
            header=self.authorize(key,project_id)
            if self._stored_plan(key).get('storage')=='reference':
                raise ArchiveError('archive_reference_requires_archive')
            current[key]=header['current_revision']
            with self.db() as db:
                for row in db.execute('SELECT payload FROM revisions WHERE archive_id=?',(key,)):
                    plans.append(json.loads(row[0]))
        for plan in plans:
            for key in plan['records']+[a['hash'] for a in plan['assets']]:
                with self.db() as db: item=db.execute('SELECT * FROM objects WHERE id=?',(key,)).fetchone()
                if item is None: raise ArchiveError('archive_object_missing')
                objects[key]={'id':key,'encoding':item['encoding'],'size':item['size'],'storedSize':item['stored_size']}
        manifest={'version':1,'archives':plans,'objects':objects,'currentRevisions':current}
        self._validate_backup_plan(manifest)
        manifest_bytes=encoded(manifest)
        if len(objects)+1>200000 or len(manifest_bytes)>32*1024*1024 or sum(o['storedSize'] for o in objects.values())+len(manifest_bytes)>BACKUP_LIMIT: raise ArchiveError('archive_backup_too_large')
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr('manifest.json',manifest_bytes)
            for key in objects: bundle.write(self.object_path(key),'objects/'+key)

    def recover(self, backup_path, project_id):
        stage=Path(tempfile.mkdtemp(prefix='restore-',dir=self.staging))
        try:
            with zipfile.ZipFile(backup_path) as bundle:
                infos=bundle.infolist()
                if len(infos)>200000 or sum(i.file_size for i in infos)>BACKUP_LIMIT: raise ArchiveError('archive_backup_too_large')
                names=[i.filename for i in infos]
                if len(names)!=len(set(names)) or any(i.flag_bits&1 or ((i.external_attr>>16)&0o170000)==0o120000 for i in infos):
                    raise ArchiveError('archive_backup_invalid')
                if any(name!='manifest.json' and not re.fullmatch(r'objects/[a-f0-9]{64}',name) for name in names):
                    raise ArchiveError('archive_backup_invalid')
                if bundle.getinfo('manifest.json').file_size>32*1024*1024: raise ArchiveError('archive_backup_too_large')
                plan=json.loads(bundle.read('manifest.json'))
                self._validate_backup_plan(plan)
                if set(names)!={'manifest.json',*('objects/'+key for key in plan['objects'])}: raise ArchiveError('archive_backup_invalid')
                for key,item in plan['objects'].items():
                    info=bundle.getinfo('objects/'+key)
                    if info.file_size>ASSET_LIMIT or info.file_size!=item['storedSize']: raise ArchiveError('archive_backup_invalid')
                    data=bundle.read(info)
                    raw=self._validate_object(data,item)
                    if item['encoding']=='gzip': json.loads(raw)
                    atomic(stage/key,data)
            self._validate_backup_records(plan,stage)
            return self._commit(plan,stage,project_id,restoring=True)
        except (zipfile.BadZipFile,KeyError,TypeError,UnicodeError,json.JSONDecodeError,gzip.BadGzipFile,EOFError):
            raise ArchiveError('archive_backup_invalid') from None
        finally:
            shutil.rmtree(stage,ignore_errors=True)

    def _validate_backup_records(self, plan, stage):
        for archive in plan['archives']:
            assets={item['hash'] for item in archive['assets']}
            count=0
            def check(value):
                if isinstance(value,dict):
                    if JSON_MARKER in value:
                        try: materialize(value)
                        except (ValueError,TypeError): raise ArchiveError('archive_backup_invalid') from None
                    if MARKER in value:
                        if value[MARKER] not in assets or set(value)!={MARKER,'prefix'} or not isinstance(value['prefix'],str):
                            raise ArchiveError('archive_backup_invalid')
                        if value['prefix'] and not re.fullmatch(r'data:[a-zA-Z0-9.+/-]+;base64,',value['prefix']):
                            raise ArchiveError('archive_backup_invalid')
                    else:
                        for item in value.values(): check(item)
                elif isinstance(value,list):
                    for item in value: check(item)
            for key in archive['records']:
                record=json.loads(unpack((stage/key).read_bytes()))
                if not isinstance(record,dict): raise ArchiveError('archive_backup_invalid')
                check(record)
                if message(archive['source'],record): count+=1
            if count!=archive['messageCount']:
                raise ArchiveError('archive_backup_invalid')

    def _validate_backup_plan(self, plan):
        if not isinstance(plan,dict) or plan.get('version')!=1 or not isinstance(plan.get('archives'),list) or not isinstance(plan.get('objects'),dict): raise ArchiveError('archive_backup_invalid')
        for key,item in plan['objects'].items():
            if not HASH.fullmatch(key) or not isinstance(item,dict) or item.get('id')!=key or item.get('encoding') not in {'raw','gzip'}: raise ArchiveError('archive_backup_invalid')
            if any(type(item.get(field)) is not int or item[field]<0 or item[field]>ASSET_LIMIT for field in ('size','storedSize')): raise ArchiveError('archive_backup_invalid')
        if sum(item['size'] for item in plan['objects'].values())>BACKUP_LIMIT: raise ArchiveError('archive_backup_too_large')
        for row in plan['archives']:
            if not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not HASH.fullmatch(row[k]) for k in ('id','revision')): raise ArchiveError('archive_backup_invalid')
            if row.get('storage','archive')!='archive': raise ArchiveError('archive_backup_invalid')
            if row.get('source') not in ADAPTERS or not isinstance(row.get('nativeId'),str) or not isinstance(row.get('title'),str): raise ArchiveError('archive_backup_invalid')
            if row['id']!=digest((row['source']+'\0'+row['nativeId']).encode()): raise ArchiveError('archive_backup_invalid')
            if not isinstance(row.get('records'),list) or not isinstance(row.get('assets'),list) or not isinstance(row.get('warnings'),list) or type(row.get('messageCount')) is not int: raise ArchiveError('archive_backup_invalid')
            if row.get('format')!=ADAPTERS[row['source']]['format'] or any(not isinstance(w,str) for w in row['warnings']): raise ArchiveError('archive_backup_invalid')
            for key in row['records']:
                if key not in plan['objects'] or plan['objects'][key]['encoding']!='gzip': raise ArchiveError('archive_backup_invalid')
            for asset in row['assets']:
                if not isinstance(asset,dict) or not isinstance(asset.get('name'),str) or not isinstance(asset.get('mime'),str) or asset.get('hash') not in plan['objects']: raise ArchiveError('archive_backup_invalid')
                if not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+',asset['mime']) or any(c in asset['name'] for c in ('\r','\n','\x00')): raise ArchiveError('archive_backup_invalid')
                if type(asset.get('size')) is not int or asset['size']!=plan['objects'][asset['hash']]['size']: raise ArchiveError('archive_backup_invalid')
                if plan['objects'][asset['hash']]['encoding']!='raw': raise ArchiveError('archive_backup_invalid')
            expected=digest(encoded({'records':row['records'],'assets':row['assets'],'format':row.get('format')}))
            if row['revision']!=expected: raise ArchiveError('archive_backup_invalid')
        referenced={key for row in plan['archives'] for key in row['records']} | {asset['hash'] for row in plan['archives'] for asset in row['assets']}
        if referenced!=set(plan['objects']): raise ArchiveError('archive_backup_invalid')
        current=plan.get('currentRevisions',{})
        if not isinstance(current,dict) or set(current)!={r['id'] for r in plan['archives']} or any(not any(r['id']==key and r['revision']==revision for r in plan['archives']) for key,revision in current.items()): raise ArchiveError('archive_backup_invalid')


def plain_content(content):
    if isinstance(content,str): return content
    if isinstance(content,list): return '\n'.join(filter(None,(plain_content(item) for item in content)))
    if isinstance(content,dict):
        if isinstance(content.get('text'),str): return content['text']
        if content.get('type') in {'tool_result','tool_use'}:
            return str(content.get('content') or content.get('name') or '')
    return ''


class NativeArchiveService:
    def __init__(self, store, repositories, source_roots=None):
        self.store=store;self.repositories=repositories;self.source_roots=source_roots

    async def project(self, project_id):
        project=await self.repositories.projects.get(project_id)
        if project is None: raise ArchiveError('archive_project_missing')
        return project

    async def list(self, project_id=None, trash=False):
        import asyncio
        projects=await self.repositories.projects.list_all()
        await asyncio.to_thread(self.store.reconcile_projects,[p.id for p in projects])
        return await asyncio.to_thread(self.store.list,project_id,trash)

    async def detail(self, archive_id, project_id, offset=0, limit=200):
        import asyncio
        await self.project(project_id)
        return await asyncio.to_thread(self.store.detail,archive_id,project_id,offset,limit)

    def sources(self):
        return self.source_roots if self.source_roots is not None else defaults()

    def choices(self):
        """History readers with an existing local default; no transcript discovery."""
        return [{'id':row['id'],'label':row['label']} for row in self.sources()
                if Path(row['path']).expanduser().exists()]

    async def managed_native_ids(self, project_id):
        identities=set()
        conversations=[*await self.repositories.conversations.list_for_project(project_id),
                       *await self.repositories.conversations.list_for_project(project_id,archived=True)]
        for conversation in conversations:
            if not conversation.native_session_id: continue
            binding=await self.repositories.bindings.get(conversation.agent_binding_id)
            if binding is None: continue
            backend=binding.backend_id.removeprefix('backend:')
            source='hermes' if backend in {'hermes','hermes-acp'} else backend
            identities.add((source,conversation.native_session_id))
        return identities

    async def project_items(self, project_id=None):
        projects=await self.repositories.projects.list_all()
        managed=set()
        for project in projects: managed.update(await self.managed_native_ids(project.id))
        if project_id: projects=[p for p in projects if p.id==project_id]
        project_ids={p.id for p in projects}
        items=[];seen=set()
        for item in sorted(await self.list(project_id),key=lambda row:row['storage']!='reference'):
            if item['availability']=='missing': continue
            if (item['source'],item['nativeId']) in managed: continue
            linked=[identity for identity in item['projectIds'] if identity in project_ids
                    and (identity,item['source'],item['nativeId']) not in seen]
            if not linked: continue
            seen.update((identity,item['source'],item['nativeId']) for identity in linked)
            items.append({key:item[key] for key in ('id','source','nativeId','title','revision','created','messageCount','assetCount','storage','availability')} | {'projectIds':linked,'warnings':[]})
        return {'items':items,'projects':[{'id':p.id,'displayName':p.display_name,'slug':p.slug} for p in projects]}

    async def candidates(self, source, project_id):
        import asyncio
        await self.project(project_id)
        managed=set()
        for project in await self.repositories.projects.list_all(): managed.update(await self.managed_native_ids(project.id))
        linked={item['id'] for item in await self.list(project_id)}
        found=await asyncio.to_thread(self.scan,source)
        return {'sessions':[{key:row[key] for key in ('sourceId','source','nativeId','title','modified')}
                | {'added':(source,row['nativeId']) in managed or
                   digest(('reference\0'+source+'\0'+row['nativeId']).encode()) in linked or
                   digest((source+'\0'+row['nativeId']).encode()) in linked}
                for row in found['sessions']]}

    async def add(self, source_ids, project_id):
        import asyncio
        target=await self.project(project_id)
        managed=set()
        for project in await self.repositories.projects.list_all(): managed.update(await self.managed_native_ids(project.id))
        rows=await asyncio.to_thread(self.store.sources,source_ids)
        selected=[row['sourceId'] for row in rows if (row['source'],row['nativeId']) not in managed]
        if not selected: return {'archiveIds':[],'count':0}
        def commit():
            preview=self.store.preview(selected,project_id,target.workspace_root,storage='reference')
            return self.store.import_preview(preview['previewToken'],project_id)
        return await asyncio.to_thread(commit)

    def scan(self, source, path=None):
        known=next((item for item in self.sources() if item['id']==source),None)
        if known is None: raise ArchiveError('archive_source_unsupported')
        try: rows=scan(source,path or known['path'])
        except ValueError as exc: raise ArchiveError(str(exc)) from None
        self.store.remember_sources(rows)
        imported={item['id']:item for item in self.store.list()}
        for row in rows:
            key=digest((row['source']+'\0'+row['nativeId']).encode())
            reference=digest(('reference\0'+row['source']+'\0'+row['nativeId']).encode())
            row['archived']=key in imported
            row['referenced']=reference in imported
            row['imported']=row['archived'] or row['referenced']
        return {'sessions':rows,'count':len(rows)}
