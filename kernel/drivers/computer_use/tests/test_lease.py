import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from drivers.computer_use import ResourceBusyError, ResourceLease

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource leases")


async def test_same_desktop_waits_and_independent_browser_contexts_do_not(tmp_path):
    owner = ResourceLease("desktop:local", root=tmp_path)
    await owner.acquire()
    with pytest.raises(ResourceBusyError):
        await ResourceLease("desktop:local", root=tmp_path, timeout=0).acquire()
    async with ResourceLease("browser:context:one", root=tmp_path):
        async with ResourceLease("browser:context:two", root=tmp_path):
            pass
    owner.release()
    async with ResourceLease("desktop:local", root=tmp_path, timeout=0) as next_owner:
        assert next_owner.held


async def test_waiting_cancellation_does_not_release_another_owner(tmp_path):
    notices = []
    async with ResourceLease("desktop:local", root=tmp_path):
        waiter = ResourceLease("desktop:local", root=tmp_path, on_waiting=lambda: notices.append("waiting"))
        task = asyncio.create_task(waiter.acquire())
        await asyncio.sleep(.02)
        assert waiter.waiting and notices == ["waiting"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not waiter.held and not waiter.waiting
        with pytest.raises(ResourceBusyError):
            await ResourceLease("desktop:local", root=tmp_path, timeout=0).acquire()
    async with ResourceLease("desktop:local", root=tmp_path, timeout=0):
        pass


async def test_cancelled_holder_context_releases_lock(tmp_path):
    acquired = asyncio.Event()
    async def work():
        async with ResourceLease("desktop:local", root=tmp_path):
            acquired.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(work())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with ResourceLease("desktop:local", root=tmp_path, timeout=0):
        pass


async def test_lock_is_cross_process_not_only_asyncio(tmp_path):
    code = """
import asyncio, sys
from drivers.computer_use import ResourceLease, ResourceBusyError
async def main():
    try:
        async with ResourceLease('desktop:local', root=sys.argv[1], timeout=0):
            print('acquired')
    except ResourceBusyError:
        print('busy')
asyncio.run(main())
"""
    kernel = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(kernel), "PYTHONDONTWRITEBYTECODE": "1"}
    async with ResourceLease("desktop:local", root=tmp_path):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, str(tmp_path), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"busy"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, stderr.decode()
    assert stdout.strip() == b"acquired"


async def test_reentrant_instance_rejected_and_lease_inode_stays(tmp_path):
    lease = ResourceLease("desktop:local", root=tmp_path)
    async with lease:
        with pytest.raises(RuntimeError):
            await lease.acquire()
        inode = next(tmp_path.iterdir()).stat().st_ino
    async with lease:
        assert next(tmp_path.iterdir()).stat().st_ino == inode


async def test_symlinked_lock_directory_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError):
        await ResourceLease("desktop:local", root=link).acquire()
