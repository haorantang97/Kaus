"""Q1 (ACP path) and the ACP half of Q2.

Method names follow the Agent Client Protocol (`initialize`, `session/new`,
`session/load`, `session/prompt`, `session/cancel`, `session/update`,
`session/request_permission`).  Hermes' own docs do not state a protocol
version, so the probe negotiates: integer 1 first, then the string form, and
records what the agent replies.

The decisive question here is *persistence*.  Hermes' ACP page says sessions
live in the adapter's in-memory manager and that `list/load/resume/fork` are
scoped to the running server process.  If that is literally true, ACP fails the
R-07 interop criterion, and the probe proves it by starting a **second**
`hermes acp` process and asking it to load the first one's session.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from .rpcio import JsonRpcClient, StdioTransport, rpc_error, rpc_result
from .util import (
    Ctx,
    ProbeError,
    clip,
    find_session_ids,
    first_str,
    jdump,
)

PROBE_TOOL_PROMPT = (
    "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and then reply with the output."
)


def _spawn_acp(ctx: Ctx, label: str) -> tuple[JsonRpcClient, StdioTransport] | None:
    hermes = ctx.facts.get("hermes_bin", "hermes")
    argv = ctx.sandbox.hermes_argv(hermes, ["acp"])
    ctx.plan("cmd", " ".join(argv) + f"   # ACP over stdio ({label})")
    if ctx.opts.dry_run:
        return None
    env = {**os.environ, **ctx.sandbox.env()}
    transport = StdioTransport(argv, env=env, cwd=str(ctx.sandbox.workdir))
    return JsonRpcClient(transport), transport


def _initialize(ctx: Ctx, client: JsonRpcClient, prefix: str) -> dict | None:
    r = ctx.result(f"{prefix}.initialize", "Q1", "acp", "ACP initialize（协议版本 + 能力协商）")
    variants = [
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        },
        {"protocolVersion": "0.1.0", "clientCapabilities": {}},
        {"protocolVersion": 1},
    ]
    resp, attempts = client.call_variants("initialize", variants, timeout=25)
    r.add("json", "attempts", [
        {"params": a.get("params"),
         "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
        for a in attempts
    ])
    if resp is None:
        r.unsupported("initialize 未成功；ACP 面不可用（详见 attempts 与 stderr）")
        return None
    result = rpc_result(resp)
    r.add("json", "initialize result", clip(jdump(result, 3000), 3000))
    ver = None
    if isinstance(result, dict):
        ver = result.get("protocolVersion")
    caps = result.get("agentCapabilities") if isinstance(result, dict) else None
    load_session = None
    session_caps = None
    if isinstance(caps, dict):
        load_session = caps.get("loadSession")
        session_caps = caps.get("sessionCapabilities")
    ctx.facts["acp_protocol_version"] = ver
    ctx.facts["acp_agent_capabilities"] = caps
    ctx.facts["acp_session_capabilities"] = session_caps
    r.measured(
        f"protocolVersion={ver!r}；agentCapabilities.loadSession={load_session!r}"
        f"；sessionCapabilities={jdump(session_caps, 300) if session_caps else 'None'}"
    )

    # The declared session capabilities are the interesting part: the official
    # ACP page says sessions are process-local memory only, so a build that
    # advertises fork/list/resume contradicts its own documentation.  Record
    # that as a standalone finding and let the cross-process test settle it.
    cr = ctx.result("acp.session_capabilities", "Q2", "acp",
                    "ACP 声明的 session 能力（与官方文档『仅进程内内存』的说法是否冲突）")
    cr.add("json", "agentCapabilities", clip(jdump(caps, 2000), 2000))
    declared = []
    if isinstance(session_caps, dict):
        declared = sorted(k for k, v in session_caps.items() if v)
    elif isinstance(session_caps, list):
        declared = [str(x) for x in session_caps]
    if declared:
        conflicting = [k for k in declared if k.lower() in ("fork", "list", "resume", "load")]
        cr.measured(
            f"声明的 session 能力: {', '.join(declared)}"
            + (
                f"。其中 {', '.join(conflicting)} 与官方 ACP 文档"
                "『session 仅由 adapter 进程内内存管理器持有、进程退出即失』直接冲突 —— "
                "以本报告的跨进程实测为准"
                if conflicting else ""
            )
        )
    elif load_session is not None:
        cr.measured(f"未声明 sessionCapabilities，仅有 loadSession={load_session!r}")
    else:
        cr.untested("initialize 结果里没有 sessionCapabilities / loadSession 字段")
    return result if isinstance(result, dict) else None


# Failures that mean "this sandbox is not configured", not "Hermes cannot do it".
_ENV_PROBLEM_PATTERNS = (
    (r"(?i)no llm provider configured", "沙盒没有任何 provider 配置"),
    (r"(?i)no api key|missing api key|api key not (?:set|found)", "沙盒缺少 API key"),
    (r"(?i)provider .* not configured", "沙盒的 provider 未配置"),
    (r"(?i)no model (?:configured|selected|available)", "沙盒没有可用模型"),
    (r"(?i)authentication required|not authenticated", "沙盒未完成认证"),
)


def _environment_problem(text: str) -> str | None:
    """Classify a failure as environment-not-configured rather than unsupported."""
    for pattern, label in _ENV_PROBLEM_PATTERNS:
        if re.search(pattern, text or ""):
            return label
    return None


def check_acp(ctx: Ctx) -> dict:
    out: dict = {}
    path = "acp"
    if ctx.facts.get("hermes_missing"):
        ctx.result("acp.launch", "Q1", path, "hermes acp 启动").untested("hermes 未安装")
        return out
    from . import discovery

    if discovery.has_sub(ctx, "acp") is False:
        ctx.result("acp.launch", "Q1", path, "hermes acp 启动") \
            .unsupported("本次安装没有 `hermes acp` 子命令")
        return out

    lr = ctx.result("acp.launch", "Q1", path, "hermes acp 启动（stdio）")
    spawned = _spawn_acp(ctx, "primary")
    if spawned is None:
        lr.untested("dry-run")
        return out
    client, transport = spawned
    try:
        time.sleep(0.3)
        if not transport.alive():
            lr.unsupported(f"hermes acp 立即退出；stderr: {clip(transport.stderr_text(), 1500)}")
            return out
        lr.measured("hermes acp 进程存活，stdout 保留给 JSON-RPC")
        lr.add("stderr", "acp stderr", clip(transport.stderr_text(), 2000))

        init = _initialize(ctx, client, "acp")
        if init is None:
            return out

        # session/new -----------------------------------------------------
        nr = ctx.result("acp.session.new", "Q1", path, "session/new（创建会话）")
        cwd = str(ctx.sandbox.workdir)
        resp, attempts = client.call_variants(
            "session/new",
            [
                {"cwd": cwd, "mcpServers": []},
                {"cwd": cwd},
                {"cwd": cwd, "mcpServers": [], "model": None},
            ],
            timeout=60,
        )
        nr.add("json", "attempts", [
            {"params": a.get("params"),
             "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
            for a in attempts
        ])
        sid = None
        if resp is None:
            errors = " | ".join(
                str(rpc_error(a.get("response")) if "response" in a else a.get("error_local"))
                for a in attempts
            )
            blob = errors + "\n" + transport.stderr_text()
            env_problem = _environment_problem(blob)
            nr.add("stderr", "acp stderr", clip(transport.stderr_text(), 2000))
            if env_problem:
                # This is the difference between "Hermes can't" and "we didn't
                # give it credentials".  Calling it 不支持 would be a false
                # negative that propagates into every downstream check.
                nr.untested(
                    f"{env_problem} —— 沙盒缺 provider 配置，不是 ACP 不支持 session/new。"
                    f"请带 --seed-config（见 README 顶部）重跑。服务端原话：{clip(errors, 300)}"
                )
            elif re.search(r"(?i)mcpservers", blob):
                nr.untested(
                    "session/new 要求 `mcpServers` 为必填参数（探针已在候选形状里带了 "
                    "`mcpServers: []`，若仍失败见 attempts）；服务端原话："
                    f"{clip(errors, 300)}"
                )
            else:
                nr.unsupported(f"session/new 全部失败：{clip(errors, 400)}")
        else:
            result = rpc_result(resp)
            nr.add("json", "result", clip(jdump(result, 2000), 2000))
            sid = first_str(result, ("sessionId", "session_id", "id"))
            out["session_id"] = sid
            nr.measured(f"sessionId={sid!r}")
            native = find_session_ids(result)
            nr.add("note", "looks like a native Hermes session id?",
                   {"native_shaped_ids": native,
                    "acp_session_id_is_native_shaped": bool(
                        sid and re.match(r"^\d{8}_\d{6}_[0-9a-fA-F]{4,16}$", sid))})

        # session/list -----------------------------------------------------
        # Advertised via agentCapabilities.sessionCapabilities on 0.18.2, so it
        # is worth probing even though the ACP spec does not require it.
        sl = ctx.result("acp.session.list", "Q1", path, "session/list（列出 ACP 可见的会话）")
        resp, attempts = client.call_variants(
            "session/list", [{}, None, {"cwd": cwd}], timeout=25
        )
        sl.add("json", "attempts", [
            {"params": a.get("params"),
             "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
            for a in attempts
        ])
        if resp is None:
            errs = " | ".join(
                str(rpc_error(a.get("response")) if "response" in a else a.get("error_local"))
                for a in attempts
            )
            if re.search(r"-32601|not found|unknown method", errs, re.I):
                sl.unsupported(f"session/list 方法不存在 — {clip(errs, 300)}")
            else:
                sl.unsupported(f"session/list 全部参数形状被拒 — {clip(errs, 300)}")
        else:
            listed = rpc_result(resp)
            ids = find_session_ids(listed)
            sl.add("json", "result", clip(jdump(listed, 2000), 2000))
            sl.measured(
                f"session/list OK，可见 {len(ids)} 个 id: "
                + (", ".join(ids[:5]) or "(空列表)")
                + "。列表非空即说明 ACP 能看到自身进程之外创建的会话"
            )
            out["listed_ids"] = ids

        # session/load (same process) -------------------------------------
        lr2 = ctx.result("acp.session.load.same_process", "Q1", path,
                         "session/load（同进程内 resume）")
        if not sid:
            lr2.untested("没有 sessionId")
        else:
            resp, attempts = client.call_variants(
                "session/load", [{"sessionId": sid, "cwd": cwd}, {"sessionId": sid}], timeout=30
            )
            lr2.add("json", "attempts", [
                {"params": a.get("params"),
                 "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
                for a in attempts
            ])
            if resp is None:
                lr2.unsupported("同进程 session/load 被拒绝")
            else:
                lr2.measured("同进程 session/load OK")
                lr2.add("json", "result", clip(jdump(rpc_result(resp), 1500), 1500))

        # prompt / streaming ----------------------------------------------
        sr = ctx.result("acp.stream", "Q1", path, "session/prompt 文本流 / tool 事件 / permission")
        if not sid:
            sr.untested("没有 sessionId")
        elif not ctx.opts.live:
            sr.untested("未开 --live：session/prompt 会真实调用模型并计费，默认不执行")
        else:
            permission_seen: list[dict] = []

            def hook(msg: dict) -> None:
                m = msg.get("method") or ""
                if "request_permission" in m or "permission" in m:
                    permission_seen.append(msg)
                    if msg.get("id") is not None:
                        # answer immediately so the agent is not left hanging
                        client.respond(
                            msg["id"],
                            {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
                        )

            client.on_notification(hook)
            resp, attempts = client.call_variants(
                "session/prompt",
                [
                    {"sessionId": sid, "prompt": [{"type": "text", "text": PROBE_TOOL_PROMPT}]},
                    {"sessionId": sid, "prompt": PROBE_TOOL_PROMPT},
                ],
                timeout=120,
            )
            methods = client.collect_methods()
            sr.add("json", "notification methods", methods)
            sr.add("json", "sample session/update payloads",
                   clip(jdump(client.notifications()[:20], 4000), 4000))
            if resp is None:
                sr.unsupported("session/prompt 全部参数形状被拒绝")
            else:
                sr.measured("stopReason=" + str(
                    (rpc_result(resp) or {}).get("stopReason") if isinstance(rpc_result(resp), dict) else "?"
                ) + "；通知方法: " + ", ".join(methods[:12]))
                update_kinds = _acp_update_kinds(client)
                sr.add("json", "session/update sessionUpdate kinds", update_kinds)

            pr = ctx.result("acp.permission", "Q1", path, "session/request_permission")
            if permission_seen:
                pr.measured("收到 session/request_permission 并已用 optionId 回传")
                pr.add("json", "requests", clip(jdump(permission_seen[:5], 2500), 2500))
            else:
                pr.untested("本轮未触发权限请求（模型可能没调受控工具）；不能据此判定不支持")

            # cancel --------------------------------------------------------
            cr = ctx.result("acp.cancel", "Q1", path, "session/cancel（打断）")
            try:
                client.notify("session/cancel", {"sessionId": sid})
                cr.measured("session/cancel 通知已发出（ACP 中 cancel 是通知，无回执）")
            except Exception as exc:  # noqa: BLE001
                cr.untested(f"发送失败: {exc}")

        # usage -------------------------------------------------------------
        ur = ctx.result("acp.usage", "Q1", path, "usage / token 统计")
        tokens = _find_usage(client)
        if tokens:
            ur.measured("在 session/update 中发现 usage 字段")
            ur.add("json", "usage-ish payloads", clip(jdump(tokens[:5], 2000), 2000))
        elif not ctx.opts.live:
            ur.untested("未开 --live，没有回合可统计")
        else:
            ur.unsupported(
                "ACP 事件流里没有出现 token/usage 字段 —— usage 需要走原生面（state.db / "
                "session.usage）补齐"
            )

        out["notification_methods"] = client.collect_methods()
        out["stderr"] = clip(transport.stderr_text(), 2000)

        # cross-process load (the decisive persistence test) -----------------
        _check_acp_second_process(ctx, sid, cwd)
    finally:
        client.close()
    return out


def _acp_update_kinds(client: JsonRpcClient) -> list[str]:
    kinds: list[str] = []
    for msg in client.notifications():
        params = msg.get("params")
        if isinstance(params, dict):
            upd = params.get("update")
            if isinstance(upd, dict):
                k = upd.get("sessionUpdate") or upd.get("type")
                if isinstance(k, str) and k not in kinds:
                    kinds.append(k)
    return kinds


def _find_usage(client: JsonRpcClient) -> list[dict]:
    hits = []
    for msg in client.notifications():
        text = jdump(msg, 4000)
        if re.search(r"(?i)\b(usage|input_tokens|output_tokens|totalTokens|token_count)\b", text):
            hits.append(msg)
    return hits


def _check_acp_second_process(ctx: Ctx, sid: str | None, cwd: str) -> None:
    r = ctx.result("acp.session.load.cross_process", "Q2", "acp",
                   "ACP 会话跨进程 resume（另起一个 hermes acp，load 前一个的 sessionId）")
    if not sid:
        r.untested("第一个 ACP 进程没有创建出 sessionId")
        return
    spawned = _spawn_acp(ctx, "second process")
    if spawned is None:
        r.untested("dry-run")
        return
    client2, transport2 = spawned
    try:
        _init = client2.call_variants(
            "initialize",
            [{"protocolVersion": 1, "clientCapabilities": {}}, {"protocolVersion": "0.1.0"}],
            timeout=25,
        )[0]

        # Try both spellings: `session/load` is the ACP-spec name, `session/resume`
        # appears in Hermes' own sessionCapabilities on 0.18.2.
        winner = None
        all_attempts: dict[str, list] = {}
        for method in ("session/load", "session/resume"):
            resp, attempts = client2.call_variants(
                method, [{"sessionId": sid, "cwd": cwd}, {"sessionId": sid}], timeout=30
            )
            all_attempts[method] = [
                {"params": a.get("params"),
                 "error": rpc_error(a.get("response")) if "response" in a else a.get("error_local")}
                for a in attempts
            ]
            if resp is not None:
                winner = (method, resp)
                break
        r.add("json", "attempts", all_attempts)
        r.add("stderr", "second process stderr", clip(transport2.stderr_text(), 1500))

        # session/list from the second process is the softer version of the
        # same question: can a fresh ACP process even *see* the session?
        lresp, _lattempts = client2.call_variants("session/list", [{}, None], timeout=25)
        visible = find_session_ids(rpc_result(lresp)) if lresp is not None else []
        r.add("json", "second process session/list ids", visible[:20])
        r.add("note", "id visible to a fresh ACP process?", sid in visible)

        if winner is not None:
            method, resp = winner
            r.measured(
                f"跨进程 {method} 成功 —— ACP session 有持久化后端，"
                "与官方文档『仅进程内内存』的说法不符；R-07 判据对 ACP 这条路径成立"
            )
            r.add("json", "result", clip(jdump(rpc_result(resp), 1500), 1500))
        elif sid in visible:
            r.measured(
                "新进程能在 session/list 里看到该 id，但 load/resume 均失败 —— "
                "会话是持久的、可枚举的，但 ACP 无法把它接回来续接。"
                "对 R-07 而言这条路径只满足『可见』不满足『可续接』"
            )
        else:
            blob = jdump(all_attempts, 2000) + transport2.stderr_text()
            env_problem = _environment_problem(blob)
            if env_problem:
                r.untested(
                    f"{env_problem} —— 第二个 ACP 进程同样缺 provider，"
                    "无法区分『会话不可跨进程』与『环境没配好』。请带 --seed-config 重跑"
                )
            else:
                r.unsupported(
                    "另一个 hermes acp 进程既看不到也无法 load/resume 该 sessionId —— "
                    "与官方文档一致：ACP session 只活在当前 adapter 进程的内存里。"
                    "结论：ACP 单独不满足 R-07 的跨协议续接判据，"
                    "除非其底层消息仍落在同一 state.db（见 Q2/Q3 原生侧）"
                )
    finally:
        client2.close()
