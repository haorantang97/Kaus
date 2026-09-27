"""Q2 (cross-protocol interop), Q3 (native history completeness), Q5 (out-of-band
write detectability) -- everything that looks at Hermes' own store rather than a
wire protocol.

These three are the ones that actually decide architecture:

* Q2 decides whether R-07's "one logical session identity" holds, and therefore
  whether §8.1 survives.
* Q3 decides whether R-02 can keep "the dashboard is only a window" or has to
  add the discardable cache layer.
* Q5 sizes the detector R-04's soft-prompt needs.  The subtlety measured here
  is WAL: with `journal_mode=WAL` the main `state.db` mtime often does *not*
  move until a checkpoint, while `state.db-wal` moves immediately.  A detector
  watching the wrong file silently never fires.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .util import Ctx, clip, find_session_ids, jdump, run_cmd

# Columns / tables that would carry a permission decision if one were stored.
APPROVAL_HINTS = ("approval", "permission", "consent", "sudo", "denied", "allowed", "disposition")

# Distinguishing these two is the whole trick behind the zero-cost resume test:
# "session not found" means the id did not resolve; a provider complaint means
# it *did* resolve and the CLI got all the way to needing a model.
_RESUME_NOT_FOUND_RE = re.compile(
    r"(?i)(session not found|no such session|unknown session|"
    r"no previous (?:cli )?session(?: found)?|could not find session)"
)
_PROVIDER_MISSING_RE = re.compile(
    r"(?i)(no llm provider configured|no api key|missing api key|"
    r"api key not (?:set|found)|provider [\w'\"-]+ .{0,40}no api key|"
    r"no model (?:configured|selected|available)|not authenticated)"
)


# --------------------------------------------------------------------------
# read-only sqlite helpers (sandbox paths only)
# --------------------------------------------------------------------------


def _connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)


def _guard(ctx: Ctx, db: Path) -> None:
    """Never touch a database outside the sandbox home."""
    home = ctx.sandbox.home.resolve() if ctx.sandbox.home.exists() else ctx.sandbox.home
    try:
        db.resolve().relative_to(home)
    except ValueError as exc:
        raise RuntimeError(f"refusing to read {db}: outside sandbox home {home}") from exc


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    for name, _kind in rows:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name!r})").fetchall()]
        except sqlite3.Error:
            cols = []
        out[str(name)] = cols
    return out


# --------------------------------------------------------------------------
# Q3 + storage facts
# --------------------------------------------------------------------------


def check_native_store(ctx: Ctx, session_ids: dict[str, str | None]) -> dict:
    """Inspect the sandbox state.db: schema, per-session rows, completeness."""
    path = "native"
    out: dict = {}
    db = ctx.sandbox.state_db

    sr = ctx.result("native.state_db", "Q3", path, "原生存储位置与 schema")
    ctx.plan("read", f"{db} (sqlite, mode=ro)")
    if ctx.opts.dry_run:
        sr.untested("dry-run")
        return out
    if not db.is_file():
        sr.unsupported(
            f"{db} 不存在 —— 说明本次探针创建的会话没有落到该 HERMES_HOME 的 state.db，"
            "或该 build 使用了别的存储布局；这是 R-02 的关键否证，需要人工复核"
        )
        sibling = sorted(p.name for p in ctx.sandbox.home.glob("*") if p.is_file())[:40]
        sr.add("json", "sandbox home contents", sibling)
        return out
    _guard(ctx, db)
    try:
        conn = _connect_ro(db)
    except sqlite3.Error as exc:
        sr.untested(f"无法只读打开 state.db: {exc}")
        return out
    try:
        tables = _tables(conn)
        out["tables"] = tables
        try:
            jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            jm = "?"
        try:
            ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            ver = None
        out["journal_mode"] = jm
        out["schema_version"] = ver
        sr.measured(
            f"{db}；journal_mode={jm}；schema_version={ver}；表数={len(tables)}"
        )
        sr.add("json", "tables and columns", tables)

        # -- approval persistence -------------------------------------
        ar = ctx.result("native.approval_columns", "Q3", path,
                        "原生存储里是否存在权限/审批决策的落点")
        hits: dict[str, list[str]] = {}
        for tname, cols in tables.items():
            matched = [c for c in cols if any(h in c.lower() for h in APPROVAL_HINTS)]
            if any(h in tname.lower() for h in APPROVAL_HINTS):
                matched = matched or ["<table name matches>"]
            if matched:
                hits[tname] = matched
        ar.add("json", "matching tables/columns", hits)
        if hits:
            ar.measured("存在候选落点: " + "; ".join(f"{k}({','.join(v)})" for k, v in hits.items()))
        else:
            ar.unsupported(
                "schema 中没有任何审批/权限相关表或列 —— 权限决策不进原生账本。"
                "按 R-02 的回退条款，卡片若要回放『用户当时批准了什么』就必须补"
                "可丢弃缓存层"
            )

        # -- per-session content --------------------------------------
        for label, sid in session_ids.items():
            _session_completeness(ctx, conn, tables, label, sid)

        # session inventory (Q20 附带)
        try:
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            out["session_count"] = n
            srcs = conn.execute(
                "SELECT source, COUNT(*) FROM sessions GROUP BY source"
            ).fetchall() if "source" in tables.get("sessions", []) else []
            out["sources"] = {str(a): b for a, b in srcs}
            inv = ctx.result("native.inventory", "Q3", path, "沙盒内会话总量与 source 分布")
            inv.measured(f"sessions={n}；source 分布={out['sources']}")
            inv.add("note", "解读",
                    "source 值区分了会话是被哪条路径创建的；跨协议互通的第一手证据")
        except sqlite3.Error as exc:
            ctx.result("native.inventory", "Q3", path, "沙盒内会话总量与 source 分布") \
                .untested(f"{exc}")
    finally:
        conn.close()
    return out


def _session_completeness(
    ctx: Ctx, conn: sqlite3.Connection, tables: dict, label: str, sid: str | None
) -> None:
    r = ctx.result(f"native.history.{label}", "Q3", "native",
                   f"原生历史完整度 · 由 {label} 创建的会话")
    if not sid:
        r.untested(f"{label} 路径没有产出 session id")
        return
    if "sessions" not in tables:
        r.untested("state.db 没有 sessions 表")
        return
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    except sqlite3.Error as exc:
        r.untested(f"查询失败: {exc}")
        return
    # The premise behind R-02 and R-07 alike: does this path's session land in
    # the same native ledger?  Stated as its own finding so the decision section
    # can cite it directly rather than inferring it from a completeness table.
    lr = ctx.result(f"native.same_store.{label}", "Q2", "native",
                    f"{label} 创建的会话是否落在同一原生 state.db")
    if row is None:
        lr.unsupported(
            f"session {sid} 不在 state.db 的 sessions 表里 —— 该路径创建的会话"
            "没有进原生账本"
        )
        r.unsupported(
            f"session {sid} 不在 state.db 的 sessions 表里 —— 该路径创建的会话"
            "没有进原生账本，R-02『唯一账本』对这条路径不成立"
        )
        return
    cols = tables["sessions"]
    session_row = dict(zip(cols, row))
    source = session_row.get("source")
    lr.add("json", "sessions row", {k: clip(str(v), 300) for k, v in session_row.items()})
    lr.measured(
        f"session {sid} 确实在同一 state.db 的 sessions 表中"
        + (f"，source={source!r}" if source else "")
        + " —— 该路径的会话与 CLI 共用同一原生存储，R-02『原生存储是唯一账本』"
        "对这条路径成立"
    )
    r.add("json", "sessions row", {k: clip(str(v), 300) for k, v in session_row.items()})

    msg_cols = tables.get("messages", [])
    facts: dict[str, Any] = {"session_in_db": True}
    if msg_cols:
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,)
            ).fetchall()
        except sqlite3.Error as exc:
            r.untested(f"messages 查询失败: {exc}")
            return
        msgs = [dict(zip(msg_cols, m)) for m in rows]
        facts["message_count"] = len(msgs)
        roles = sorted({str(m.get("role")) for m in msgs})
        facts["roles"] = roles
        facts["has_assistant_text"] = any(
            m.get("role") == "assistant" and (m.get("content") or "").strip() for m in msgs
        )
        facts["has_tool_calls"] = any(m.get("tool_calls") for m in msgs)
        facts["has_tool_results"] = any(
            m.get("role") == "tool" or m.get("tool_call_id") for m in msgs
        )
        facts["has_tool_name"] = any(m.get("tool_name") for m in msgs)
        facts["has_reasoning"] = any(m.get("reasoning") or m.get("reasoning_content") for m in msgs)
        facts["has_token_counts"] = any(m.get("token_count") for m in msgs)
        facts["has_display_metadata"] = any(m.get("display_metadata") for m in msgs)
        approval_like = [
            m for m in msgs
            if any(h in json.dumps(m, default=str).lower() for h in ("approval", "permission", "denied by user"))
        ]
        facts["messages_mentioning_approval"] = len(approval_like)
        r.add("json", "message sample", [
            {k: clip(str(v), 240) for k, v in m.items() if v not in (None, "", 0)}
            for m in msgs[:6]
        ])
    r.add("json", "completeness", facts)

    if not msg_cols:
        r.measured("会话在 state.db 中，但没有 messages 表可核对内容")
        return
    if facts.get("message_count", 0) == 0:
        r.measured(
            "会话已登记但没有消息行（未开 --live 时正常）；"
            "内容级完整度需 --live 重跑"
        )
        return
    missing = [k for k in ("has_tool_calls", "has_tool_results") if not facts.get(k)]
    r.measured(
        f"消息 {facts['message_count']} 条，roles={facts['roles']}；"
        f"工具调用={facts['has_tool_calls']}，工具结果={facts['has_tool_results']}，"
        f"推理={facts['has_reasoning']}，token={facts['has_token_counts']}"
        + (f"；缺失: {missing}" if missing else "")
    )


# --------------------------------------------------------------------------
# Q2 interop
# --------------------------------------------------------------------------


def check_interop(ctx: Ctx, session_ids: dict[str, str | None]) -> dict:
    """Can `hermes chat --resume <id>` pick up a protocol-created session?"""
    path = "native"
    out: dict = {}
    hermes = ctx.facts.get("hermes_bin", "hermes")

    if ctx.opts.dry_run:
        # Show the full plan even though no id exists yet.
        session_ids = {k: (v or "<session-id>") for k, v in (session_ids or {}).items()} or {
            "tui_ws": "<session-id>"
        }

    # -- direction 1: protocol -> CLI ----------------------------------
    for label, sid in session_ids.items():
        vis = ctx.result(f"interop.visible.{label}", "Q2", path,
                         f"{label} 创建的 session 是否出现在 `hermes sessions list`")
        if not sid:
            vis.untested(f"{label} 没有产出 session id")
            continue
        argv = ctx.sandbox.hermes_argv(hermes, ["sessions", "list", "--limit", "50"])
        res = run_cmd(ctx, argv, env=ctx.sandbox.env(), timeout=40)
        vis.add("stdout", "hermes sessions list", clip(res.stdout, 3000))
        vis.add("stderr", "stderr", clip(res.stderr, 800))
        if res.skipped:
            vis.untested("dry-run")
        elif res.timed_out:
            vis.untested("hermes sessions list 超时")
        elif sid in res.stdout:
            vis.measured(f"{sid} 在 sessions list 中可见 —— 与 CLI 共用同一存储")
        elif res.ok:
            vis.unsupported(
                f"{sid} 不在 `hermes sessions list` 输出中（命令本身成功）。"
                "该路径的会话对 CLI 不可见"
            )
        else:
            vis.untested(f"命令失败: {res.brief()} {clip(res.stderr, 300)}")

        ex = ctx.result(f"interop.export.{label}", "Q2", path,
                        f"`hermes sessions export --session-id` 能否导出 {label} 的会话")
        dest = ctx.sandbox.workdir / f"export-{label}.jsonl"
        argv = ctx.sandbox.hermes_argv(
            hermes, ["sessions", "export", str(dest), "--session-id", sid]
        )
        res = run_cmd(ctx, argv, env=ctx.sandbox.env(), timeout=60)
        ex.add("stdout", "export", clip(res.stdout + res.stderr, 1500))
        if res.skipped:
            ex.untested("dry-run")
        elif dest.is_file() and dest.stat().st_size > 0:
            text = dest.read_text(encoding="utf-8", errors="replace")
            ex.measured(f"导出 {dest.stat().st_size} 字节")
            ex.add("json", "export head", clip(text, 3000))
            out.setdefault("exports", {})[label] = _export_shape(text)
            ex.add("json", "export shape", out["exports"][label])
        elif dest.is_file():
            # An empty export is not a failure: a session with no turns has
            # nothing to write.  It still proves the id was accepted.
            ex.measured(
                "导出命令接受了该 session id，但文件为空 —— 该会话没有消息行"
                "（未开 --live 时正常）。内容级完整度需 --live 重跑"
            )
            out.setdefault("exports", {})[label] = {"lines": 0, "empty": True}
        else:
            ex.unsupported(f"导出未产生文件（{res.brief()}）：{clip(res.stdout + res.stderr, 300)}")

        rs = ctx.result(f"interop.resume.{label}", "Q2", path,
                        f"`hermes chat --resume {{{label} 的 id}}` 能否续接")
        argv = ctx.sandbox.hermes_argv(hermes, ["chat", "--resume", sid])
        # A sandbox with no provider is a *feature* here: `-q ... --oneshot`
        # resolves the session id, then dies on "no provider" before any model
        # call.  The error we get back tells us which of the two failed, and it
        # costs nothing.  Only fall back to the stdin-closed probe when a real
        # provider is configured and we are not allowed to spend tokens.
        no_provider_sandbox = not ctx.opts.seed_config
        use_oneshot = ctx.opts.live or no_provider_sandbox
        if use_oneshot:
            argv += ["-q", "hermes-probe-resume-check", "--oneshot"]
        res = run_cmd(
            ctx, argv, env=ctx.sandbox.env(), timeout=60,
            stdin_text=None if use_oneshot else "",
        )
        blob = (res.stdout + "\n" + res.stderr)
        rs.add("stdout", "chat --resume", clip(blob, 3000))
        rs.add("note", "probe shape", {
            "argv_tail": argv[-3:],
            "sandbox_has_provider_config": bool(ctx.opts.seed_config),
            "live": ctx.opts.live,
        })
        low = blob.lower()
        not_found = _RESUME_NOT_FOUND_RE.search(blob)
        provider_gap = _PROVIDER_MISSING_RE.search(blob)
        if res.skipped:
            rs.untested("dry-run")
        elif not_found:
            rs.unsupported(
                f"CLI 明确报找不到该 session（『{not_found.group(0)}』）—— "
                f"{label} 创建的会话对 `chat --resume` 不可见"
            )
        elif provider_gap:
            # The decisive non-live signal: the CLI got past id resolution and
            # only then hit the missing provider.
            rs.measured(
                f"CLI 解析该 id 成功后才因『{provider_gap.group(0)}』退出 —— "
                "报错发生在 session 查找之后，证明 `chat --resume` 认得这个 id。"
                "（沙盒无 provider，本判定零 token 成本）"
            )
        elif res.rc == 0:
            rs.measured(f"退出码 0，未报 session 不存在（{res.brief()}）")
        elif res.timed_out:
            rs.measured(
                "没有立刻报 'Session not found'，而是进入交互态直到超时 —— "
                "『该 id 被接受』的弱证据"
            )
        else:
            rs.untested(
                f"退出码 {res.rc}，既没报 session 不存在也没报缺 provider，无法判定："
                f"{clip(blob, 400)}"
            )

    # -- direction 2: CLI -> protocol -----------------------------------
    cr = ctx.result("interop.cli_created", "Q2", path,
                    "反向：CLI 创建的 session 能否被各协议路径 resume")
    if not ctx.opts.live:
        cr.untested(
            "未开 --live：创建一条带内容的 CLI 会话需要真实模型调用。"
            "反向验收必须在 --live 下重跑（这是 R-07 判据的另一半）"
        )
        return out
    argv = ctx.sandbox.hermes_argv(hermes, ["chat", "-q", "Reply with: CLI-PROBE-OK", "--oneshot"])
    res = run_cmd(ctx, argv, env=ctx.sandbox.env(), timeout=180)
    cr.add("stdout", "hermes chat --oneshot", clip(res.stdout + res.stderr, 2000))
    if not res.ok:
        cr.untested(f"CLI 会话创建失败: {res.brief()} {clip(res.stderr, 400)}")
        return out
    ls = run_cmd(ctx, ctx.sandbox.hermes_argv(hermes, ["sessions", "list", "--limit", "5"]),
                 env=ctx.sandbox.env(), timeout=40)
    ids = find_session_ids(ls.stdout)
    cr.add("stdout", "sessions list", clip(ls.stdout, 2000))
    if not ids:
        cr.untested("无法从 sessions list 解析出新建的 CLI session id")
        return out
    cli_sid = ids[0]
    out["cli_session_id"] = cli_sid
    cr.measured(
        f"CLI 会话 {cli_sid} 已创建；该 id 需要在下一轮由各协议路径尝试 resume "
        "（run_probe.py 会在 --live 下把它回灌给 TUI Gateway / ACP / HTTP 三条路径）"
    )
    return out


def _export_shape(text: str) -> dict:
    """Summarise what a sessions-export JSONL actually carries."""
    shape = {
        "lines": 0,
        "top_level_keys": [],
        "roles": [],
        "has_tool_calls": False,
        "has_tool_results": False,
        "has_reasoning": False,
        "has_usage": False,
        "has_approval_trace": False,
    }
    keys: set[str] = set()
    roles: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        shape["lines"] += 1
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            keys.update(obj.keys())
            role = obj.get("role")
            if isinstance(role, str):
                roles.add(role)
            blob = json.dumps(obj, default=str).lower()
            shape["has_tool_calls"] |= bool(obj.get("tool_calls")) or '"tool_calls"' in blob
            shape["has_tool_results"] |= role == "tool" or bool(obj.get("tool_call_id"))
            shape["has_reasoning"] |= bool(obj.get("reasoning") or obj.get("reasoning_content"))
            shape["has_usage"] |= "token" in blob or "usage" in blob
            shape["has_approval_trace"] |= "approval" in blob or "permission" in blob
    shape["top_level_keys"] = sorted(keys)[:40]
    shape["roles"] = sorted(roles)
    return shape


# --------------------------------------------------------------------------
# Q5 out-of-band write detection
# --------------------------------------------------------------------------


def check_out_of_band(ctx: Ctx, sid: str | None) -> dict:
    """How fast can an adapter notice a write it did not make?"""
    path = "native"
    out: dict = {}
    hermes = ctx.facts.get("hermes_bin", "hermes")
    r = ctx.result("oob.detection", "Q5", path, "带外 CLI 写入的可检测性与时延")

    db = ctx.sandbox.state_db
    if ctx.opts.dry_run:
        marker = "probe-oob-<timestamp>"
        ctx.plan("cmd", " ".join(ctx.sandbox.hermes_argv(
            hermes, ["sessions", "rename", sid or "<sid>", marker])) +
            "   # 带外写入（不调用模型）")
        ctx.plan("poll", f"stat {db} / {db}-wal 每 100ms，最多 20s")
        r.untested("dry-run")
        return out
    if not sid:
        r.untested("没有可用于带外写入的 session id")
        return out
    if not db.is_file():
        r.untested(f"{db} 不存在，无法测量")
        return out

    baseline = ctx.sandbox.db_signals()
    base_title = _read_title(db, sid)
    marker = f"probe-oob-{int(time.time())}"
    out["marker"] = marker
    out["baseline"] = baseline

    argv = ctx.sandbox.hermes_argv(hermes, ["sessions", "rename", sid, marker])
    t0 = time.time()
    res = run_cmd(ctx, argv, env=ctx.sandbox.env(), timeout=60)
    write_done = time.time()
    r.add("stdout", "hermes sessions rename", clip(res.stdout + res.stderr, 1200))
    if not res.ok:
        r.untested(
            f"带外写入命令失败（{res.brief()}）；改用 --live 让 `hermes chat --resume ... "
            f"-q ... --oneshot` 制造带外写入后重跑"
        )
        return out

    signals: dict[str, float | None] = {
        "state.db mtime": None,
        "state.db size": None,
        "state.db-wal mtime": None,
        "state.db-wal size": None,
        "sql readback": None,
    }
    deadline = time.time() + 20.0
    while time.time() < deadline and any(v is None for v in signals.values()):
        now = ctx.sandbox.db_signals()
        for fname, key_m, key_s in (
            ("state.db", "state.db mtime", "state.db size"),
            ("state.db-wal", "state.db-wal mtime", "state.db-wal size"),
        ):
            b = baseline.get(fname, {})
            c = now.get(fname, {})
            if c.get("exists"):
                if signals[key_m] is None and b.get("mtime") != c.get("mtime"):
                    signals[key_m] = time.time() - t0
                if signals[key_s] is None and b.get("size") != c.get("size"):
                    signals[key_s] = time.time() - t0
        if signals["sql readback"] is None:
            title = _read_title(db, sid)
            if title and title != base_title and marker in str(title):
                signals["sql readback"] = time.time() - t0
        time.sleep(0.05)

    out["latency_seconds"] = {k: (round(v, 3) if v is not None else None) for k, v in signals.items()}
    out["write_command_seconds"] = round(write_done - t0, 3)
    out["after"] = ctx.sandbox.db_signals()
    r.add("json", "detection latency (seconds from issuing the write)", out["latency_seconds"])
    r.add("json", "file signals before/after", {"before": baseline, "after": out["after"]})

    fired = [k for k, v in signals.items() if v is not None]
    never = [k for k, v in signals.items() if v is None]
    if fired:
        r.measured(
            "可检测信号: " + ", ".join(f"{k}={out['latency_seconds'][k]}s" for k in fired)
            + ("；20s 内始终未变: " + ", ".join(never) if never else "")
            + f"（写入命令本身耗时 {out['write_command_seconds']}s，是时延下限）"
        )
        r.add("note", "对 R-04 的含义",
              "WAL 模式下 state.db 主文件的 mtime 可能直到 checkpoint 才变；"
              "软提示检测器必须同时看 -wal 文件或直接做只读 SQL 回读，"
              "只 stat state.db 会漏报")
    else:
        r.unsupported("20s 内没有任何文件或 SQL 信号变化 —— 带外写入不可被轮询检测")
    return out


def _read_title(db: Path, sid: str) -> str | None:
    try:
        conn = _connect_ro(db)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Q4 second process
# --------------------------------------------------------------------------


def check_two_gateways(ctx: Ctx) -> dict:
    """Can two backend processes share one HERMES_HOME (R-10 topology)?"""
    from .checks_gateway import start_serve
    from .httpc import wait_for_http

    r = ctx.result("native.two_processes", "Q4", "native",
                   "同一 HERMES_HOME 上并存两个 hermes serve 进程")
    if not ctx.opts.allow_two_processes:
        r.untested("--no-two-processes")
        return {}
    if ctx.opts.home_mode == "profile":
        r.untested(
            "profile 模式下不做：官方文档明确警告『不要让两个 agent 进程指向同一 profile』，"
            "两者都会自动写记忆。仅在 isolated 沙盒里测"
        )
        return {}
    from . import discovery

    if discovery.has_sub(ctx, "serve") is False:
        r.unsupported("没有 `hermes serve` 子命令")
        return {}
    a = start_serve(ctx)
    if a is None:
        r.untested("dry-run")
        return {}
    b = None
    try:
        ra = wait_for_http(f"http://127.0.0.1:{a.port}/api/status", timeout=45)
        if not ra.status:
            r.untested(f"第一个 serve 未就绪：{ra.brief()}")
            return {}
        b = start_serve(ctx)
        rb = wait_for_http(f"http://127.0.0.1:{b.port}/api/status", timeout=45)
        r.add("http", "serve #1 /api/status", ra.to_json())
        r.add("http", "serve #2 /api/status", rb.to_json())
        r.add("stdout", "serve #2 output", b.text())
        if rb.status:
            r.measured(
                f"两个 serve 进程（端口 {a.port} / {b.port}）可同时对同一 HERMES_HOME 服务 —— "
                "R-10『单 gateway 多 session』不是被进程模型强制的；"
                "SQLite WAL 允许多读单写，但两个进程各自持有独立会话运行时"
            )
        else:
            r.unsupported(
                f"第二个 serve 无法就绪（{rb.brief()}）—— 每个 HERMES_HOME 只允许一个后端进程，"
                "R-10 的常驻单网关拓扑是被强制的，不是可选的"
            )
    finally:
        a.stop()
        if b is not None:
            b.stop()
    return {}
