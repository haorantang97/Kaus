#!/usr/bin/env python3
"""A deliberately small stand-in for the real `hermes` binary.

Its only job is to let `run_probe.py --self-test` exercise every code path in
the probe -- stdio JSON-RPC, WebSocket JSON-RPC, ACP, HTTP+SSE, sqlite
inspection, out-of-band writes -- on a machine that has no Hermes installed.

It is NOT a Hermes emulator and its parameter shapes are guesses.  A green
self-test proves the harness works; it proves nothing about Hermes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probelib.ws import ServerSocket, server_handshake  # noqa: E402

VERSION = "0.9.9-fake-probe-fixture"


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes-fake")))


def db_path() -> Path:
    return home() / "state.db"


def _db() -> sqlite3.Connection:
    home().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "id TEXT PRIMARY KEY, title TEXT, source TEXT, started_at REAL, "
        "ended_at REAL, message_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, token_count INTEGER)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    if not conn.execute("SELECT 1 FROM schema_version").fetchone():
        conn.execute("INSERT INTO schema_version (version) VALUES (23)")
    conn.commit()
    return conn


def new_sid(hexlen: int = 8) -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(hexlen // 2)


def create_session(source: str, title: str = "probe session", hexlen: int = 8) -> str:
    sid = new_sid(hexlen)
    conn = _db()
    conn.execute(
        "INSERT INTO sessions (id, title, source, started_at, message_count) VALUES (?,?,?,?,0)",
        (sid, title, source, time.time()),
    )
    conn.commit()
    conn.close()
    return sid


def add_turn(sid: str, text: str) -> None:
    conn = _db()
    now = time.time()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, token_count) VALUES (?,?,?,?,?)",
        (sid, "user", text, now, 12),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, token_count)"
        " VALUES (?,?,?,?,?,?,?)",
        (sid, "assistant", "", json.dumps([{"id": "call_1", "function": {
            "name": "terminal", "arguments": "{\"command\": \"echo HERMES-PROBE-TOOL-MARKER\"}"}}]),
         "terminal", now + 0.1, 8),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, token_count)"
        " VALUES (?,?,?,?,?,?,?)",
        (sid, "tool", "HERMES-PROBE-TOOL-MARKER", "call_1", "terminal", now + 0.2, 4),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, token_count) VALUES (?,?,?,?,?)",
        (sid, "assistant", "HERMES-PROBE-TOOL-MARKER", now + 0.3, 6),
    )
    conn.execute(
        "UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id=?)"
        " WHERE id=?", (sid, sid))
    conn.commit()
    conn.close()


def list_sessions(limit: int = 50) -> list[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT id,title,source,message_count FROM sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "source": r[2], "message_count": r[3]} for r in rows]


def history(sid: str) -> list[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT role,content,tool_calls,tool_call_id,tool_name,timestamp FROM messages"
        " WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    conn.close()
    return [
        {"role": r[0], "content": r[1], "tool_calls": json.loads(r[2]) if r[2] else None,
         "tool_call_id": r[3], "tool_name": r[4], "timestamp": r[5]}
        for r in rows
    ]


# --------------------------------------------------------------------------
# gateway JSON-RPC core (shared by stdio and websocket)
# --------------------------------------------------------------------------

_ACTIVE: dict[str, set] = {}
_ACTIVE_LOCK = threading.Lock()


def gateway_dispatch(method: str, params: dict | None, send_note) -> tuple[dict | None, dict | None]:
    """Return (result, error)."""
    params = params or {}
    sid = params.get("session_id") or params.get("sessionId") or params.get("id")

    if method == "session.create":
        new = create_session("gateway", params.get("title") or "gateway session", 8)
        return {"session_id": new, "cwd": params.get("cwd")}, None
    if method == "session.list":
        return {"sessions": list_sessions(int(params.get("limit") or 50))}, None
    if method == "session.active_list":
        with _ACTIVE_LOCK:
            return {"active": sorted(_ACTIVE.keys())}, None
    if method == "session.activate":
        if not sid:
            return None, {"code": -32602, "message": "Invalid params: session_id is required"}
        with _ACTIVE_LOCK:
            _ACTIVE.setdefault(sid, set()).add(threading.get_ident())
        return {"session_id": sid, "attached": True}, None
    if method == "session.history":
        if not sid:
            return None, {"code": -32602, "message": "Invalid params: session_id is required"}
        return {"session_id": sid, "messages": history(sid)}, None
    if method == "commands.catalog":
        return {"commands": ["/model", "/new", "/compress", "/title"]}, None
    if method == "session.usage":
        if not sid:
            return None, {"code": -32602, "message": "Invalid params: session_id is required"}
        return {"session_id": sid, "input_tokens": 30, "output_tokens": 14}, None
    if method == "session.status":
        return {"session_id": sid, "state": "idle"}, None
    if method == "session.interrupt":
        if not sid:
            return None, {"code": -32602, "message": "Invalid params: session_id is required"}
        return {"interrupted": True}, None
    if method == "prompt.submit":
        text = params.get("text") or params.get("prompt") or params.get("message")
        if not sid or not text:
            return None, {"code": -32602, "message": "Invalid params: session_id and text required"}
        add_turn(sid, str(text))
        threading.Thread(target=_fake_stream, args=(sid, send_note), daemon=True).start()
        return {"accepted": True}, None
    if method == "session.close":
        return {"closed": True}, None
    return None, {"code": -32601, "message": f"Method not found: {method}"}


def _fake_stream(sid: str, send_note) -> None:
    time.sleep(0.1)
    send_note("message.delta", {"session_id": sid, "text": "HERMES-"})
    send_note("tool.start", {"session_id": sid, "tool": "terminal",
                             "args": {"command": "echo HERMES-PROBE-TOOL-MARKER"}})
    send_note("approval.request", {"session_id": sid, "id": "appr_1", "tool": "terminal"})
    time.sleep(0.1)
    send_note("tool.complete", {"session_id": sid, "tool": "terminal",
                                "output": "HERMES-PROBE-TOOL-MARKER"})
    send_note("message.delta", {"session_id": sid, "text": "PROBE-OK"})
    send_note("message.complete", {"session_id": sid,
                                   "usage": {"input_tokens": 30, "output_tokens": 14}})


# --------------------------------------------------------------------------
# stdio transports
# --------------------------------------------------------------------------


def _stdio_loop(dispatch, banner: dict | None = None) -> int:
    out_lock = threading.Lock()

    def write(obj: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def send_note(method: str, params: dict) -> None:
        write({"jsonrpc": "2.0", "method": method, "params": params})

    print("fake hermes: logs go to stderr only", file=sys.stderr, flush=True)
    if banner:
        write(banner)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "method" not in msg:
            continue
        result, error = dispatch(msg.get("method"), msg.get("params"), send_note)
        if msg.get("id") is None:
            continue
        payload = {"jsonrpc": "2.0", "id": msg["id"]}
        if error:
            payload["error"] = error
        else:
            payload["result"] = result
        write(payload)
    return 0


def cmd_tui_gateway() -> int:
    return _stdio_loop(
        gateway_dispatch,
        banner={"jsonrpc": "2.0", "method": "gateway.ready",
                "params": {"version": VERSION, "transport": "stdio"}},
    )


# --------------------------------------------------------------------------
# ACP
# --------------------------------------------------------------------------

_ACP_SESSIONS: dict[str, dict] = {}


def acp_dispatch(method: str, params: dict | None, send_note):
    params = params or {}
    if method == "initialize":
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False},
                # 0.18.2 advertises these, contradicting the official ACP page.
                "sessionCapabilities": {"fork": True, "list": True, "resume": True},
            },
            "authMethods": [],
        }, None
    if method == "session/list":
        return {"sessions": [{"sessionId": s} for s in _ACP_SESSIONS]}, None
    if method == "session/new":
        # Reproduce the sandbox-without-credentials failure so the probe's
        # "environment problem, not unsupported" classification is covered.
        if os.environ.get("HERMES_FAKE_NO_PROVIDER"):
            return None, {"code": -32603, "message": "No LLM provider configured"}
        sid = create_session("acp", "acp session", 8)
        _ACP_SESSIONS[sid] = {"cwd": params.get("cwd")}
        return {"sessionId": sid}, None
    if method == "session/load":
        sid = params.get("sessionId")
        if sid in _ACP_SESSIONS:
            return {"sessionId": sid}, None
        # cross-process: the in-memory manager does not know it
        return None, {"code": -32602, "message": f"Session not found in this ACP process: {sid}"}
    if method == "session/prompt":
        sid = params.get("sessionId")
        if sid not in _ACP_SESSIONS:
            return None, {"code": -32602, "message": "Unknown sessionId"}
        add_turn(sid, "probe")
        for upd in (
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "HERMES-"}},
            {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "terminal"},
            {"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "completed"},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "PROBE-OK"}},
        ):
            send_note("session/update", {"sessionId": sid, "update": upd})
        return {"stopReason": "end_turn"}, None
    if method == "session/cancel":
        return {}, None
    return None, {"code": -32601, "message": f"Method not found: {method}"}


def cmd_acp() -> int:
    return _stdio_loop(acp_dispatch)


# --------------------------------------------------------------------------
# `serve`: HTTP /api/status + WebSocket /api/ws
# --------------------------------------------------------------------------


def _http_response(sock, status: str, body: bytes, ctype: str = "application/json") -> None:
    sock.sendall(
        f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n".encode() + body
    )


def _read_head(sock) -> tuple[str, str, dict, bytes]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            return "", "", {}, b""
        raw += chunk
        if len(raw) > 262144:
            break
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").splitlines()
    if not lines:
        return "", "", {}, b""
    parts = lines[0].split(" ")
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length") or 0)
    body = rest
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return method, path, headers, body


def cmd_serve(argv: list[str]) -> int:
    port = 9119
    hostname = "127.0.0.1"
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        if a == "--host" and i + 1 < len(argv):
            hostname = argv[i + 1]
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((hostname, port))
    except OSError as exc:
        print(f"bind failed: {exc}", file=sys.stderr, flush=True)
        return 1
    srv.listen(16)
    print(f"HERMES_BACKEND_READY port={port}", flush=True)

    def handle(conn):
        try:
            method, path, headers, _body = _read_head(conn)
            if not method:
                conn.close()
                return
            if path.split("?")[0] == "/api/ws" and "websocket" in headers.get("upgrade", "").lower():
                # Optional origin gate, so the probe's handshake ladder can be
                # exercised offline.  HERMES_FAKE_WS_ORIGIN_MODE:
                #   open (default) - accept anything
                #   strict         - only Origin == http://127.0.0.1:<port>
                #   reject         - always 403 (ladder must exhaust and report)
                mode = os.environ.get("HERMES_FAKE_WS_ORIGIN_MODE", "open")
                origin = headers.get("origin")
                allowed = True
                if mode == "strict":
                    allowed = origin == f"http://127.0.0.1:{port}"
                elif mode == "reject":
                    allowed = False
                if not allowed:
                    body = json.dumps({
                        "error": "forbidden",
                        "reason": "ws_origin_not_allowed",
                        "origin": origin,
                        "hint": f"expected http://127.0.0.1:{port} or no Origin header",
                    }).encode()
                    conn.sendall(
                        ("HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
                        + body)
                    return
                # replay handshake using headers we already read
                import base64
                import hashlib

                key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                conn.sendall(
                    ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode()
                )
                ws = ServerSocket(conn)
                ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "gateway.ready",
                                         "params": {"version": VERSION, "transport": "websocket"}}))

                def send_note(m, p):
                    try:
                        ws.send_text(json.dumps({"jsonrpc": "2.0", "method": m, "params": p}))
                    except OSError:
                        pass

                while True:
                    text = ws.recv_text()
                    if text is None:
                        break
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    result, error = gateway_dispatch(msg.get("method"), msg.get("params"), send_note)
                    if msg.get("id") is None:
                        continue
                    payload = {"jsonrpc": "2.0", "id": msg["id"]}
                    if error:
                        payload["error"] = error
                    else:
                        payload["result"] = result
                    ws.send_text(json.dumps(payload))
                ws.close()
                return
            if path.startswith("/api/status"):
                _http_response(conn, "200 OK",
                               json.dumps({"ok": True, "version": VERSION,
                                           "auth": "none (loopback)"}).encode())
            else:
                _http_response(conn, "404 Not Found", b'{"error":"not found"}')
        except Exception as exc:  # noqa: BLE001
            print(f"serve handler error: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    return 0


# --------------------------------------------------------------------------
# `gateway run`: OpenAI-compatible API server
# --------------------------------------------------------------------------

_RUNS: dict[str, dict] = {}


def _env_file() -> dict:
    out = {}
    p = home() / ".env"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def cmd_gateway(argv: list[str]) -> int:
    envf = _env_file()
    key = os.environ.get("API_SERVER_KEY") or envf.get("API_SERVER_KEY") or ""
    port = int(os.environ.get("API_SERVER_PORT") or envf.get("API_SERVER_PORT") or 8642)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as exc:
        print(f"bind failed: {exc}", file=sys.stderr, flush=True)
        return 1
    srv.listen(16)
    print(f"api server listening on 127.0.0.1:{port}", flush=True)

    def authed(headers) -> bool:
        return bool(key) and headers.get("authorization", "") == f"Bearer {key}"

    def handle(conn):
        try:
            method, raw_path, headers, body = _read_head(conn)
            path = raw_path.split("?")[0]
            if not method:
                return
            if path in ("/health", "/v1/health"):
                _http_response(conn, "200 OK", b'{"status":"ok"}')
                return
            if not authed(headers):
                _http_response(conn, "401 Unauthorized", b'{"error":"missing bearer token"}')
                return
            payload = {}
            if body:
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    payload = {}

            if path == "/v1/capabilities":
                _http_response(conn, "200 OK", json.dumps({
                    "version": VERSION, "protocol": "openai-compatible",
                    "endpoints": ["/v1/chat/completions", "/v1/responses", "/v1/models",
                                  "/v1/runs", "/v1/runs/{id}/events", "/v1/runs/{id}/stop",
                                  "/v1/runs/{id}/approval", "/api/sessions"],
                    "events": ["assistant.delta", "tool.started", "tool.completed", "run.completed"],
                }).encode())
                return
            if path == "/v1/models":
                _http_response(conn, "200 OK", json.dumps(
                    {"object": "list", "data": [{"id": "hermes-agent", "object": "model"}]}).encode())
                return
            if path == "/api/sessions" and method == "GET":
                _http_response(conn, "200 OK", json.dumps({"sessions": list_sessions()}).encode())
                return
            if path == "/api/sessions" and method == "POST":
                # Mirrors the real 0.18.2 shape: the id is NESTED under
                # "session" and uses the api_<ts>_<hex> form, not the CLI's
                # YYYYMMDD_HHMMSS_<hex>.  A probe that reads the top-level "id"
                # gets None -- which is exactly the bug this reproduces.
                sid = f"api_{int(time.time())}_{secrets.token_hex(4)}"
                conn_db = _db()
                conn_db.execute(
                    "INSERT INTO sessions (id, title, source, started_at, message_count)"
                    " VALUES (?,?,?,?,0)",
                    (sid, payload.get("title") or "api session", "api_server", time.time()),
                )
                conn_db.commit()
                conn_db.close()
                conn.sendall(
                    ("HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                     f"X-Hermes-Session-Id: {sid}\r\n").encode())
                b = json.dumps({
                    "object": "hermes.session",
                    "session": {"id": sid, "source": "api_server",
                                "title": payload.get("title") or "api session"},
                }).encode()
                conn.sendall(f"Content-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)
                return
            m = re.match(r"^/api/sessions/([^/]+)/messages$", path)
            if m:
                _http_response(conn, "200 OK",
                               json.dumps({"messages": history(m.group(1))}).encode())
                return
            if path == "/v1/runs" and method == "POST":
                sid = payload.get("session_id") or create_session("api", "run session", 8)
                add_turn(sid, str(payload.get("input") or ""))
                rid = "run_" + secrets.token_hex(6)
                _RUNS[rid] = {"session_id": sid, "status": "completed",
                              "usage": {"input_tokens": 30, "output_tokens": 14}}
                b = json.dumps({"run_id": rid, "status": "started"}).encode()
                conn.sendall(
                    ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     f"X-Hermes-Session-Id: {sid}\r\nContent-Length: {len(b)}\r\n"
                     "Connection: close\r\n\r\n").encode() + b)
                return
            m = re.match(r"^/v1/runs/([^/]+)/events$", path)
            if m:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
                for name, data in (
                    ("assistant.delta", {"text": "HERMES-"}),
                    ("tool.started", {"tool": "terminal"}),
                    ("tool.completed", {"tool": "terminal", "output": "HERMES-PROBE-TOOL-MARKER"}),
                    ("assistant.delta", {"text": "PROBE-OK"}),
                    ("run.completed", {"usage": {"input_tokens": 30, "output_tokens": 14}}),
                ):
                    conn.sendall(
                        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                    time.sleep(0.05)
                return
            m = re.match(r"^/v1/runs/([^/]+)/stop$", path)
            if m:
                _http_response(conn, "200 OK", b'{"status":"stopping"}')
                return
            m = re.match(r"^/v1/runs/([^/]+)/approval$", path)
            if m:
                _http_response(conn, "409 Conflict", b'{"error":"no pending approval"}')
                return
            m = re.match(r"^/v1/runs/([^/]+)$", path)
            if m:
                _http_response(conn, "200 OK", json.dumps(
                    _RUNS.get(m.group(1), {"status": "unknown"})).encode())
                return
            _http_response(conn, "404 Not Found", b'{"error":"not found"}')
        except Exception as exc:  # noqa: BLE001
            print(f"gateway handler error: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    while True:
        try:
            c, _a = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(c,), daemon=True).start()
    return 0


# --------------------------------------------------------------------------
# sessions / chat / profile
# --------------------------------------------------------------------------


def cmd_sessions(argv: list[str]) -> int:
    if not argv:
        print("usage: hermes sessions <list|export|rename|delete>")
        return 2
    verb, rest = argv[0], argv[1:]
    if verb == "list":
        for s in list_sessions():
            print(f"{s['id']}  {s['source']:<8} {s['message_count']:>3}  {s['title']}")
        return 0
    if verb == "export":
        if not rest:
            print("usage: hermes sessions export <file> [--session-id ID]", file=sys.stderr)
            return 2
        dest = Path(rest[0])
        sid = None
        if "--session-id" in rest:
            sid = rest[rest.index("--session-id") + 1]
        rows = history(sid) if sid else []
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"exported {len(rows)} messages to {dest}")
        return 0
    if verb == "rename":
        if len(rest) < 2:
            print("usage: hermes sessions rename <id> <title>", file=sys.stderr)
            return 2
        sid, title = rest[0], " ".join(rest[1:])
        conn = _db()
        cur = conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
        conn.commit()
        changed = cur.rowcount
        conn.close()
        if not changed:
            print(f"Session not found: {sid}", file=sys.stderr)
            return 1
        print(f"renamed {sid} -> {title}")
        return 0
    print(f"unknown sessions verb: {verb}", file=sys.stderr)
    return 2


def cmd_chat(argv: list[str]) -> int:
    sid = None
    oneshot = False
    for i, a in enumerate(argv):
        if a in ("--resume", "-r") and i + 1 < len(argv):
            sid = argv[i + 1]
        if a == "--oneshot":
            oneshot = True
    if sid:
        conn = _db()
        found = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        conn.close()
        if not found:
            print(f"Session not found: {sid}", file=sys.stderr)
            return 1
        print(f"Resuming session {sid}")
    else:
        sid = create_session("cli", "cli session", 6)
        print(f"Started session {sid}")
    if oneshot:
        add_turn(sid, "oneshot")
        print("CLI-PROBE-OK")
        return 0
    # interactive: read until stdin closes
    for _line in sys.stdin:
        pass
    return 0


HELP = f"""hermes {VERSION} (fake fixture)

Usage: hermes [OPTIONS] COMMAND [ARGS]...

Options:
  -V, --version          Show version
  -p, --profile TEXT     Select profile
  -r, --resume TEXT      Resume a session
  -c, --continue         Continue latest session
  --tui                  Launch the TUI

Commands:
  acp                    Run Hermes as an ACP server (stdio)
  chat                   Interactive or one-shot conversation
  gateway                Messaging gateway / OpenAI-compatible API server
  profile                Manage isolated Hermes instances
  serve                  Start headless backend server
  sessions               Browse, export, prune, rename sessions
  tui-gateway            Run the TUI gateway on stdio
"""


def main(argv: list[str]) -> int:
    args = list(argv)
    # strip a leading `-p <name>` the way the real CLI accepts it anywhere
    while len(args) >= 2 and args[0] in ("-p", "--profile"):
        os.environ["HERMES_HOME"] = str(
            Path(os.path.expanduser("~/.hermes")) / "profiles" / args[1])
        args = args[2:]
    if not args or args[0] in ("--help", "-h"):
        print(HELP)
        return 0
    if args[0] in ("--version", "-V"):
        print(f"hermes {VERSION}")
        return 0
    cmd, rest = args[0], args[1:]
    if rest and rest[-1] in ("--help", "-h"):
        print(f"Usage: hermes {cmd} [OPTIONS]\n\nOptions:\n  --host TEXT\n  --port INTEGER\n"
              "  --session-id TEXT\n  --limit INTEGER\n  --resume TEXT\n  --oneshot\n  -q TEXT\n"
              "  --yes\n  --help\n\nCommands:\n  run    Run in the foreground\n")
        return 0
    if cmd == "acp":
        return cmd_acp()
    if cmd == "tui-gateway":
        return cmd_tui_gateway()
    if cmd == "serve":
        return cmd_serve(rest)
    if cmd == "gateway":
        return cmd_gateway(rest)
    if cmd == "sessions":
        return cmd_sessions(rest)
    if cmd == "chat":
        return cmd_chat(rest)
    if cmd == "profile":
        print("fake: profile command is a no-op")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
