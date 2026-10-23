"""Serialize Kaus Gateway configuration projections across local processes."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import os

from drivers.skill_execution.common import state_root


@asynccontextmanager
async def configuration_lock(spec):
    from .source_session import _connection_id
    directory = state_root(spec) / 'openclaw-config-locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise OSError('Invalid OpenClaw projection lock directory')
    fd = os.open(directory / (_connection_id(spec) + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
