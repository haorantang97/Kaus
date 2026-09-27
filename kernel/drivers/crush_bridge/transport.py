"""Bounded HTTP/1.1 over a private Unix socket; Python standard library only."""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
from typing import AsyncIterator, Callable

from .protocol import BridgeError

MAX_BODY = 16 * 1024 * 1024


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class NativeHTTP:
    def __init__(self, path: str):
        self.path = path
        self.connections: set[UnixConnection] = set()
        self.lock = threading.Lock()
        self.closed = False

    def connection(self, timeout: float | None) -> UnixConnection:
        connection = UnixConnection(self.path, timeout)
        with self.lock:
            if self.closed:
                raise BridgeError("Crush connection is closed")
            self.connections.add(connection)
        return connection

    def release(self, connection: UnixConnection) -> None:
        connection.close()
        with self.lock:
            self.connections.discard(connection)

    async def request(self, method: str, path: str, body: dict | None = None, timeout: float = 45):
        def perform():
            connection = self.connection(timeout)
            try:
                headers = {"Content-Type": "application/json"} if body is not None else {}
                connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
                response = connection.getresponse()
                if not 200 <= response.status < 300:
                    raise BridgeError(f"Crush API rejected {method} {path.split('?')[0]} (HTTP {response.status})")
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise BridgeError("Crush API response exceeded the size limit")
                return json.loads(data) if data else {}
            finally:
                self.release(connection)
        return await asyncio.to_thread(perform)

    async def events(self, path: str, ready: Callable[[], None]) -> AsyncIterator[dict]:
        connection = self.connection(None)
        try:
            def open_stream():
                connection.request("GET", path, headers={"Accept": "text/event-stream"})
                response = connection.getresponse()
                if response.status != 200:
                    raise BridgeError("Crush event stream was rejected")
                return response
            response = await asyncio.to_thread(open_stream)
            ready()
            lines, size = [], 0
            while raw := await asyncio.to_thread(response.readline, MAX_BODY + 1):
                size += len(raw)
                if size > MAX_BODY:
                    raise BridgeError("Crush event exceeded the size limit")
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif line == "":
                    if lines:
                        yield json.loads("\n".join(lines))
                    lines, size = [], 0
        finally:
            # Wake a blocked reader before closing its response wrapper.
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.release(connection)

    async def aclose(self) -> None:
        with self.lock:
            self.closed = True
            connections = list(self.connections)
        for connection in connections:
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.release(connection)
