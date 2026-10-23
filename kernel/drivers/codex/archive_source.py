"""Read-only Codex rollout archive adapter."""
import os,sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from drivers.archive_sources.jsonl import records,scan_paths,probe_identity
SOURCES={'codex':{'label':'Codex','format':'rollout-jsonl-v1'}}
def defaults():
    return [{'id':'codex',**SOURCES['codex'],'path':str(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')))}]
def scan(source,root):
    root=Path(root).expanduser().resolve()
    if not root.exists(): raise ValueError('archive_source_missing')
    if root.is_file(): paths=[root] if root.suffix=='.jsonl' else []
    elif (root/'sessions').exists(): paths=[*(root/'sessions').rglob('*.jsonl'),*(root/'archived_sessions').rglob('*.jsonl')]
    else: paths=root.rglob('*.jsonl')
    rows=scan_paths(source,root,paths)
    # Title/cwd index only: source transcripts are not read during discovery.
    if root.is_dir():
        for path in sorted(root.glob('state_*.sqlite'),reverse=True):
            if not path.resolve().is_relative_to(root): continue
            try:
                with closing(sqlite3.connect('file:'+quote(str(path))+'?mode=ro',uri=True)) as db:
                    columns={r[1] for r in db.execute('PRAGMA table_info(threads)')}
                    if not {'id','title'}<=columns: continue
                    titles={r[0]:r[1] for r in db.execute('SELECT id,title FROM threads')}
                    for row in rows:
                        if titles.get(row['nativeId']): row['title']=str(titles[row['nativeId']])[:240]
                    break
            except sqlite3.Error: continue
    return rows

def message(source,record):
    if record.get('type')=='response_item' and record.get('payload',{}).get('type')=='message':
        payload=record['payload']
        return {'role':payload.get('role','unknown'),'content':payload.get('content',[]),'timestamp':record.get('timestamp')}
    return None

def session_metadata(source,record):
    return record.get('payload',{}) if record.get('type')=='session_meta' else {}

def probe(row, *, native_identity=None):
    return probe_identity(row,native_identity,lambda record:session_metadata(row['source'],record).get('id'))
