"""Real subprocess checks for EOF persistence and bounded ACP shutdown."""
from __future__ import annotations

import asyncio
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from drivers.acp import client
from drivers.acp.client import AcpAgentSpec, AcpConnection


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
