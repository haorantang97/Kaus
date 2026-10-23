"""Read-only metadata utilities shared by archive adapters."""
import hashlib, json, os, stat
from pathlib import Path
MAX_RECORD_BYTES=32*1024*1024

def open_native_file(path, *, directory=False):
    """Open a selected file without following any replaced path component."""
    path=Path(path).absolute()
    if any(part in {'.','..'} for part in path.parts): raise ValueError('archive_source_missing')
    fd=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=next_fd
        selected=os.open(path.name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|(os.O_DIRECTORY if directory else 0),dir_fd=fd)
        info=os.fstat(selected)
        if (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1)) or (directory and not stat.S_ISDIR(info.st_mode)):
            os.close(selected)
            raise ValueError('archive_source_missing')
        return selected
    finally:
        os.close(fd)

def validate_native_file(path, *, directory=False):
    fd=open_native_file(path,directory=directory)
    try: return os.fstat(fd)
    finally: os.close(fd)

def stamp(path):
    path = Path(path)
    values = []
    for item in (path, Path(str(path)+'-wal')):
        try:
            st = item.stat()
            values.append([str(item.resolve()), st.st_size, st.st_mtime_ns, st.st_ino])
        except FileNotFoundError:
            values.append([str(item), None])
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()
