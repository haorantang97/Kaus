"""JSON-RPC 2.0 client over stdio or WebSocket.

Two things this module deliberately does *not* assume:

1. **Framing.**  Hermes' docs say the TUI gateway speaks "JSON-RPC over stdio"
   but never state whether the framing is newline-delimited JSON (NDJSON) or
   LSP-style ``Content-Length`` headers.  The reader auto-detects: it peeks for
   a ``Content-Length:`` header and otherwise treats the stream as NDJSON.
   Both directions are recorded so the report can state which one is real.
2. **Parameter shapes.**  The published method catalogue lists names only.
   :meth:`JsonRpcClient.call_variants` therefore tries a list of candidate
   parameter objects and keeps the server's own error text as evidence -- an
   "Invalid params: missing sessionId" reply is a *finding*, not a failure.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from .util import ProbeError, clip, strip_ansi

FRAMING_NDJSON = "ndjson"
FRAMING_HEADERS = "content-length"
FRAMING_UNKNOWN = "unknown"
FRAMING_WS = "websocket-message (one JSON-RPC message per text frame)"


class Transport:
    """Minimal duplex byte/line transport."""

    name = "transport"

    def send_text(self, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def stderr_text(self) -> str:
        return ""


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------


class StdioTransport(Transport):
    name = "stdio"

    def __init__(self, argv: Sequence[str], env: dict | None = None, cwd: str | None = None):
        self.argv = [str(a) for a in argv]
        self.proc = subprocess.Popen(  # noqa: S603
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            bufsize=0,
        )
        self._err_chunks: list[str] = []
        self._err_lock = threading.Lock()
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            with self._err_lock:
                if len(self._err_chunks) < 400:
                    self._err_chunks.append(strip_ansi(raw.decode("utf-8", "replace")))

    def send_text(self, text: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text.encode("utf-8"))
        self.proc.stdin.flush()

    def read_exact(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def read_line(self) -> bytes:
        assert self.proc.stdout is not None
        return self.proc.stdout.readline()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stderr_text(self) -> str:
        with self._err_lock:
            return clip("".join(self._err_chunks), 6000)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# JSON-RPC client
# --------------------------------------------------------------------------


class JsonRpcClient:
    """Speaks JSON-RPC 2.0 and keeps every inbound notification."""

    def __init__(self, transport: Transport, *, reader: Callable[[], str | None] | None = None):
        self.transport = transport
        self._reader = reader
        self._id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._notifications: list[dict] = []
        self._server_requests: list[dict] = []
        self._raw_in: list[str] = []
        self._raw_out: list[str] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        # A WebSocket carries exactly one JSON-RPC message per text frame, so
        # there is no stdio-style framing question to auto-detect.
        self.framing = FRAMING_WS if reader is not None else FRAMING_UNKNOWN
        self.read_error: str | None = None
        self._notify_hooks: list[Callable[[dict], None]] = []
        self._pump = threading.Thread(target=self._read_loop, daemon=True)
        self._pump.start()

    # -- framing -----------------------------------------------------------
    def _read_message_stdio(self) -> str | None:
        t = self.transport
        assert isinstance(t, StdioTransport)
        line = t.read_line()
        if not line:
            return None
        text = line.decode("utf-8", "replace")
        low = text.lower()
        if low.startswith("content-length:"):
            self.framing = FRAMING_HEADERS
            length = int(text.split(":", 1)[1].strip())
            # consume until blank line
            while True:
                nxt = t.read_line()
                if not nxt or nxt in (b"\r\n", b"\n"):
                    break
            return t.read_exact(length).decode("utf-8", "replace")
        stripped = text.strip()
        if not stripped:
            return ""
        if self.framing is FRAMING_UNKNOWN:
            self.framing = FRAMING_NDJSON
        return stripped

    def _read_message(self) -> str | None:
        if self._reader is not None:
            return self._reader()
        return self._read_message_stdio()

    # -- pump --------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                raw = self._read_message()
            except Exception as exc:  # noqa: BLE001
                self.read_error = f"{type(exc).__name__}: {exc}"
                break
            if raw is None:
                break
            if not raw.strip():
                continue
            with self._lock:
                if len(self._raw_in) < 500:
                    self._raw_in.append(clip(raw, 2000))
            try:
                msg = json.loads(raw)
            except ValueError:
                # Non-JSON on the protocol stream is itself a finding.
                with self._lock:
                    self._notifications.append({"__non_json__": clip(raw, 500)})
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Any) -> None:
        if isinstance(msg, list):
            for item in msg:
                self._dispatch(item)
            return
        if not isinstance(msg, dict):
            return
        mid = msg.get("id")
        if "method" in msg and mid is None:
            with self._lock:
                self._notifications.append(msg)
            for hook in list(self._notify_hooks):
                try:
                    hook(msg)
                except Exception:  # noqa: BLE001
                    pass
            return
        if "method" in msg and mid is not None:
            # server -> client request (permission prompts, fs access, ...)
            with self._lock:
                self._server_requests.append(msg)
            for hook in list(self._notify_hooks):
                try:
                    hook(msg)
                except Exception:  # noqa: BLE001
                    pass
            return
        if mid is not None:
            q = self._pending.get(mid if isinstance(mid, int) else -1)
            if q is not None:
                q.put(msg)

    def on_notification(self, hook: Callable[[dict], None]) -> None:
        self._notify_hooks.append(hook)

    # -- calls -------------------------------------------------------------
    def _write(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            if len(self._raw_out) < 500:
                self._raw_out.append(clip(text, 2000))
        if self.framing == FRAMING_HEADERS:
            body = text.encode("utf-8")
            self.transport.send_text(f"Content-Length: {len(body)}\r\n\r\n")
            self.transport.send_text(text)
        elif self.framing == FRAMING_WS:
            # one message per frame -- no delimiter
            self.transport.send_text(text)
        else:
            self.transport.send_text(text + "\n")

    def notify(self, method: str, params: Any = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def call(self, method: str, params: Any = None, timeout: float = 15.0) -> dict:
        self._id += 1
        mid = self._id
        q: queue.Queue = queue.Queue(maxsize=1)
        self._pending[mid] = q
        payload: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        try:
            return q.get(timeout=timeout)
        except queue.Empty as exc:
            raise ProbeError(
                f"{method}: no JSON-RPC response within {timeout:g}s"
                + (f" (read error: {self.read_error})" if self.read_error else "")
            ) from exc
        finally:
            self._pending.pop(mid, None)

    def respond(self, request_id: Any, result: Any = None, error: Any = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        self._write(payload)

    def call_variants(
        self,
        method: str,
        variants: Sequence[Any],
        timeout: float = 15.0,
    ) -> tuple[dict | None, list[dict]]:
        """Try each params shape until one returns a non-error result.

        Returns ``(winning_response_or_None, attempts)`` where each attempt is
        ``{"params": ..., "response": ...}`` -- the failures are kept because
        the server's error text usually names the parameters it wanted.
        """
        attempts: list[dict] = []
        for params in variants:
            try:
                resp = self.call(method, params, timeout=timeout)
            except ProbeError as exc:
                attempts.append({"params": params, "error_local": str(exc)})
                continue
            attempts.append({"params": params, "response": resp})
            if isinstance(resp, dict) and "error" not in resp:
                return resp, attempts
        return None, attempts

    # -- inspection --------------------------------------------------------
    def notifications(self) -> list[dict]:
        with self._lock:
            return list(self._notifications)

    def server_requests(self) -> list[dict]:
        with self._lock:
            return list(self._server_requests)

    def raw_in(self, n: int = 20) -> list[str]:
        with self._lock:
            return self._raw_in[:n]

    def wait_for(
        self,
        predicate: Callable[[dict], bool],
        timeout: float = 20.0,
        poll: float = 0.05,
    ) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self.notifications() + self.server_requests():
                if predicate(msg):
                    return msg
            time.sleep(poll)
        return None

    def collect_methods(self) -> list[str]:
        seen: list[str] = []
        for msg in self.notifications() + self.server_requests():
            m = msg.get("method")
            if isinstance(m, str) and m not in seen:
                seen.append(m)
        return seen

    def close(self) -> None:
        self._closed.set()
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001
            pass


def rpc_error(resp: dict | None) -> str | None:
    if not isinstance(resp, dict):
        return "no response"
    err = resp.get("error")
    if err is None:
        return None
    if isinstance(err, dict):
        return f"{err.get('code')}: {err.get('message')}" + (
            f" | data={json.dumps(err.get('data'), ensure_ascii=False)[:300]}" if err.get("data") else ""
        )
    return str(err)


def rpc_result(resp: dict | None) -> Any:
    if isinstance(resp, dict):
        return resp.get("result")
    return None
