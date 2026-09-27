"""Small bidirectional ACP stdio endpoint, independent of the native process."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Awaitable, Callable

MAX_FRAME = 8 * 1024 * 1024


class BridgeError(Exception):
    def __init__(self, message: str, code: int = -32603):
        super().__init__(message)
        self.code = code


class Peer:
    def __init__(self, write: Callable[[dict], None] | None = None):
        self.write = write or self._stdout
        self.pending: dict[str, asyncio.Future] = {}
        self.counter = 0

    @staticmethod
    def _stdout(frame: dict) -> None:
        sys.stdout.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def notify(self, method: str, params: dict) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    async def call(self, method: str, params: dict) -> dict:
        self.counter += 1
        ident = f"bridge-{self.counter}"
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        self.write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        try:
            return await future
        finally:
            self.pending.pop(ident, None)

    def response(self, frame: dict) -> None:
        future = self.pending.get(frame.get("id"))
        if future is None or future.done():
            return
        if frame.get("error"):
            future.set_exception(BridgeError("The client could not answer the interaction", -32601))
        else:
            future.set_result(frame.get("result") or {})

    async def serve(self, dispatch: Callable[[str, dict], Awaitable[dict]]) -> None:
        reader = asyncio.StreamReader(limit=MAX_FRAME)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        tasks: set[asyncio.Task] = set()

        async def handle(frame: dict) -> None:
            ident = frame.get("id")
            try:
                result = await dispatch(frame["method"], frame.get("params") or {})
                if ident is not None:
                    self.write({"jsonrpc": "2.0", "id": ident, "result": result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if ident is not None:
                    self.write({"jsonrpc": "2.0", "id": ident, "error": {
                        "code": exc.code if isinstance(exc, BridgeError) else -32603,
                        "message": str(exc) if isinstance(exc, BridgeError) else "Native bridge operation failed",
                    }})

        try:
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    self.write({"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32700, "message": "Invalid JSON frame",
                    }})
                    continue
                if "method" not in frame:
                    self.response(frame)
                    continue
                task = asyncio.create_task(handle(frame))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            transport.close()
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
