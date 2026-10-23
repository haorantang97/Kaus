"""Read-only native SQLite conversation archive source."""
from __future__ import annotations
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from urllib.parse import quote
import json
from drivers.archive_sources.io import stamp,validate_native_file
SOURCES={'hermes': {'label':'Hermes','format':'state-db-v1'}}
def defaults():
    return [{'id':'hermes',**SOURCES['hermes'],'path':str(Path(os.environ.get('HERMES_HOME',Path.home()/'.hermes')))}]
@contextmanager
def _db(path):
    before=validate_native_file(path)
    for suffix in ('-wal','-shm'):
        sidecar=Path(str(path)+suffix)
        if sidecar.exists() or sidecar.is_symlink(): validate_native_file(sidecar)
    db = sqlite3.connect('file:'+quote(str(path.absolute()))+'?mode=ro', uri=True, timeout=2)
    try:
        after=validate_native_file(path)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):
            raise ValueError('archive_source_changed')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def scan(source,root):
    root=Path(root).expanduser().resolve()
    if not root.exists(): raise ValueError('archive_source_missing')
    rows=[]
    paths = [root] if root.is_file() else [root/'state.db', *root.glob('profiles/*/state.db')]
    for path in paths:
        if not path.is_file():
            continue
        with _db(path) as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}
            if 'id' not in columns:
                raise ValueError('archive_format_unsupported')
            selected = [key for key in ('id','title','started_at','last_activity_at','message_count','cwd') if key in columns]
            for item in db.execute('SELECT '+','.join(selected)+' FROM sessions'):
                item = dict(item)
                rows.append({'source':source, 'nativeId':str(item['id']), 'path':str(path),
                    'title':str(item.get('title') or item['id']), 'modified':str(item.get('last_activity_at') or item.get('started_at') or ''),
                    'bytes':None, 'cwd':item.get('cwd'), 'stamp':stamp(path)})
    return rows

def records(row,*,offset=0):
    path=Path(row["path"])
    with _db(path) as db:
        db.execute('BEGIN')
        header = db.execute('SELECT * FROM sessions WHERE id=?', (row['nativeId'],)).fetchone()
        if header is None:
            raise ValueError('archive_source_changed')
        yield {'kind':'session', 'native':dict(header)}, None
        columns = {r['name'] for r in db.execute('PRAGMA table_info(messages)')}
        if not {'session_id','role','content'} <= columns:
            raise ValueError('archive_format_unsupported')
        for record in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY '+('id' if 'id' in columns else 'rowid'),(row['nativeId'],)):
            yield {'kind':'message', 'native':dict(record)}, None

def probe(row, *, native_identity=None):
    with _db(Path(row['path'])) as db:
        return db.execute('SELECT 1 FROM sessions WHERE id=?',(row['nativeId'],)).fetchone() is not None

def message(source,record):
    if source == 'hermes' and record.get('kind') == 'message':
        payload = record['native']
        content = payload.get('content') or ''
        if isinstance(content,str) and content.lstrip().startswith('['):
            try:
                content = json.loads(content)
            except ValueError:
                pass
        return {'role':payload.get('role','unknown'),'content':content,'timestamp':payload.get('timestamp'),
            'reasoning':payload.get('reasoning_content') or payload.get('reasoning'), 'toolCalls':payload.get('tool_calls')}
    return None


def session_metadata(source,record):
    if record.get('kind')=='session': return {'title':record['native'].get('title'),'cwd':record['native'].get('cwd'),'id':record['native'].get('id')}
    return {}


def serialized_fields(record):
    """Native SQLite stores multimodal content as a serialized JSON array."""
    content=record.get('native',{}).get('content') if record.get('kind')=='message' else None
    if isinstance(content,str) and content.lstrip().startswith('['):
        try:
            if isinstance(json.loads(content),list): return (('native','content'),)
        except ValueError:
            pass
    return ()
