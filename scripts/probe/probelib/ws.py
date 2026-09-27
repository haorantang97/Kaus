"""Dependency-free RFC 6455 WebSocket client (plus the server half used by the
self-test fixture).

The probe prefers the third-party ``websockets`` package when it is importable
so that behaviour matches whatever the user's other tooling does; when it is
absent -- the normal case for a stdlib-only environment -- it falls back to the
minimal implementation below.  Which one was used is recorded in the report.

Only what a JSON-RPC control channel needs is implemented: text frames,
continuation frames, ping/pong, close.  No permessage-deflate, no fragmentation
on send.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocketHandshakeError(WebSocketError):
    """Carries the server's rejection so the report can quote it.

    Hermes' `/api/ws` gate answers 403 with a body that names the reason
    (see `_ws_auth_reason` in hermes_cli/web_server.py), so the body is the
    single most useful diagnostic and must never be thrown away.
    """

    def __init__(self, status: int, status_line: str, headers: dict[str, str], body: str):
        self.status = status
        self.status_line = status_line
        self.headers = headers
        self.body = body
        super().__init__(
            f"handshake rejected: {status_line.strip()}"
            + (f" | body: {body[:400]}" if body.strip() else " | (empty body)")
        )


def have_websockets_lib() -> bool:
    try:
        import websockets  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------------
# framing helpers
# --------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketError("socket closed while reading frame")
        buf += chunk
    return buf


def _read_error_body(sock: socket.socket, already: bytes, headers: dict[str, str]) -> str:
    """Drain a rejected handshake's response body (it names the reason)."""
    body = already
    want = 0
    try:
        want = int(headers.get("content-length") or 0)
    except ValueError:
        want = 0
    sock.settimeout(2.0)
    try:
        while (want and len(body) < want) or (not want and len(body) < 8192):
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
    except (OSError, TimeoutError):
        pass
    return body.decode("utf-8", "replace")[:4000]


def _read_frame(sock: socket.socket) -> tuple[int, bytes, bool]:
    head = _recv_exact(sock, 2)
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, fin


def _write_frame(sock: socket.socket, opcode: int, payload: bytes, *, mask: bool) -> None:
    header = bytearray()
    header.append(0x80 | opcode)
    length = len(payload)
    maskbit = 0x80 if mask else 0x00
    if length < 126:
        header.append(maskbit | length)
    elif length < (1 << 16):
        header.append(maskbit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(maskbit | 127)
        header += struct.pack("!Q", length)
    body = payload
    if mask:
        key = os.urandom(4)
        header += key
        body = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + body)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class MiniWebSocket:
    """Blocking text-frame WebSocket client."""

    impl = "builtin"

    def __init__(self, sock: socket.socket, url: str, headers: dict[str, str]):
        self.sock = sock
        self.url = url
        self.response_headers = headers
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        origin: str | None = None,
        subprotocols: list[str] | None = None,
    ) -> "MiniWebSocket":
        """Open a WebSocket.

        ``origin`` is sent **only when explicitly supplied**.  Browsers always
        send one; native clients normally do not, and a wrong Origin is a
        common cause of a 403 on origin-checked gateways -- so the default here
        is to send none and let the caller ladder through candidates.
        """
        parsed = urlparse(url)
        secure = parsed.scheme == "wss"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        if subprotocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("server closed during handshake")
            raw += chunk
            if len(raw) > 65536:
                raise WebSocketError("handshake response too large")
        head, _, rest = raw.partition(b"\r\n\r\n")
        head_text = head.decode("utf-8", "replace")
        status_line = head_text.splitlines()[0] if head_text else ""
        resp_headers = {}
        for line in head_text.splitlines()[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                resp_headers[k.strip().lower()] = v.strip()
        if " 101" not in status_line:
            body = _read_error_body(sock, rest, resp_headers)
            try:
                sock.close()
            except OSError:
                pass
            status = 0
            m = re.search(r"\s(\d{3})\s", status_line + " ")
            if m:
                status = int(m.group(1))
            raise WebSocketHandshakeError(status, status_line, resp_headers, body)
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324
        got = resp_headers.get("sec-websocket-accept")
        if got and got != expected:
            raise WebSocketError("Sec-WebSocket-Accept mismatch")
        sock.settimeout(None)
        return cls(sock, url, resp_headers)

    def send_text(self, text: str) -> None:
        with self._lock:
            _write_frame(self.sock, OP_TEXT, text.encode("utf-8"), mask=True)

    def recv_text(self) -> str | None:
        buf = b""
        opcode_of_message = None
        while True:
            try:
                opcode, payload, fin = _read_frame(self.sock)
            except (WebSocketError, OSError):
                return None
            if opcode == OP_CLOSE:
                try:
                    with self._lock:
                        _write_frame(self.sock, OP_CLOSE, b"", mask=True)
                except OSError:
                    pass
                return None
            if opcode == OP_PING:
                with self._lock:
                    _write_frame(self.sock, OP_PONG, payload, mask=True)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN):
                opcode_of_message = opcode
                buf = payload
            elif opcode == OP_CONT:
                buf += payload
            if fin:
                if opcode_of_message == OP_BIN:
                    return buf.decode("utf-8", "replace")
                return buf.decode("utf-8", "replace")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with self._lock:
                _write_frame(self.sock, OP_CLOSE, b"", mask=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class LibWebSocket:
    """Adapter over the third-party ``websockets`` sync client, when present."""

    impl = "websockets"

    def __init__(self, conn):
        self.conn = conn
        self.response_headers = dict(getattr(conn, "response", None).headers) if getattr(conn, "response", None) else {}

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        origin: str | None = None,
        subprotocols: list[str] | None = None,
    ):
        from websockets.sync.client import connect as _connect  # type: ignore

        extra = dict(headers or {})
        if origin:
            extra["Origin"] = origin
        kwargs = {"additional_headers": extra, "open_timeout": timeout}
        if subprotocols:
            kwargs["subprotocols"] = subprotocols
        conn = _connect(url, **kwargs)
        return cls(conn)

    def send_text(self, text: str) -> None:
        self.conn.send(text)

    def recv_text(self) -> str | None:
        try:
            msg = self.conn.recv()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(msg, bytes):
            return msg.decode("utf-8", "replace")
        return msg

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def connect_ws(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    origin: str | None = None,
    subprotocols: list[str] | None = None,
):
    """Connect, preferring the stdlib implementation, falling back to the lib.

    Returns ``(connection, impl_name, notes)``.  A handshake rejection is
    re-raised as :class:`WebSocketHandshakeError` so the caller can read the
    status and body instead of only a message string.
    """
    notes: list[str] = []
    first_handshake_error: WebSocketHandshakeError | None = None
    try:
        conn = MiniWebSocket.connect(
            url, headers=headers, timeout=timeout, origin=origin, subprotocols=subprotocols
        )
        return conn, conn.impl, notes
    except WebSocketHandshakeError as exc:
        # The server answered; the library client would be told the same thing.
        first_handshake_error = exc
        notes.append(f"builtin client: {exc}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"builtin client failed: {type(exc).__name__}: {exc}")
    if have_websockets_lib():
        try:
            conn = LibWebSocket.connect(
                url, headers=headers, timeout=timeout, origin=origin, subprotocols=subprotocols
            )
            return conn, conn.impl, notes
        except Exception as exc:  # noqa: BLE001
            notes.append(f"websockets lib failed: {type(exc).__name__}: {exc}")
    else:
        notes.append("optional dependency 'websockets' not installed; no fallback available")
    if first_handshake_error is not None:
        raise first_handshake_error
    raise WebSocketError("; ".join(notes))


# --------------------------------------------------------------------------
# server half (used only by the offline fixture)
# --------------------------------------------------------------------------


def server_handshake(sock: socket.socket) -> tuple[str, dict[str, str]]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise WebSocketError("client closed during handshake")
        raw += chunk
    head = raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
    lines = head.splitlines()
    request_line = lines[0] if lines else ""
    path = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else "/"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return path, headers


class ServerSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._lock = threading.Lock()

    def send_text(self, text: str) -> None:
        with self._lock:
            _write_frame(self.sock, OP_TEXT, text.encode("utf-8"), mask=False)

    def recv_text(self) -> str | None:
        buf = b""
        while True:
            try:
                opcode, payload, fin = _read_frame(self.sock)
            except (WebSocketError, OSError):
                return None
            if opcode == OP_CLOSE:
                return None
            if opcode == OP_PING:
                with self._lock:
                    _write_frame(self.sock, OP_PONG, payload, mask=False)
                continue
            if opcode == OP_PONG:
                continue
            buf = payload if opcode in (OP_TEXT, OP_BIN) else buf + payload
            if fin:
                return buf.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
