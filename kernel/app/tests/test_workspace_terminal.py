"""Workspace terminal (2026-11-23「页头面板」⑥): a plain shell in the conversation's
working directory. Real ``/bin/sh`` in a temporary directory; no Agent, no model."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from drivers.terminals.workspace_shell import TerminalManager, shell_environment  # noqa: E402
from app.tests.test_surface_handoff_api import TEST_TOKEN, _client, _harness  # noqa: E402
from runtime.event_envelope import ExtensionEvent  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

SH = lambda: "/bin/sh"  # noqa: E731 - quiet, predictable prompt


async def read_until(queue: asyncio.Queue, needle: bytes, seen: bytes = b"", timeout: float = 5.0) -> bytes:
    deadline = asyncio.get_running_loop().time() + timeout
    while needle not in seen:
        item = await asyncio.wait_for(queue.get(), max(0.05, deadline - asyncio.get_running_loop().time()))
        if item is None:
            break
        seen += item
    return seen


async def test_shell_runs_in_the_workspace_and_replays_to_a_later_viewer(tmp_path: Path) -> None:
    manager = TerminalManager(shell=SH)
    try:
        session = manager.ensure("c1", str(tmp_path))
        queue, _ = manager.attach(session)
        session.write(b"pwd; echo MARK-$((40+2))\n")
        out = await read_until(queue, b"MARK-42")
        assert str(tmp_path.resolve()).encode() in out or str(tmp_path).encode() in out
        manager.detach(session, queue)
        # Closing the panel does not end the shell; the next viewer sees what happened.
        assert manager.ensure("c1", str(tmp_path)) is session
        _, replay = manager.attach(session)
        assert b"MARK-42" in replay
    finally:
        manager.close_all()


async def test_exit_notifies_viewers_and_frees_the_slot(tmp_path: Path) -> None:
    manager = TerminalManager(shell=SH)
    session = manager.ensure("c1", str(tmp_path))
    queue, _ = manager.attach(session)
    session.write(b"exit 3\n")
    while (item := await asyncio.wait_for(queue.get(), 5)) is not None:
        pass
    assert session.exit_code == 3 and manager.get("c1") is None
    assert manager.ensure("c1", str(tmp_path)) is not session
    manager.close_all()


async def test_idle_shell_ends_and_close_hangs_up(tmp_path: Path) -> None:
    manager = TerminalManager(shell=SH, idle_seconds=0.2)
    session = manager.ensure("c1", str(tmp_path))
    queue, _ = manager.attach(session)
    manager.detach(session, queue)
    await asyncio.sleep(0.5)
    assert manager.get("c1") is None
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(session.pid, 0)
            await asyncio.sleep(0.05)


async def test_oldest_detached_shell_makes_room(tmp_path: Path) -> None:
    manager = TerminalManager(shell=SH, max_sessions=2)
    try:
        first = manager.ensure("a", str(tmp_path))
        manager.detach(first, manager.attach(first)[0])
        second = manager.ensure("b", str(tmp_path))
        manager.attach(second)
        manager.ensure("c", str(tmp_path))
        assert manager.get("a") is None and manager.get("b") is second
    finally:
        manager.close_all()


def test_kaus_process_settings_stay_out_of_the_shell() -> None:
    env = shell_environment({"PATH": "/bin", "KAUS_TOKEN_FILE": "x", "DASH_DEBUG": "1", "HOME": "/h"})
    assert env["PATH"] == "/bin" and env["HOME"] == "/h" and env["TERM"] == "xterm-256color"
    assert not any(key.startswith(("KAUS_", "DASH_")) for key in env)


# --- the route --------------------------------------------------------------- #


async def _with_workspace(harness, client, root: Path) -> str:
    conversation_id = harness.new_conversation(client)
    conversation = await harness.repositories.conversations.get(conversation_id)
    await harness.host.emit_conversation_event(
        conversation, ExtensionEvent(namespace="kaus", name="runtime.workspace", data={"workspaceRoot": str(root)}))
    return conversation_id


def _collect(ws, needle: bytes, timeout: float = 5.0) -> bytes:
    seen = b""
    deadline = time.monotonic() + timeout
    while needle not in seen and time.monotonic() < deadline:
        message = ws.receive()
        if message.get("bytes"):
            seen += message["bytes"]
        elif message.get("text") or message.get("type") == "websocket.close":
            break
    return seen


async def test_route_requires_the_token_and_runs_in_the_workspace(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        with _client(harness) as client:
            conversation_id = await _with_workspace(harness, client, workspace)
            url = f"/api/conversations/{conversation_id}/terminal"
            with client.websocket_connect(url) as ws:
                ws.send_json({"type": "auth", "token": "wrong"})
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_bytes()
                assert closed.value.code == 4401
            with pytest.raises(WebSocketDisconnect) as foreign:
                with client.websocket_connect(url, headers={"Origin": "https://evil.example"}) as ws:
                    ws.receive_bytes()
            assert foreign.value.code == 4403
            with client.websocket_connect(url) as ws:
                ws.send_json({"type": "auth", "token": TEST_TOKEN, "cols": 100, "rows": 30})
                ws.send_json({"type": "input", "data": "pwd; echo DONE-$((1+1))\n"})
                out = _collect(ws, b"DONE-2")
                assert b"DONE-2" in out and b"ws" in out
                ws.send_json({"type": "input", "data": "exit\n"})
                for _ in range(200):
                    message = ws.receive()
                    if message.get("text"):
                        assert '"exit"' in message["text"]
                        break
    finally:
        await harness.aclose()


async def test_route_without_a_workspace_closes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            with client.websocket_connect(f"/api/conversations/{conversation_id}/terminal") as ws:
                ws.send_json({"type": "auth", "token": TEST_TOKEN})
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_bytes()
                assert closed.value.code == 4409
    finally:
        await harness.aclose()
