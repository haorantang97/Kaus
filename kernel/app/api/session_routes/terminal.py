"""``WS /conversations/{id}/terminal``: the side panel's workspace shell.

2026-11-23「页头面板」⑥. Browsers cannot put headers on a WebSocket, and the
token must not travel in the URL, so the gate is: ``Origin`` on the handshake
(same allow-list as every session endpoint), then the first frame
``{"type": "auth", "token": ..., "cols": .., "rows": ..}``. Only after both does a
shell start.

Frames from the viewer: ``input`` (``data``, ``binary``), ``resize`` (``cols``, ``rows``).
Frames to the viewer: binary output, and ``{"type": "exit", "code": N}`` once the
shell is gone.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from app.api.session_auth import RequestFacts, SessionAuthError, SessionAuthPolicy
from app.api.session_routes.context import SessionRouteContext
from drivers.terminals.workspace_shell import TerminalManager

TERMINAL_ROUTE_PATH = "/conversations/{conversation_id}/terminal"
AUTH_TIMEOUT = 10.0
MAX_INPUT = 1024 * 1024

#: Application close codes (4000-4999); the viewer maps them to messages.
CLOSE_FORBIDDEN = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_NO_WORKSPACE = 4409


def _size(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and 2 <= value <= 1000 else fallback


def add_terminal_route(router: Any, ctx: SessionRouteContext, auth_policy: SessionAuthPolicy,
                       terminals: TerminalManager | None = None) -> None:
    from fastapi import WebSocket, WebSocketDisconnect

    holder: dict[str, TerminalManager] = {}
    if terminals is not None:
        holder["manager"] = terminals

    def manager() -> TerminalManager:
        if "manager" not in holder:
            holder["manager"] = TerminalManager()
        return holder["manager"]

    async def workspace_terminal(websocket: WebSocket, conversation_id: str) -> None:
        try:
            auth_policy.check_origin(RequestFacts(origin=websocket.headers.get("origin"), host=websocket.headers.get("host")))
        except SessionAuthError:
            await websocket.close(code=CLOSE_FORBIDDEN)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT)
            token = first.get("token") if isinstance(first, dict) and first.get("type") == "auth" else None
            auth_policy.check_token(RequestFacts(authorization=f"Bearer {token}" if isinstance(token, str) else None),
                                    allow_query_token=False)
        except (SessionAuthError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        except WebSocketDisconnect:
            return

        conversation = await ctx.repositories.conversations.get(conversation_id)
        if conversation is None:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return
        cwd = await ctx.conversation_workspace(conversation)
        if not cwd or not os.path.isdir(cwd):
            await websocket.close(code=CLOSE_NO_WORKSPACE)
            return

        terminals_ = manager()
        session = terminals_.ensure(conversation.id, cwd, cols=_size(first.get("cols"), 80), rows=_size(first.get("rows"), 24))
        session.resize(_size(first.get("cols"), 80), _size(first.get("rows"), 24))
        queue, replay = terminals_.attach(session)

        async def pump() -> None:
            if replay:
                await websocket.send_bytes(replay)
            while True:
                item = await queue.get()
                if item is None:
                    await websocket.send_json({"type": "exit", "code": session.exit_code})
                    await websocket.close()
                    return
                chunks = [item]
                # Coalesce what is already queued into one frame.
                while not queue.empty():
                    more = queue.get_nowait()
                    if more is None:
                        queue.put_nowait(None)
                        break
                    chunks.append(more)
                await websocket.send_bytes(b"".join(chunks))

        sender = asyncio.create_task(pump())
        try:
            while not sender.done():
                receive = asyncio.create_task(websocket.receive_json())
                done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
                if receive not in done:
                    receive.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await receive
                    break
                message = receive.result()
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "input" and isinstance(message.get("data"), str) and len(message["data"]) <= MAX_INPUT:
                    # ``binary`` input (some mouse reports) is one byte per character.
                    data = message["data"]
                    session.write(data.encode("latin-1", "replace") if message.get("binary") else data.encode("utf-8", "replace"))
                elif kind == "resize":
                    session.resize(_size(message.get("cols"), 0), _size(message.get("rows"), 0))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            terminals_.detach(session, queue)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    # ``from __future__ import annotations`` turns the hints into strings that FastAPI
    # resolves in module scope, where ``WebSocket`` (imported above, locally) is unknown.
    workspace_terminal.__annotations__ = {"websocket": WebSocket, "conversation_id": str, "return": None}
    router.add_api_websocket_route(TERMINAL_ROUTE_PATH, workspace_terminal, name="session_workspace_terminal")


__all__ = ["TERMINAL_ROUTE_PATH", "add_terminal_route"]
