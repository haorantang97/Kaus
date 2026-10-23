"""Real subprocess checks for EOF persistence and bounded ACP shutdown."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivers.acp import client
from drivers.acp.client import AcpAgentSpec, AcpConnection


@pytest.mark.parametrize('members,raises', [('123 Z\n999 S\n', False), ('123 S\n', True)])
def test_darwin_group_permission_error_requires_no_live_members(monkeypatch, members, raises):
    connection = AcpConnection(None, None, process=SimpleNamespace(pid=123), owns_process_group=True)
    def deny(*args):
        raise PermissionError('group unavailable')
    monkeypatch.setattr(client.os, 'killpg', deny)
    monkeypatch.setattr(client.sys, 'platform', 'darwin')
    monkeypatch.setattr(client.subprocess, 'check_output', lambda *args, **kwargs: members)
    if raises:
        with pytest.raises(PermissionError):
            connection._signal_owned_process(signal.SIGTERM)
    else:
        connection._signal_owned_process(signal.SIGTERM)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process group lifecycle')
@pytest.mark.parametrize('parent_exits_first', [False, True])
async def test_wrapped_agent_children_do_not_survive_shutdown(tmp_path, parent_exits_first, monkeypatch):
    monkeypatch.setattr(client, '_EOF_EXIT_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(client, '_TERMINATE_EXIT_GRACE_SECONDS', 0.15)
    marker = tmp_path / 'native.pid'
    native = tmp_path / 'native.py'
    native.write_text('import os,signal,time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'Path({str(marker)!r}).write_text(str(os.getpid()))\n'
        'while True: time.sleep(1)\n')
    wrapper = tmp_path / 'wrapper.py'
    wrapper.write_text('import subprocess,sys,time,json\nfrom pathlib import Path\n'
        f'child=subprocess.Popen([sys.executable,{str(native)!r}])\n'
        f'while not Path({str(marker)!r}).exists(): time.sleep(.01)\n'
        'request=json.loads(sys.stdin.readline())\n'
        'print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
        + ('sys.exit(0)\n' if parent_exits_first else 'sys.stdin.read()\nchild.wait()\n'))
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(sys.executable, str(wrapper))))
    await connection.call('ping', {}, timeout=5)
    native_pid = int(marker.read_text())
    if parent_exits_first:
        for _ in range(100):
            if connection._process.returncode is not None: break
            await asyncio.sleep(.01)
        assert connection._process.returncode == 0
    try:
        await asyncio.wait_for(connection.aclose(), 2)
        assert connection.close_confirmed
        assert connection._owned_group_active() is False
        for _ in range(100):
            try: os.kill(native_pid, 0)
            except ProcessLookupError: break
            await asyncio.sleep(.01)
        else: pytest.fail('Owned native child survived ACP shutdown')
    finally:
        try: os.kill(native_pid, signal.SIGKILL)
        except ProcessLookupError: pass
        await connection.aclose()


async def spawn_child(tmp_path: Path, after_eof: str, *, ignore_term: bool = False) -> AcpConnection:
    script = tmp_path / "child.py"
    source = """
import json
import signal
import sys
import time
from pathlib import Path
"""
    if ignore_term:
        source += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    source += """
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ready": True}}), flush=True)
"""
    script.write_text(source + textwrap.dedent(after_eof))
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(sys.executable, str(script)), cwd=str(tmp_path)))
    assert await connection.call("ping", {}, timeout=5) == {"ready": True}
    return connection


async def test_eof_allows_native_session_flush(tmp_path: Path) -> None:
    connection = await spawn_child(tmp_path, """
time.sleep(0.08)
Path("persisted.json").write_text('{"session": "complete"}')
""")
    try:
        await connection.aclose()
        assert (tmp_path / "persisted.json").read_text() == '{"session": "complete"}'
        assert connection._process.returncode == 0
        assert not connection.alive
    finally:
        await connection.aclose()


@pytest.mark.parametrize('cancel_close', [False, True])
async def test_adapter_grace_preserves_native_eof_cleanup(tmp_path, monkeypatch, cancel_close):
    monkeypatch.setattr(client, '_EOF_EXIT_GRACE_SECONDS', 0.02)
    connection = await spawn_child(tmp_path, '''
time.sleep(0.20)
Path('eof-cleanup-complete').write_text('yes')
''')
    connection.graceful_shutdown_timeout = 1.0
    try:
        close = asyncio.create_task(connection.aclose())
        if cancel_close:
            await asyncio.sleep(0.04)
            close.cancel()
            await asyncio.sleep(0.02)
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close, 2)
        else:
            await asyncio.wait_for(close, 2)
        assert (tmp_path / 'eof-cleanup-complete').read_text() == 'yes'
        assert connection._process.returncode == 0
        assert connection.close_confirmed
    finally:
        await connection.aclose()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process group lifecycle')
async def test_adapter_grace_waits_for_descendant_after_launcher_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(client, '_EOF_EXIT_GRACE_SECONDS', 0.02)
    child = tmp_path / 'native.py'
    child.write_text("import sys,time\nfrom pathlib import Path\n"
        "sys.stdin.read()\ntime.sleep(.20)\n"
        f"Path({str(tmp_path / 'descendant-cleaned')!r}).write_text('yes')\n")
    wrapper = tmp_path / 'wrapper.py'
    wrapper.write_text("import subprocess,sys,json\n"
        f"child=subprocess.Popen([sys.executable,{str(child)!r}],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "request=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':request['id'],'result':{}}),flush=True)\n"
        "sys.stdin.read()\nchild.stdin.close()\n")
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(sys.executable, str(wrapper))))
    connection.graceful_shutdown_timeout = 1.0
    await connection.call('ping', {}, timeout=5)
    try:
        await asyncio.wait_for(connection.aclose(), 2)
        assert (tmp_path / 'descendant-cleaned').read_text() == 'yes'
        assert connection.close_confirmed
    finally:
        await connection.aclose()


async def test_adapter_grace_still_bounds_uncooperative_native_process(tmp_path, monkeypatch):
    monkeypatch.setattr(client, '_TERMINATE_EXIT_GRACE_SECONDS', 0.05)
    connection = await spawn_child(tmp_path, 'while True: time.sleep(1)\n', ignore_term=True)
    connection.graceful_shutdown_timeout = 0.05
    try:
        await asyncio.wait_for(connection.aclose(), 2)
        assert connection._process.returncode == -signal.SIGKILL
        assert connection.close_confirmed
    finally:
        await connection.aclose()


@pytest.mark.skipif(os.name != 'posix', reason='Detached process and inherited pipes')
@pytest.mark.parametrize('cancel_close', [False, True])
async def test_adapter_close_is_bounded_but_unconfirmed_while_detached_child_holds_pipes(tmp_path, monkeypatch, cancel_close):
    monkeypatch.setattr(client, '_TERMINATE_EXIT_GRACE_SECONDS', 0.02)
    monkeypatch.setattr(client, '_KILL_EXIT_GRACE_SECONDS', 0.03, raising=False)
    detached = tmp_path / 'detached.py'
    marker = tmp_path / 'detached.pid'
    detached.write_text('import os,time\nfrom pathlib import Path\n'
        f'Path({str(marker)!r}).write_text(str(os.getpid()))\n'
        'time.sleep(4)\n')
    parent = tmp_path / 'parent.py'
    parent.write_text('import json,signal,subprocess,sys,time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'subprocess.Popen([sys.executable,{str(detached)!r}],start_new_session=True)\n'
        f'while not Path({str(marker)!r}).exists(): time.sleep(.005)\n'
        'request=json.loads(sys.stdin.readline())\n'
        'print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
        'sys.stdin.read()\nwhile True: time.sleep(1)\n')
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(sys.executable, str(parent))))
    connection.graceful_shutdown_timeout = 0.02
    await connection.call('ping', {}, timeout=5)
    child_pid = int(marker.read_text())
    close = asyncio.create_task(connection.aclose())
    try:
        if cancel_close:
            await asyncio.sleep(.01)
            close.cancel()
            await asyncio.sleep(.01)
            close.cancel()
        done, _ = await asyncio.wait({close}, timeout=.8)
        assert close in done, 'ACP close waited indefinitely for detached child pipe EOF'
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                close.result()
        else:
            close.result()
        assert connection._process.returncode == -signal.SIGKILL
        assert connection._owned_group_active() is False
        assert not connection.close_confirmed
        # A dead leader and empty original group do not prove inherited pipes
        # were released. Retrying while the detached child lives stays false.
        await asyncio.wait_for(connection.aclose(), 1)
        assert not connection.close_confirmed
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(close, return_exceptions=True)
        await asyncio.wait_for(connection.aclose(), 2)
    assert connection.close_confirmed


async def test_eof_and_terminate_ignoring_child_is_killed_within_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client, "_EOF_EXIT_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(client, "_TERMINATE_EXIT_GRACE_SECONDS", 0.10)
    connection = await spawn_child(tmp_path, """
Path("eof-seen").write_text("yes")
while True:
    time.sleep(1)
""", ignore_term=True)
    try:
        started = time.monotonic()
        await asyncio.wait_for(connection.aclose(), 2)
        elapsed = time.monotonic() - started
        assert (tmp_path / "eof-seen").read_text() == "yes"
        assert 0.10 <= elapsed < 1.5
        assert connection._process.returncode == -signal.SIGKILL
    finally:
        await connection.aclose()


async def test_cancelling_shutdown_reaps_owned_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client, "_EOF_EXIT_GRACE_SECONDS", 5)
    connection = await spawn_child(tmp_path, """
while True:
    time.sleep(1)
""", ignore_term=True)
    close = asyncio.create_task(connection.aclose())
    await asyncio.sleep(0.02)
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close, 2)
    assert connection._process.returncode == -signal.SIGKILL
    assert connection._read_task is None
    await connection.aclose()
    assert connection.close_confirmed


async def test_concurrent_close_waiters_share_cleanup_despite_repeated_cancellation(monkeypatch):
    connection = AcpConnection(None, None)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def cleanup():
        calls.append(True)
        entered.set()
        await finish.wait()
        connection._close_confirmed = True

    monkeypatch.setattr(connection, "_close_owned", cleanup)
    first = asyncio.create_task(connection.aclose())
    await entered.wait()
    second = asyncio.create_task(connection.aclose())
    await asyncio.sleep(0)
    assert not second.done() and not connection.close_confirmed
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not connection._close_task.done()
    finish.set()
    await second
    assert connection.close_confirmed and calls == [True]


async def test_failed_close_remains_unconfirmed_and_retry_reaps_process(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_EOF_EXIT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(client, "_TERMINATE_EXIT_GRACE_SECONDS", 0.05)
    connection = await spawn_child(tmp_path, """
while True:
    time.sleep(1)
""", ignore_term=True)
    signal_owned = connection._signal_owned_process
    failures = 1

    def transient_failure(signum):
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("synthetic temporary process-control failure")
        signal_owned(signum)

    monkeypatch.setattr(connection, "_signal_owned_process", transient_failure)
    try:
        with pytest.raises(OSError, match="temporary process-control"):
            await connection.aclose()
        assert not connection.close_confirmed
        assert connection._closed
        assert connection._process.returncode is None
        await connection.aclose()
        assert connection.close_confirmed
        assert connection._process.returncode == -signal.SIGKILL
    finally:
        await connection.aclose()


@pytest.mark.parametrize("members,expected", [
    ("123 Z\n123 Z+\n999 S\n", False),
    ("123 Z\n123 S\n", True),
    ("999 S\n", False),
])
def test_owned_group_confirmation_ignores_zombies(monkeypatch, members, expected):
    connection = AcpConnection(None, None, process=SimpleNamespace(pid=123), owns_process_group=True)
    monkeypatch.setattr(client.os, "killpg", lambda *args: None)
    monkeypatch.setattr(client.subprocess, "check_output", lambda *args, **kwargs: members)
    assert connection._owned_group_active() is expected


async def test_unknown_group_status_never_confirms_shutdown(monkeypatch):
    connection = AcpConnection(None, None, process=SimpleNamespace(pid=123), owns_process_group=True)
    monkeypatch.setattr(client.os, "killpg", lambda *args: None)

    def failed_status(*args, **kwargs):
        raise OSError("synthetic process status unavailable")

    monkeypatch.setattr(client.subprocess, "check_output", failed_status)
    assert not await connection._confirm_owned_group_exit(timeout=0.02)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group lifecycle")
async def test_exited_leader_does_not_confirm_live_descendant_and_retry_can_finish(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    native = tmp_path / "native.py"
    native.write_text(
        "import os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import subprocess,sys,time,json\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,{str(native)!r}])\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(.005)\n"
        "request=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':request['id'],'result':{}}),flush=True)\n")
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(sys.executable, str(wrapper))))
    await connection.call("ping", {}, timeout=5)
    pid = int(marker.read_text())
    async with asyncio.timeout(5):
        while connection._process.returncode is None:
            await asyncio.sleep(.005)
    signal_owned = connection._signal_owned_process
    # Model an OS call returning before a descendant is actually terminated.
    monkeypatch.setattr(connection, "_signal_owned_process", lambda _: None)
    try:
        started = time.monotonic()
        await connection.aclose()
        assert time.monotonic() - started < 1.5
        assert not connection.close_confirmed
        assert connection._process.returncode == 0
        assert connection._owned_group_active() is True
        monkeypatch.setattr(connection, "_signal_owned_process", signal_owned)
        await connection.aclose()
        assert connection.close_confirmed
        assert connection._owned_group_active() is False
    finally:
        monkeypatch.setattr(connection, "_signal_owned_process", signal_owned)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await connection.aclose()
