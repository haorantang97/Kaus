"""Streaming JSONL primitives; no engine-specific schema."""
import json,re,os
from pathlib import Path
from .io import stamp,MAX_RECORD_BYTES,open_native_file,validate_native_file

def scan_paths(source,root,paths):
    rows=[]
    for path in paths:
        try: validate_native_file(path)
        except (OSError,ValueError): continue
        resolved=path.resolve()
        if not resolved.is_relative_to(root if root.is_dir() else root.parent) or not resolved.is_file(): continue
        st=resolved.stat()
        native_id=(re.findall(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',path.stem) or [path.stem])[-1]
        rows.append({'source':source,'nativeId':native_id,'path':str(resolved),'title':path.stem,
            'modified':st.st_mtime,'bytes':st.st_size,'stamp':stamp(resolved),'appendOnly':True})
    return rows

def records(row,*,offset=0):
    path=Path(row["path"])
    with os.fdopen(open_native_file(path),'rb') as stream:
        stream.seek(offset)
        while True:
            line = stream.readline(MAX_RECORD_BYTES+1)
            if not line:
                break
            if len(line)>MAX_RECORD_BYTES:
                raise ValueError('archive_record_too_large')
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise ValueError('archive_record_corrupt') from None
            yield parsed, stream.tell()


def probe_identity(row, expected_identity, identity):
    """Inspect at most 64 KiB of leading metadata, never a whole transcript."""
    if not expected_identity: return True
    with os.fdopen(open_native_file(row['path']),'rb') as stream:
        remaining=64*1024
        for _ in range(16):
            line=stream.readline(remaining+1)
            remaining-=len(line)
            if not line or remaining<0: break
            try: record=json.loads(line)
            except (ValueError,UnicodeError): continue
            found=identity(record) if isinstance(record,dict) else None
            if found is not None: return str(found)==expected_identity
            if not remaining: break
    # Some historical files have no leading identity; the full read validates it.
    return True
