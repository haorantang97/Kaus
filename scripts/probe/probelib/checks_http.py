"""Q1 (OpenAI-compatible HTTP + SSE path).

R-07 forbids ruling this path out untested, so it is probed with the same
battery shape as the other two: create, list, resume, history, stream, tool
events, approvals, interrupt, usage.

Safety note: the surface is started by the messaging gateway process
(`hermes gateway`), which in a *real* profile may also bring up Telegram /
Discord / Slack connectors.  The probe therefore only enables it automatically
in isolated-home mode; in profile mode it requires an explicit opt-in.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from . import discovery
from .httpc import SseStream, request, wait_for_http
from .util import (
    Ctx,
    clip,
    extract_session_id,
    find_session_ids,
    free_port,
    jdump,
    register_secret,
    session_id_shape,
    strip_ansi,
)

PROBE_TOOL_PROMPT = (
    "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and then reply with the output."
)


class GatewayProcess:
    def __init__(self, proc):
        self.proc = proc
        self.lines: list[str] = []
        self._lock = threading.Lock()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream) -> None:
        for raw in iter(stream.readline, b""):
            with self._lock:
                if len(self.lines) < 500:
                    self.lines.append(strip_ansi(raw.decode("utf-8", "replace")).rstrip())

    def text(self) -> str:
        with self._lock:
            return clip("\n".join(self.lines), 5000)

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _start_gateway(ctx: Ctx, port: int):
    hermes = ctx.facts.get("hermes_bin", "hermes")
    # `hermes gateway` is a command group in recent builds; `run` is the
    # foreground verb.  Fall back to the bare form if `run` is not listed.
    sub_flags = (ctx.facts.get("subcommand_flags") or {}).get("gateway") or []
    args = ["gateway", "run"] if "run" in " ".join(sub_flags) or True else ["gateway"]
    argv = ctx.sandbox.hermes_argv(hermes, args)
    ctx.plan("cmd", " ".join(argv) + f"   # OpenAI 兼容 API server，端口 {port}")
    if ctx.opts.dry_run:
        return None
    env = {
        **os.environ,
        **ctx.sandbox.env(
            {
                "API_SERVER_ENABLED": "true",
                "API_SERVER_KEY": ctx.sandbox.api_key,
                "API_SERVER_PORT": str(port),
                "API_SERVER_HOST": "127.0.0.1",
            }
        ),
    }
    proc = subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(ctx.sandbox.workdir), env=env, bufsize=0,
    )
    return GatewayProcess(proc)


def check_http_sse(ctx: Ctx) -> dict:
    path = "http_sse"
    out: dict = {}
    if ctx.facts.get("hermes_missing"):
        ctx.result("http.launch", "Q1", path, "OpenAI 兼容 HTTP+SSE 启动").untested("hermes 未安装")
        return out
    if not ctx.opts.enable_http:
        ctx.result("http.launch", "Q1", path, "OpenAI 兼容 HTTP+SSE 启动") \
            .untested("--no-http：本次未启动 API server")
        return out
    if ctx.opts.home_mode == "profile":
        ctx.result("http.launch", "Q1", path, "OpenAI 兼容 HTTP+SSE 启动").untested(
            "profile 模式下默认不启动 `hermes gateway`：它同时是消息网关，可能拉起 "
            "Telegram/Discord 等真实连接器。请用 --home-mode isolated 测这条路径"
        )
        return out
    if discovery.has_sub(ctx, "gateway") is False:
        ctx.result("http.launch", "Q1", path, "OpenAI 兼容 HTTP+SSE 启动") \
            .unsupported("本次安装没有 `hermes gateway` 子命令")
        return out

    register_secret(ctx.sandbox.api_key)
    port = free_port()
    lr = ctx.result("http.launch", "Q1", path, "API server 启动与鉴权")
    ctx.sandbox.write_api_server_env(port)
    server = _start_gateway(ctx, port)
    if server is None:
        base = f"http://127.0.0.1:{port}"
        for line in (
            f"GET {base}/health",
            f"GET {base}/v1/capabilities",
            f"GET {base}/v1/models",
            f"POST {base}/v1/runs",
            f"GET {base}/v1/runs/<id>/events   (SSE)",
            f"POST {base}/v1/runs/<id>/stop",
            f"GET {base}/api/sessions",
        ):
            ctx.plan("http", line + "   [Authorization: Bearer <probe key>]")
        lr.untested("dry-run")
        return out

    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {ctx.sandbox.api_key}"}
    try:
        ctx.plan("http", f"GET {base}/health  (等待就绪)")
        health = wait_for_http(f"{base}/health", timeout=60)
        lr.add("http", "GET /health", health.to_json())
        lr.add("stdout", "gateway output", server.text())
        if not health.status:
            lr.untested(
                f"API server 60s 内未在 {base} 应答。常见原因：该 build 的启动动词不是 "
                "`hermes gateway run`，或 .env 未被读取。见进程输出"
            )
            return out
        lr.measured(f"API server 就绪于 {base}（/health -> {health.status}）")

        # unauthenticated probe: proves the bearer gate exists
        na = request(f"{base}/v1/models", timeout=10)
        lr.add("http", "GET /v1/models (no auth)", na.to_json())

        # -- capabilities / models -------------------------------------
        cr = ctx.result("http.capabilities", "Q6", path, "/v1/capabilities（机器可读能力面 = 协议版本证据）")
        ctx.plan("http", f"GET {base}/v1/capabilities")
        cap = request(f"{base}/v1/capabilities", headers=auth, timeout=15)
        cr.add("http", "GET /v1/capabilities", cap.to_json())
        if cap.ok:
            cr.measured("已取得能力清单（原样存入 JSON 报告）")
            ctx.facts["http_capabilities"] = cap.json()
        else:
            cr.unsupported(f"/v1/capabilities -> {cap.brief()}")

        mr = ctx.result("http.models", "Q1", path, "/v1/models（模型目录）")
        ctx.plan("http", f"GET {base}/v1/models")
        models = request(f"{base}/v1/models", headers=auth, timeout=15)
        mr.add("http", "GET /v1/models", models.to_json())
        mr.measured(models.brief()) if models.ok else mr.unsupported(models.brief())

        # -- session list / create via REST ------------------------------
        sl = ctx.result("http.sessions.list", "Q1", path, "/api/sessions（列出会话）")
        ctx.plan("http", f"GET {base}/api/sessions")
        sessions = request(f"{base}/api/sessions", headers=auth, timeout=15)
        sl.add("http", "GET /api/sessions", sessions.to_json())
        if sessions.ok:
            sl.measured(f"HTTP {sessions.status}；可见 session id: "
                        + ", ".join(find_session_ids(sessions.body)[:5] or ["(none)"]))
        elif sessions.status in (404, 405):
            sl.unsupported(f"该 build 没有 /api/sessions（{sessions.brief()}）")
        else:
            sl.untested(sessions.brief())

        sc = ctx.result("http.sessions.create", "Q1", path, "POST /api/sessions（创建空会话，无模型调用）")
        ctx.plan("http", f"POST {base}/api/sessions")
        created = request(f"{base}/api/sessions", method="POST", headers=auth, body={}, timeout=20)
        sc.add("http", "POST /api/sessions", created.to_json())
        http_sid = None
        if created.ok:
            # 0.18.2 answers {"object":"hermes.session","session":{"id":"api_...","source":
            # "api_server",...}} -- the id is nested and its shape is api_<ts>_<hex>,
            # not the CLI's YYYYMMDD_HHMMSS_<hex>.  Reading the top-level "id"
            # yields None and silently voids every downstream check.
            payload = created.json()
            http_sid, how = extract_session_id(payload if payload is not None else created.body)
            shape = session_id_shape(http_sid) if http_sid else None
            out["session_id"] = http_sid
            out["session_id_shape"] = shape
            sc.add("json", "id extraction", {
                "session_id": http_sid,
                "found_via": how,
                "id_shape": shape or "unrecognised (recorded anyway)",
                "response_header_x_hermes_session_id": created.headers.get("x-hermes-session-id"),
            })
            if http_sid:
                sc.measured(
                    f"创建成功，session id = {http_sid!r}（取自 {how}；id 形状 = "
                    f"{shape or '未知形状，但已如实记录'}）。"
                    "注意：API 会话 id 形状与 CLI 的 YYYYMMDD_HHMMSS_hex 不同，"
                    "形状不同不等于不是原生会话"
                )
            else:
                sc.untested(
                    f"HTTP {created.status} 成功但没能从响应里解析出 session id；"
                    f"响应体见证据（需要扩展 extract_session_id 的形状表）"
                )
        elif created.status in (404, 405):
            sc.unsupported(f"该 build 不支持 POST /api/sessions（{created.brief()}）")
        else:
            sc.untested(created.brief())

        # -- history ------------------------------------------------------
        hr = ctx.result("http.sessions.history", "Q1", path, "/api/sessions/{id}/messages（历史）")
        if not http_sid:
            hr.untested("没有可用 session id")
        else:
            ctx.plan("http", f"GET {base}/api/sessions/{http_sid}/messages")
            hist = request(f"{base}/api/sessions/{http_sid}/messages", headers=auth, timeout=20)
            hr.add("http", "GET messages", hist.to_json())
            hr.measured(hist.brief()) if hist.ok else hr.unsupported(hist.brief())

        # -- runs + SSE ---------------------------------------------------
        rr = ctx.result("http.runs", "Q1", path, "POST /v1/runs + SSE 事件流 + tool 事件 + interrupt")
        if not ctx.opts.live:
            ctx.plan("http", f"POST {base}/v1/runs  {{'input': ...}}   # 需要 --live（真实模型调用）")
            rr.untested("未开 --live：POST /v1/runs 会真实调用模型并计费")
        else:
            body: dict[str, Any] = {"input": PROBE_TOOL_PROMPT}
            if http_sid:
                body["session_id"] = http_sid
            ctx.plan("http", f"POST {base}/v1/runs")
            run = request(f"{base}/v1/runs", method="POST", headers=auth, body=body, timeout=30)
            rr.add("http", "POST /v1/runs", run.to_json())
            run_json = run.json() or {}
            run_id = run_json.get("run_id") or run_json.get("id")
            hdr_sid = run.headers.get("x-hermes-session-id")
            rr.add("note", "X-Hermes-Session-Id (response header)", hdr_sid)
            if hdr_sid:
                out["session_id"] = hdr_sid
            if not run_id:
                rr.unsupported(f"POST /v1/runs 未返回 run_id（{run.brief()}）")
            else:
                ctx.plan("http", f"GET {base}/v1/runs/{run_id}/events   (SSE)")
                sse = SseStream(f"{base}/v1/runs/{run_id}/events", headers=auth, timeout=120).start()
                got = sse.wait_for(
                    lambda ev: "complete" in str(ev.get("event") or "").lower(), timeout=100
                )
                names = sse.event_names()
                rr.add("json", "SSE event names", names)
                rr.add("json", "SSE sample", clip(jdump(sse.snapshot(25), 4000), 4000))
                rr.add("json", "SSE raw head", sse.raw_lines[:30])
                if sse.error:
                    rr.add("note", "SSE error", sse.error)
                if names:
                    rr.measured("SSE 事件: " + ", ".join(names[:15]))
                else:
                    rr.untested(f"SSE 未产出事件（status={sse.status}, error={sse.error}）")
                sse.stop()

                # interrupt
                ir = ctx.result("http.stop", "Q1", path, "POST /v1/runs/{id}/stop（打断）")
                ctx.plan("http", f"POST {base}/v1/runs/{run_id}/stop")
                stop = request(f"{base}/v1/runs/{run_id}/stop", method="POST",
                               headers=auth, body={}, timeout=20)
                ir.add("http", "POST stop", stop.to_json())
                ir.measured(stop.brief()) if stop.ok else ir.unsupported(stop.brief())

                # usage
                ur = ctx.result("http.usage", "Q1", path, "GET /v1/runs/{id}（usage / token）")
                poll = request(f"{base}/v1/runs/{run_id}", headers=auth, timeout=20)
                ur.add("http", "GET run", poll.to_json())
                if poll.ok and ("usage" in poll.body or "token" in poll.body):
                    ur.measured("run 对象里带 usage/token 字段")
                elif poll.ok:
                    ur.unsupported("run 对象里没有 usage/token 字段")
                else:
                    ur.untested(poll.brief())

                # approval
                ar = ctx.result("http.approval", "Q1", path, "POST /v1/runs/{id}/approval（权限决策）")
                approval_events = [
                    ev for ev in sse.snapshot(80)
                    if "approval" in str(ev.get("event") or "").lower()
                    or "approval" in jdump(ev.get("data"), 500).lower()
                ]
                if approval_events:
                    ar.add("json", "approval events", clip(jdump(approval_events[:5], 2000), 2000))
                    ar.measured("SSE 中出现审批事件；端点存在")
                else:
                    probe = request(f"{base}/v1/runs/{run_id}/approval", method="POST",
                                    headers=auth, body={"decision": "deny"}, timeout=15)
                    ar.add("http", "POST approval (no pending request)", probe.to_json())
                    if probe.status in (404, 405):
                        ar.unsupported(f"端点不存在（{probe.brief()}）")
                    else:
                        ar.untested(
                            "本轮没有待决审批；端点以 "
                            f"{probe.status} 回应『无待决请求』，不能据此判定形状"
                        )

        out["base_url"] = base
        out["gateway_output"] = server.text()
    finally:
        server.stop()
    return out
