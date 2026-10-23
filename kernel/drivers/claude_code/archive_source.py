"""Read-only Claude Code project JSONL archive adapter."""
import json,os
from pathlib import Path
from drivers.archive_sources.jsonl import records,scan_paths,probe_identity
SOURCES={'claude-code':{'label':'Claude Code','format':'project-jsonl-v1'}}
def defaults():
    return [{'id':'claude-code',**SOURCES['claude-code'],'path':str(Path(os.environ.get('CLAUDE_CONFIG_DIR',Path.home()/'.claude'))/'projects')}]
def scan(source,root):
    root=Path(root).expanduser().resolve()
    if not root.exists(): raise ValueError('archive_source_missing')
    paths=([root] if root.suffix=='.jsonl' else []) if root.is_file() else root.rglob('*.jsonl')
    rows=scan_paths(source,root,paths)
    titles={}
    for parent in {Path(row['path']).parent for row in rows}:
        path=parent/'sessions-index.json'
        try:
            if not path.resolve().is_relative_to(root if root.is_dir() else root.parent) or path.stat().st_size>16*1024*1024: continue
            data=json.loads(path.read_text())
            for entry in data.get('entries',[]):
                if isinstance(entry,dict) and isinstance(entry.get('sessionId'),str): titles[entry['sessionId']]=entry
        except (OSError,ValueError,AttributeError): continue
    for row in rows:
        metadata=titles.get(row['nativeId'],{})
        row['title']=str(metadata.get('summary') or metadata.get('firstPrompt') or row['title'])[:240]
        row['cwd']=metadata.get('projectPath')
    return rows

def message(source,record):
    payload=record.get('message')
    if record.get('type') in {'user','assistant'} and isinstance(payload,dict):
        return {'role':payload.get('role',record['type']),'content':payload.get('content',[]),'timestamp':record.get('timestamp')}
    return None

def session_metadata(source,record):
    return {'title':record.get('summary') or record.get('customTitle'),'cwd':record.get('cwd'),'id':record.get('sessionId')}

def probe(row, *, native_identity=None):
    return probe_identity(row,native_identity,lambda record:record.get('sessionId'))
