"""Q1 (TUI Gateway, both transports) + Q4 (multi-client attach).

The method catalogue is published; the *parameter shapes are not*.  Every call
therefore goes through ``call_variants`` with a small list of plausible shapes,
and whatever the server answers -- including "Invalid params: sessionId is
required" -- is captured verbatim.  A run against a real Hermes turns this file
into a parameter reference.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import discovery
from .httpc import request, wait_for_http
from .rpcio import JsonRpcClient, StdioTransport, Transport, rpc_error, rpc_result
from .util import (
    Ctx,
    ProbeError,
    Result,
    clip,
    extract_session_id,
    find_session_ids,
    first_str,
    free_port,
    jdump,
    run_cmd,
    strip_ansi,
)
from .ws import WebSocketError, WebSocketHandshakeError, connect_ws, have_websockets_lib


def _reconnect_ws(ctx: Ctx, url: str, timeout: float = 15.0):
    """Open another connection using the handshake shape that already worked."""
    v = ctx.facts.get("_ws_variant") or {}
    return connect_ws(
        url, timeout=timeout, origin=v.get("origin"), subprotocols=v.get("subprotocols")
    )

PROBE_PROMPT = "Reply with exactly: HERMES-PROBE-OK"
PROBE_TOOL_PROMPT = (
    "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and then reply with the output."
)

# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------


class WsTransport(Transport):
    name = "websocket"

    def __init__(self, conn, impl: str):
        self.conn = conn
        self.impl = impl

    def send_text(self, text: str) -> None:
        self.conn.send_text(text)

    def read(self) -> str | None:
        return self.conn.recv_text()

    def close(self) -> None:
        self.conn.close()


def _stdio_candidates(ctx: Ctx) -> list[tuple[str, list[str]]]:
    """Ordered guesses for "run the TUI gateway on stdio".

    Nothing in the published docs names this command, so the probe tries the
    plausible spellings and reports which one answered.  ``--tui-stdio-cmd``
    overrides the list entirely.
    """
    hermes = ctx.facts.get("hermes_bin", "hermes")
    if ctx.opts.tui_stdio_cmd:
        import shlex

        return [("--tui-stdio-cmd", shlex.split(ctx.opts.tui_stdio_cmd))]

    def hcmd(args: list[str]) -> list[str]:
        # keeps `-p <probe profile>` in front in profile mode
        return ctx.sandbox.hermes_argv(hermes, args)

    cands: list[tuple[str, list[str]]] = []
    subs = ctx.facts.get("subcommands") or []
    for name in ("tui-gateway", "tui_gateway", "gateway-stdio"):
        if name in subs:
            cands.append((f"hermes {name}", hcmd([name])))
    mods = ctx.facts.get("python_modules") or {}
    interp = ctx.facts.get("hermes_python") or ""
    # `python -m ...` bypasses the CLI, so it never sees `-p`.  Only offer it
    # when HERMES_HOME alone is enough to scope it to the sandbox -- otherwise
    # it would run against the user's default home.
    if ctx.sandbox.mode == "isolated":
        if interp and mods.get("tui_gateway.server"):
            cands.append(("python -m tui_gateway.server", [interp, "-m", "tui_gateway.server"]))
        if interp and mods.get("tui_gateway"):
            cands.append(("python -m tui_gateway", [interp, "-m", "tui_gateway"]))
    # last-resort spellings that cost nothing to try
    cands.append(("hermes tui-gateway", hcmd(["tui-gateway"])))
    cands.append(("hermes gateway --stdio", hcmd(["gateway", "--stdio"])))
    # de-duplicate while preserving order
    seen: set[tuple[str, ...]] = set()
    out = []
    for label, argv in cands:
        key = tuple(argv)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, argv))
    return out


# --------------------------------------------------------------------------
# the shared method battery
# --------------------------------------------------------------------------

def _sid_variants(sid: str) -> list[dict]:
    return [{"session_id": sid}, {"sessionId": sid}, {"id": sid}]


def _prompt_variants(sid: str, text: str) -> list[dict]:
    return [
        {"session_id": sid, "text": text},
        {"sessionId": sid, "text": text},
        {"session_id": sid, "prompt": text},
        {"sessionId": sid, "prompt": text},
        {"session_id": sid, "message": text},
        {"session_id": sid, "content": text},
        {"text": text},
    ]


def _extract_sid(payload: Any) -> str | None:
    sid, _how = extract_session_id(payload)
    return sid


def _record_call(
    ctx: Ctx,
    prefix: str,
    path: str,
    rid: str,
    question: str,
    title: str,
    method: str,
    resp: dict | None,
    attempts: list[dict],
    *,
    on_ok: Callable[[Result, Any], None] | None = None,
) -> Any:
    r = ctx.result(f"{prefix}.{rid}", question, path, title)
    r.add("json", f"{method} attempts", [
        {
            "params": a.get("params"),
            "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local"),
            "result_preview": clip(jdump(rpc_result(a.get("response")), 900), 900)
            if "response" in a and rpc_error(a.get("response")) is None
            else None,
        }
        for a in attempts
    ])
    if resp is None:
        errs = [
            rpc_error(a.get("response")) if "response" in a else a.get("error_local")
            for a in attempts
        ]
        joined = " | ".join(str(e) for e in errs if e)[:600]
        # A "method not found" is a capability answer; anything else is a miss.
        if any("-32601" in str(e) or "not found" in str(e).lower() or "unknown method" in str(e).lower()
               for e in errs):
            r.unsupported(f"{method}: 服务端报方法不存在 — {joined}")
        elif errs and any(e for e in errs):
            r.unsupported(f"{method}: 所有参数形状均被拒绝 — {joined}")
        else:
            r.untested(f"{method}: 无响应")
        return None
    result = rpc_result(resp)
    r.measured(f"{method} OK")
    r.add("json", f"{method} result", clip(jdump(result, 2500), 2500))
    if on_ok:
        on_ok(r, result)
    return result


def run_gateway_battery(
    ctx: Ctx,
    client: JsonRpcClient,
    *,
    path: str,
    prefix: str,
    banner_wait: float = 4.0,
) -> dict:
    """Run the TUI-Gateway method matrix over an already-connected client."""
    out: dict = {"session_id": None, "notification_methods": [], "framing": None}

    # 0. handshake / banner ------------------------------------------------
    hr = ctx.result(f"{prefix}.handshake", "Q1", path, "握手 / gateway.ready 通知")
    ready = client.wait_for(lambda m: isinstance(m.get("method"), str), timeout=banner_wait)
    hr.add("json", "first inbound messages", client.raw_in(8))
    out["framing"] = client.framing
    if ready:
        hr.measured(f"收到通知 {ready.get('method')}；framing={client.framing}")
        hr.add("json", "ready payload", clip(jdump(ready, 1500), 1500))
    else:
        hr.untested(
            f"{banner_wait:g}s 内没有任何主动通知（不代表失败：可能要先调用方法）；"
            f"framing={client.framing}"
        )

    # 1. session.create ----------------------------------------------------
    cwd = str(ctx.sandbox.workdir) if ctx.sandbox else "."
    resp, attempts = client.call_variants(
        "session.create", [{}, {"cwd": cwd}, {"cwd": cwd, "title": "hermes-probe"}]
    )
    created = _record_call(
        ctx, prefix, path, "session.create", "Q1", "session.create（新建会话）",
        "session.create", resp, attempts,
    )
    sid = _extract_sid(created) if created is not None else None
    if sid:
        out["session_id"] = sid
        ctx.log(f"      session_id = {sid}")

    # 2. session.list ------------------------------------------------------
    resp, attempts = client.call_variants("session.list", [{}, {"limit": 20}, None])
    listed = _record_call(
        ctx, prefix, path, "session.list", "Q1", "session.list（列出会话）",
        "session.list", resp, attempts,
    )
    if listed is not None and not sid:
        ids = find_session_ids(listed)
        if ids:
            sid = ids[0]
            out["session_id"] = sid

    # 3. session.active_list ----------------------------------------------
    resp, attempts = client.call_variants("session.active_list", [{}, None])
    _record_call(
        ctx, prefix, path, "session.active_list", "Q4", "session.active_list（活跃会话集合）",
        "session.active_list", resp, attempts,
    )

    # 4. session.activate (attach) ----------------------------------------
    if sid:
        resp, attempts = client.call_variants("session.activate", _sid_variants(sid))
        _record_call(
            ctx, prefix, path, "session.activate", "Q1", "session.activate（attach 已有会话 = resume）",
            "session.activate", resp, attempts,
        )
    else:
        ctx.result(f"{prefix}.session.activate", "Q1", path, "session.activate（attach 已有会话 = resume）") \
            .untested("没有拿到 session id，无法测试 activate")

    # 5. session.history ---------------------------------------------------
    if sid:
        resp, attempts = client.call_variants(
            "session.history",
            _sid_variants(sid) + [{"session_id": sid, "limit": 50}],
        )
        hist = _record_call(
            ctx, prefix, path, "session.history", "Q1", "session.history（历史重建）",
            "session.history", resp, attempts,
        )
        out["history"] = hist
    else:
        ctx.result(f"{prefix}.session.history", "Q1", path, "session.history（历史重建）") \
            .untested("没有拿到 session id")

    # 6. commands.catalog --------------------------------------------------
    resp, attempts = client.call_variants("commands.catalog", [{}, None])
    _record_call(
        ctx, prefix, path, "commands.catalog", "Q1", "commands.catalog（斜杠命令目录 = 能力发现）",
        "commands.catalog", resp, attempts,
    )

    # 7. prompt.submit + streaming ----------------------------------------
    stream_r = ctx.result(f"{prefix}.stream", "Q1", path, "文本流 / tool 事件 / permission 请求")
    if not sid:
        stream_r.untested("没有拿到 session id")
    elif not ctx.opts.live:
        stream_r.untested("未开 --live：prompt.submit 会真实调用模型并计费，默认不执行")
    else:
        before = len(client.notifications())
        resp, attempts = client.call_variants(
            "prompt.submit", _prompt_variants(sid, PROBE_TOOL_PROMPT), timeout=30
        )
        stream_r.add("json", "prompt.submit attempts", [
            {"params": a.get("params"),
             "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
            for a in attempts
        ])
        if resp is None:
            stream_r.unsupported("prompt.submit 的所有参数形状都被拒绝（见 attempts）")
        else:
            deadline = time.time() + 90
            while time.time() < deadline:
                methods = client.collect_methods()
                if any(m.endswith("complete") or m.endswith("completed") or m == "run.completed"
                       for m in methods):
                    break
                time.sleep(0.25)
            methods = client.collect_methods()
            out["notification_methods"] = methods
            stream_r.add("json", "notification methods observed", methods)
            stream_r.add("json", "sample notifications",
                         clip(jdump(client.notifications()[before:before + 25], 4000), 4000))
            stream_r.measured("观察到通知: " + ", ".join(methods[:20]))

            # permission / approval requests
            ar = ctx.result(f"{prefix}.approval", "Q1", path, "permission / question 请求（服务端→客户端）")
            reqs = client.server_requests()
            approval_notes = [
                m for m in client.notifications()
                if isinstance(m.get("method"), str)
                and re.search(r"approval|clarify|sudo|secret|permission", m["method"])
            ]
            if reqs or approval_notes:
                ar.measured(
                    "收到: "
                    + ", ".join(sorted({str(m.get("method")) for m in reqs + approval_notes}))
                )
                ar.add("json", "requests", clip(jdump((reqs + approval_notes)[:8], 3000), 3000))
                target = (reqs + approval_notes)[0]
                iid = first_str(target, ("id", "request_id", "approval_id", "interaction_id"))
                resolve = ctx.result(f"{prefix}.approval.respond", "Q1", path,
                                     "approval.respond（回传决策）")
                if target in reqs and target.get("id") is not None:
                    client.respond(target["id"], {"outcome": "denied", "decision": "deny"})
                    resolve.measured("已按 JSON-RPC request/response 回传 deny")
                elif iid:
                    resp2, att2 = client.call_variants(
                        "approval.respond",
                        [
                            {"id": iid, "decision": "deny"},
                            {"request_id": iid, "decision": "deny"},
                            {"id": iid, "approved": False},
                            {"id": iid, "outcome": "deny"},
                        ],
                    )
                    resolve.add("json", "attempts", [
                        {"params": a.get("params"),
                         "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
                        for a in att2
                    ])
                    if resp2 is None:
                        resolve.unsupported("approval.respond 的所有参数形状都被拒绝")
                    else:
                        resolve.measured("approval.respond OK")
                else:
                    resolve.untested("请求里找不到可回传的 id 字段")
            else:
                ar.untested(
                    "本轮没有触发权限请求 —— 可能是模型没调工具，或该 profile 未开审批门；"
                    "不能据此判定不支持"
                )

    # 8. session.usage -----------------------------------------------------
    if sid:
        resp, attempts = client.call_variants("session.usage", _sid_variants(sid) + [{}])
        _record_call(
            ctx, prefix, path, "session.usage", "Q1", "session.usage（token / 费用）",
            "session.usage", resp, attempts,
        )
        resp, attempts = client.call_variants("session.status", _sid_variants(sid) + [{}])
        _record_call(
            ctx, prefix, path, "session.status", "Q1", "session.status",
            "session.status", resp, attempts,
        )
    else:
        ctx.result(f"{prefix}.session.usage", "Q1", path, "session.usage（token / 费用）") \
            .untested("没有拿到 session id")

    # 9. session.interrupt -------------------------------------------------
    ir = ctx.result(f"{prefix}.session.interrupt", "Q1", path, "session.interrupt（打断）")
    if not sid:
        ir.untested("没有拿到 session id")
    else:
        resp, attempts = client.call_variants("session.interrupt", _sid_variants(sid) + [{}])
        ir.add("json", "attempts", [
            {"params": a.get("params"),
             "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
            for a in attempts
        ])
        if resp is None:
            ir.unsupported("session.interrupt 的所有参数形状都被拒绝")
        else:
            ir.measured("session.interrupt 接受（注意：空闲会话上的 interrupt 是 no-op，"
                        "真正的打断语义需在 --live 且有在途回合时验证")
            ir.add("json", "result", clip(jdump(rpc_result(resp), 1200), 1200))

    return out


# --------------------------------------------------------------------------
# path A: stdio
# --------------------------------------------------------------------------


def check_tui_stdio(ctx: Ctx) -> dict:
    path = "tui_stdio"
    out: dict = {}
    if ctx.facts.get("hermes_missing"):
        ctx.result("tui_stdio.launch", "Q1", path, "TUI Gateway stdio 启动").untested("hermes 未安装")
        return out

    cands = _stdio_candidates(ctx)
    lr = ctx.result("tui_stdio.launch", "Q1", path, "TUI Gateway stdio 启动方式（文档未给出命令，靠试）")
    lr.add("json", "candidates", [{"label": lbl, "argv": argv} for lbl, argv in cands])

    if ctx.opts.dry_run:
        for lbl, argv in cands:
            ctx.plan("cmd", " ".join(argv) + "   # stdio JSON-RPC，逐个尝试直到握手成功")
        lr.untested("dry-run")
        return out

    env = ctx.sandbox.env() if ctx.sandbox else {}
    tried: list[dict] = []
    client = None
    chosen = None
    for label, argv in cands:
        ctx.plan("cmd", " ".join(argv) + "   # stdio JSON-RPC 候选")
        try:
            transport = StdioTransport(argv, env={**dict(__import__("os").environ), **env},
                                       cwd=str(ctx.sandbox.workdir) if ctx.sandbox else None)
        except (OSError, FileNotFoundError) as exc:
            tried.append({"label": label, "argv": argv, "error": f"spawn failed: {exc}"})
            continue
        probe_client = JsonRpcClient(transport)
        # A live gateway answers *something* to a harmless call.
        try:
            resp = probe_client.call("session.list", {}, timeout=8.0)
            alive = isinstance(resp, dict)
        except ProbeError as exc:
            alive = False
            resp = {"__local_error__": str(exc)}
        stderr = transport.stderr_text()
        tried.append({
            "label": label,
            "argv": argv,
            "process_alive": transport.alive(),
            "response": clip(jdump(resp, 800), 800),
            "stderr": clip(stderr, 1200),
        })
        if alive:
            client, chosen = probe_client, label
            break
        probe_client.close()

    lr.add("json", "attempts", tried)
    if client is None:
        lr.unsupported(
            "没有任何候选命令以 stdio JSON-RPC 回应。文档只说 tui_gateway 的实现文件是 "
            "tui_gateway/server.py，没有给出用户可调用的命令；若真实安装里存在别的入口，"
            "用 --tui-stdio-cmd '<命令>' 重跑"
        )
        return out

    lr.measured(f"stdio 网关通过 `{chosen}` 启动成功")
    try:
        out = run_gateway_battery(ctx, client, path=path, prefix="tui_stdio")
        out["launch"] = chosen
        st = client.transport.stderr_text() if isinstance(client.transport, StdioTransport) else ""
        if st:
            ctx.result("tui_stdio.stderr", "Q1", path, "stdio 网关 stderr（协议流保持干净？）") \
                .measured("stdout 未混入非 JSON 内容" if client.framing != "unknown" else "framing 未确定") \
                .add("stderr", "gateway stderr", clip(st, 3000))
    finally:
        client.close()
    return out


# --------------------------------------------------------------------------
# path B: hermes serve + /api/ws
# --------------------------------------------------------------------------

READY_RE = re.compile(r"HERMES_(?:BACKEND|DASHBOARD)_READY\s+port=(\d+)")


class ServeProcess:
    def __init__(self, ctx: Ctx, proc, port: int):
        self.ctx = ctx
        self.proc = proc
        self.port = port
        self.out_lines: list[str] = []
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._drain, daemon=True)
        self._t.start()

    def _drain(self) -> None:
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is None:
                continue
        # read both streams via one thread each
        def pump(stream):
            for raw in iter(stream.readline, b""):
                line = strip_ansi(raw.decode("utf-8", "replace")).rstrip()
                with self._lock:
                    if len(self.out_lines) < 600:
                        self.out_lines.append(line)
                m = READY_RE.search(line)
                if m and not self.port:
                    self.port = int(m.group(1))

        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                threading.Thread(target=pump, args=(stream,), daemon=True).start()

    def text(self) -> str:
        with self._lock:
            return clip("\n".join(self.out_lines), 5000)

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def start_serve(ctx: Ctx, *, extra_args: list[str] | None = None, port: int | None = None):
    """Start `hermes serve` (headless backend that hosts /api/ws)."""
    import os
    import subprocess

    hermes = ctx.facts.get("hermes_bin", "hermes")
    port = port or free_port()
    args = ["serve"]
    if discovery.sub_has_flag(ctx, "serve", "--host") is not False:
        args += ["--host", "127.0.0.1"]
    if discovery.sub_has_flag(ctx, "serve", "--port") is not False:
        args += ["--port", str(port)]
    args += extra_args or []
    argv = ctx.sandbox.hermes_argv(hermes, args)
    ctx.plan("cmd", " ".join(argv) + "   # 无头后端，提供 /api/ws（TUI Gateway 的 WebSocket 面）")
    if ctx.opts.dry_run:
        return None
    env = {**os.environ, **ctx.sandbox.env()}
    proc = subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(ctx.sandbox.workdir), env=env, bufsize=0,
    )
    return ServeProcess(ctx, proc, port)


def check_tui_ws(ctx: Ctx) -> dict:
    path = "tui_ws"
    out: dict = {}
    if ctx.facts.get("hermes_missing"):
        ctx.result("tui_ws.launch", "Q1", path, "hermes serve + /api/ws").untested("hermes 未安装")
        return out
    if discovery.has_sub(ctx, "serve") is False:
        ctx.result("tui_ws.launch", "Q1", path, "hermes serve + /api/ws") \
            .unsupported("本次安装没有 `hermes serve` 子命令；WebSocket 面无法启动")
        return out

    lr = ctx.result("tui_ws.launch", "Q1", path, "hermes serve 启动 + ready sentinel")
    server = start_serve(ctx)
    if server is None:
        lr.untested("dry-run")
        ctx.plan("ws", "ws://127.0.0.1:<port>/api/ws   # JSON-RPC over WebSocket")
        return out

    try:
        status_url = f"http://127.0.0.1:{server.port}/api/status"
        ctx.plan("http", f"GET {status_url}  (等待后端就绪)")
        resp = wait_for_http(status_url, timeout=45)
        lr.add("stdout", "serve stdout/stderr", server.text())
        lr.add("http", "GET /api/status", resp.to_json())
        ready_line = READY_RE.search(server.text())
        if ready_line:
            server.port = int(ready_line.group(1))
            lr.add("note", "ready sentinel", ready_line.group(0))
        if not resp.status:
            lr.untested(
                f"后端在 45s 内没有在 127.0.0.1:{server.port} 上应答 /api/status；"
                f"进程输出见证据"
            )
            return out
        lr.measured(f"hermes serve 在 127.0.0.1:{server.port} 就绪（/api/status -> {resp.status}）")
        ctx.facts["serve_port"] = server.port

        # -- WebSocket connect -------------------------------------------
        conn, impl, ws_url = _ws_connect_ladder(ctx, server, resp)
        if conn is None:
            return out

        client = JsonRpcClient(WsTransport(conn, impl), reader=lambda: conn.recv_text())
        try:
            out = run_gateway_battery(ctx, client, path=path, prefix="tui_ws")
            out["ws_url"] = ws_url
            out["impl"] = impl
            # -- Q4 multi-attach on the same server ----------------------
            _check_multi_attach(ctx, ws_url, out.get("session_id"), server)
        finally:
            client.close()
    finally:
        # keep the server for later checks if requested
        ctx.facts["_serve_process"] = None
        server.stop()
    return out


def _ws_handshake_variants(port: int, status_json: Any) -> list[dict]:
    """Candidate handshakes for `/api/ws`, most-likely first.

    Hermes gates this endpoint (see sources.py: `_ws_request_is_allowed` /
    `_ws_client_is_allowed` / `_ws_host_origin_is_allowed` in
    `hermes_cli/web_server.py`, and issues #34396 / #37399 / #38412 / #85496).
    The exact rule is version-dependent, so the probe tries a ladder and
    reports which shape passed -- that shape is what the Driver must use.
    """
    tok = None
    if isinstance(status_json, dict):
        for key in ("token", "session_token", "ws_token", "auth_token"):
            v = status_json.get(key)
            if isinstance(v, str) and v:
                tok = v
                break
    variants: list[dict] = [
        # Native clients normally send no Origin at all.  A *wrong* Origin is a
        # common 403 cause, so this goes first.
        {"label": "no Origin header", "origin": None, "query": "", "subprotocols": None},
        {"label": f"Origin: http://127.0.0.1:{port} (exact bound authority)",
         "origin": f"http://127.0.0.1:{port}", "query": "", "subprotocols": None},
        {"label": f"Origin: http://localhost:{port}",
         "origin": f"http://localhost:{port}", "query": "", "subprotocols": None},
        # What the packaged Electron desktop sends; documented as accepted on
        # loopback binds (issue #38412).
        {"label": "Origin: file:///null (Electron desktop shape)",
         "origin": "file:///null", "query": "", "subprotocols": None},
        {"label": "Origin: http://127.0.0.1 (no port -- the shape that failed on 0.18.2)",
         "origin": "http://127.0.0.1", "query": "", "subprotocols": None},
        {"label": "no Origin + Sec-WebSocket-Protocol: hermes",
         "origin": None, "query": "", "subprotocols": ["hermes"]},
    ]
    if tok:
        variants.insert(1, {"label": "?token=<from /api/status> + no Origin",
                            "origin": None, "query": f"?token={tok}", "subprotocols": None})
    return variants


def _ws_connect_ladder(ctx: Ctx, server, status_resp):
    """Try each handshake shape; report which one the gateway accepted."""
    path = "tui_ws"
    base = f"ws://127.0.0.1:{server.port}/api/ws"
    cr = ctx.result("tui_ws.connect", "Q1", path,
                    "/api/ws WebSocket 握手（含 403 门禁排查阶梯 + 无 websockets 依赖的降级）")
    cr.add("note", "websockets lib available", have_websockets_lib())
    cr.add("http", "/api/status (握手前)", status_resp.to_json())

    variants = _ws_handshake_variants(server.port, status_resp.json())
    attempts: list[dict] = []
    for v in variants:
        url = base + v["query"]
        ctx.plan("ws", f"CONNECT {url}   # {v['label']}")
        try:
            conn, impl, notes = connect_ws(
                url, timeout=15, origin=v["origin"], subprotocols=v["subprotocols"]
            )
        except WebSocketHandshakeError as exc:
            attempts.append({
                "variant": v["label"], "url": url, "outcome": f"HTTP {exc.status}",
                "status_line": exc.status_line.strip(),
                "body": clip(exc.body, 1200),
                "response_headers": exc.headers,
            })
            continue
        except WebSocketError as exc:
            attempts.append({"variant": v["label"], "url": url,
                             "outcome": f"{type(exc).__name__}: {exc}"})
            continue
        attempts.append({"variant": v["label"], "url": url, "outcome": "101 Switching Protocols",
                         "impl": impl, "notes": notes})
        cr.add("json", "handshake ladder", attempts)
        cr.measured(
            f"握手成功：变体 = 「{v['label']}」，实现 = {impl}"
            f"（builtin = 纯标准库；websockets = 第三方库）。"
            "Driver 必须复用这个握手形状"
        )
        ctx.facts["ws_handshake_variant"] = v["label"]
        ctx.facts["_ws_variant"] = v
        return conn, impl, url

    cr.add("json", "handshake ladder", attempts)
    cr.add("stdout", "serve output", server.text())
    statuses = sorted({a.get("outcome", "") for a in attempts})
    bodies = [a.get("body") for a in attempts if a.get("body")]
    cr.unsupported(
        f"{base}：全部 {len(variants)} 种握手形状都被拒（{', '.join(statuses)}）。"
        + (f"服务端给出的理由：{clip(bodies[0], 300)}" if bodies else "服务端未给出正文理由。")
        + " 该 build 的 /api/ws 门禁比阶梯覆盖的更严；"
        "见 sources.py 里 web_server.py 的 _ws_request_is_allowed 系列函数与相关 issue"
    )
    return None, None, base


def _check_multi_attach(ctx: Ctx, ws_url: str, sid: str | None, server) -> None:
    """Q4: can two clients attach to the same live session at once?"""
    path = "tui_ws"
    r = ctx.result("tui_ws.multi_attach", "Q4", path, "同一活跃 session 被两个客户端同时 attach")
    if not sid:
        r.untested("上一步没有拿到 session id")
        return
    ctx.plan("ws", f"CONNECT {ws_url}   # 第二个客户端，attach 同一 session {sid}")
    try:
        conn2, impl2, _ = _reconnect_ws(ctx, ws_url)
    except WebSocketError as exc:
        r.unsupported(f"第二条 WebSocket 无法建立: {exc}")
        return
    client2 = JsonRpcClient(WsTransport(conn2, impl2), reader=lambda: conn2.recv_text())
    try:
        resp, attempts = client2.call_variants("session.activate", _sid_variants(sid))
        r.add("json", "second client session.activate", [
            {"params": a.get("params"),
             "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
            for a in attempts
        ])
        if resp is None:
            r.unsupported(
                "第二个客户端 activate 同一 session 被拒绝 —— 指向"
                "『单写入者』模型；R-04 的软提示方案需要保留"
            )
            return
        resp2, att2 = client2.call_variants("session.active_list", [{}, None])
        r.add("json", "second client active_list", clip(jdump(rpc_result(resp2), 1500), 1500))
        if not ctx.opts.live:
            r.measured(
                "两条 WebSocket 均可 attach 同一 session（activate 均返回成功）；"
                "但未开 --live，无法验证事件是否同时广播到两端"
            )
            r.add("note", "缺口", "需 --live 才能验证 message.delta 是否两端同收")
            return
        # live: send on client2, watch whether the first connection sees it
        conn3, impl3, _ = _reconnect_ws(ctx, ws_url)
        client3 = JsonRpcClient(WsTransport(conn3, impl3), reader=lambda: conn3.recv_text())
        try:
            client3.call_variants("session.activate", _sid_variants(sid))
            client2.call_variants("prompt.submit", _prompt_variants(sid, PROBE_PROMPT), timeout=30)
            seen = client3.wait_for(
                lambda m: isinstance(m.get("method"), str)
                and ("delta" in m["method"] or "message" in m["method"]),
                timeout=60,
            )
            r.add("json", "observer notifications", client3.collect_methods())
            if seen:
                r.measured(
                    "两个客户端同时 attach 且事件双向广播 —— R-04 可升级为『单写入者 + "
                    "常驻网关』，R-10 的单 gateway 多 session 拓扑成立"
                )
            else:
                r.measured(
                    "两端都能 activate，但第二个客户端发起的回合没有广播给观察端 —— "
                    "attach 是『各自独立视图』而不是共享流；不能据此升级为单写入者"
                )
        finally:
            client3.close()
    finally:
        client2.close()
