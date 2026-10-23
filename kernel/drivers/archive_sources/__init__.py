"""Explicit read-only archive adapter registry. Implementations stay vendor-owned."""
from __future__ import annotations
import hashlib, importlib, json
from pathlib import Path
from .io import MAX_RECORD_BYTES, stamp
from .serialized_json import materialize

def _modules():
    for file in sorted(Path(__file__).resolve().parents[1].glob('*/archive_source.py')):
        yield importlib.import_module('drivers.'+file.parent.name+'.archive_source')
MODULES=tuple(_modules())
ADAPTERS={key:meta for module in MODULES for key,meta in module.SOURCES.items()}
def adapter(source):
    for module in MODULES:
        if source in module.SOURCES: return module
    raise ValueError('archive_source_unsupported')
def defaults():
    return [source for module in MODULES for source in module.defaults()]
def scan(source,root):
    rows=adapter(source).scan(source,root)
    for row in rows:
        row['sourceId']=hashlib.sha256((row['source']+'\0'+row['path']+'\0'+row['nativeId']).encode()).hexdigest()
        row['format']=ADAPTERS[source]['format']
        row['scanRoot']=str(Path(root).expanduser().resolve())
    return sorted(rows,key=lambda row:str(row['modified']),reverse=True)
def records(row,*,offset=0):
    yield from adapter(row['source']).records(row,offset=offset)
def message(source,record):
    return adapter(source).message(source,materialize(record))
def session_metadata(source,record):
    return adapter(source).session_metadata(source,record)
def serialized_fields(source,record):
    hook=getattr(adapter(source),'serialized_fields',None)
    return hook(record) if hook else ()

def probe(row, native_identity=None):
    """Cheap session existence check; never enumerate transcript messages."""
    hook=getattr(adapter(row['source']),'probe',None)
    return hook(row,native_identity=native_identity) if hook else True
