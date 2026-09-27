"""Render probe-report.md and probe-report.json.

The Markdown is written for the architecture reviewer, not for a machine: every
row carries the tri-state, and the last section maps findings onto the decisions
they gate (R-02 / R-04 / R-07 / R-10 and 追加问题 16-22).
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path

from .util import MEASURED, NOT_TESTED, STATE_CN, STATE_MARK, UNSUPPORTED, Ctx, clip, redact

QUESTION_TITLES = {
    "Q1": "1. 三条路径各自是否可用（session / 流 / tool / permission / interrupt / usage）",
    "Q2": "2. 跨协议互通（与 `hermes chat --resume` 双向续接）",
    "Q3": "3. 原生历史完整度（决定 R-02 是否需要可丢弃缓存）",
    "Q4": "4. 网关多端接入（决定 R-04 能否升级为单写入者）",
    "Q5": "5. 带外写入可检测性与时延",
    "Q6": "6. 版本 / 可执行文件 / 协议版本",
    "Q0": "0. 探针自身的执行异常",
}

PATH_TITLES = {
    "tui_stdio": "TUI Gateway JSON-RPC · stdio",
    "tui_ws": "TUI Gateway JSON-RPC · WebSocket (`hermes serve` → `/api/ws`)",
    "acp": "ACP · stdio (`hermes acp`)",
    "http_sse": "OpenAI 兼容 HTTP + SSE (`hermes gateway`)",
    "native": "原生存储 / CLI",
    "env": "环境",
}


def _counts(results) -> dict:
    out = {MEASURED: 0, NOT_TESTED: 0, UNSUPPORTED: 0}
    for r in results:
        out[r.state] = out.get(r.state, 0) + 1
    return out


def build_json(ctx: Ctx, sources: list[dict], unverified=None, observed=None) -> dict:
    return {
        "schema": "hermes.protocol-probe.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(time.time() - ctx.started, 1),
        "options": {
            "dry_run": ctx.opts.dry_run,
            "live": ctx.opts.live,
            "home_mode": ctx.opts.home_mode,
            "profile_name": ctx.opts.profile_name,
            "enable_http": ctx.opts.enable_http,
            "allow_two_processes": ctx.opts.allow_two_processes,
            "only": ctx.opts.only,
            "timeout": ctx.opts.timeout,
        },
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "sandbox": ctx.sandbox.to_json() if ctx.sandbox else None,
        "facts": redact(ctx.facts),
        "summary": _counts(ctx.results),
        "results": [r.to_json() for r in ctx.results],
        "doc_sources": sources,
        "unverified": unverified or [],
        "observed_facts": observed or [],
        "plan": ctx.plan_lines,
    }


def _fence(value) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(redact(value), ensure_ascii=False, indent=2, default=str)
    return "```\n" + clip(redact(text), 2600).replace("```", "``\u200b`") + "\n```"


def build_markdown(ctx: Ctx, sources: list[dict], unverified=None) -> str:
    L: list[str] = []
    counts = _counts(ctx.results)
    sb = ctx.sandbox

    L.append("# Hermes 协议探针报告")
    L.append("")
    L.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    L.append(f"- 运行模式：{'DRY-RUN（只打印命令，不执行）' if ctx.opts.dry_run else '实跑'}"
             f" / {'--live（允许真实模型调用）' if ctx.opts.live else '非 live（不产生 token 费用）'}")
    L.append(f"- 结果计数：实测 {counts[MEASURED]} · 未测 {counts[NOT_TESTED]} · 不支持 {counts[UNSUPPORTED]}")
    if sb:
        L.append(f"- 沙盒 HERMES_HOME：`{sb.home}`（模式 `{sb.mode}`"
                 + (f"，profile `{sb.profile}`" if sb.profile else "") + "）")
        L.append(f"- 用户真实 home `{sb.real_home}` 未被读写"
                 "（唯一例外：显式传入 `--seed-config` 的那一个文件）")
    L.append("")
    L.append("> 三态含义：**实测结果** = 探针跑通并取得证据；**未测** = 条件不具备（缺 --live、"
             "缺依赖、上一步没拿到 id 等），不构成否定；**不支持** = 目标端明确拒绝或该能力不存在。")
    L.append("")

    # environment ------------------------------------------------------
    L.append("## 环境")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    for key, label in (
        ("hermes_bin", "hermes 可执行文件"),
        ("hermes_bin_realpath", "realpath"),
        ("hermes_version", "Hermes 版本"),
        ("hermes_python", "Hermes 使用的解释器"),
        ("hermes_install_dir", "Install directory"),
        ("hermes_commit", "Commit"),
        ("ws_handshake_variant", "/api/ws 通过的握手形状"),
        ("acp_protocol_version", "ACP protocolVersion（实测）"),
        ("serve_port", "hermes serve 监听端口"),
    ):
        if ctx.facts.get(key) is not None:
            L.append(f"| {label} | `{redact(str(ctx.facts[key]))}` |")
    L.append(f"| 探针主机 | {platform.platform()} / Python {sys.version.split()[0]} |")
    L.append("")
    if ctx.facts.get("subcommand_present"):
        present = ctx.facts["subcommand_present"]
        L.append("子命令探测：" + ", ".join(
            f"`{k}`{'✓' if v else '✗'}" for k, v in present.items()))
        L.append("")

    # per question -----------------------------------------------------
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q0"):
        group = [r for r in ctx.results if r.question == q]
        if not group:
            continue
        L.append(f"## {QUESTION_TITLES[q]}")
        L.append("")
        if q == "Q1":
            for path in ("tui_stdio", "tui_ws", "acp", "http_sse"):
                rows = [r for r in group if r.path == path]
                if not rows:
                    continue
                L.append(f"### {PATH_TITLES.get(path, path)}")
                L.append("")
                L.extend(_table(rows))
                L.append("")
        else:
            L.extend(_table(group, show_path=True))
            L.append("")

    # evidence ---------------------------------------------------------
    L.append("## 证据附录")
    L.append("")
    L.append("按结果 id 排列；命令与响应已做凭据脱敏。")
    L.append("")
    for r in ctx.results:
        if not r.evidence:
            continue
        L.append(f"<details><summary><code>{r.id}</code> — {r.title} "
                 f"[{STATE_CN[r.state]}]</summary>")
        L.append("")
        if r.detail:
            L.append(f"**结论：** {redact(r.detail)}")
            L.append("")
        if r.reason:
            L.append(f"**原因：** {redact(r.reason)}")
            L.append("")
        for ev in r.evidence:
            L.append(f"*{ev.kind} · {ev.label}*")
            L.append("")
            L.append(_fence(ev.value))
            L.append("")
        L.append("</details>")
        L.append("")

    # decisions --------------------------------------------------------
    L.append("## 对架构决策的指向")
    L.append("")
    L.extend(_decisions(ctx))
    L.append("")

    # commands executed ------------------------------------------------
    L.append("## 本次实际执行/计划的动作")
    L.append("")
    L.append("```")
    L.extend(redact(line) for line in ctx.plan_lines[:300])
    if len(ctx.plan_lines) > 300:
        L.append(f"... 另有 {len(ctx.plan_lines) - 300} 行")
    L.append("```")
    L.append("")

    # cleanup ----------------------------------------------------------
    L.append("## 清理")
    L.append("")
    if sb:
        for hint in sb.cleanup_instructions():
            L.append(f"```bash\n{hint}\n```")
        if sb.mode == "profile":
            L.append("")
            L.append("> profile 模式在用户真实 `~/.hermes/profiles/` 下创建了目录。"
                     "确认报告无误后按上面的命令删除；探针不会自动删真实 home 下的任何东西。")
    L.append("")

    # sources ----------------------------------------------------------
    L.append("## 资料来源（探针设计依据）")
    L.append("")
    L.append("| 事实 | 来源 |")
    L.append("|---|---|")
    for s in sources:
        L.append(f"| {s['fact']} | {s['url']} |")
    L.append("")
    L.append("> 文档与实测冲突时以本报告的『实测结果』为准。")
    L.append("")
    if unverified:
        L.append("### 官方文档没有写、必须由探针发现的事项（未验证）")
        L.append("")
        for item in unverified:
            L.append(f"- {item}")
        L.append("")
    L.append("")
    return "\n".join(L)


def _table(rows, show_path: bool = False) -> list[str]:
    if show_path:
        out = ["| 路径 | 项 | 状态 | 结论 / 原因 |", "|---|---|---|---|"]
    else:
        out = ["| 项 | 状态 | 结论 / 原因 |", "|---|---|---|"]
    for r in rows:
        text = redact(r.detail if r.state == MEASURED else (r.reason or r.detail)) or "—"
        text = text.replace("|", "\\|").replace("\n", " ")
        cells = [r.title, f"{STATE_MARK[r.state]} {STATE_CN[r.state]}", clip(text, 420)]
        if show_path:
            cells.insert(0, f"`{r.path}`")
        out.append("| " + " | ".join(cells) + " |")
    return out


def _get(ctx: Ctx, rid: str):
    for r in ctx.results:
        if r.id == rid:
            return r
    return None


def _verdict(ctx: Ctx, rid: str) -> str:
    r = _get(ctx, rid)
    if r is None:
        return "未测（该检查未运行）"
    return f"{STATE_CN[r.state]} — {clip(redact(r.detail or r.reason) or '—', 260)}"


def _decisions(ctx: Ctx) -> list[str]:
    L: list[str] = []

    L.append("### R-02 / 追加问题 21：仪表盘只当窗口，还是要补可丢弃缓存？")
    L.append("")
    L.append(f"- 原生存储 schema：{_verdict(ctx, 'native.state_db')}")
    L.append(f"- 权限决策是否入账本：{_verdict(ctx, 'native.approval_columns')}")
    for r in ctx.results:
        if r.id.startswith("native.history."):
            L.append(f"- {r.title}：{STATE_CN[r.state]} — {clip(redact(r.detail or r.reason), 240)}")
    L.append("")
    L.append("判据：若『权限决策』一行是 **不支持**，则 R-02 的回退条款触发 —— "
             "卡片要回放审批过程就必须补一层可丢弃缓存（只存缺失部分）。若工具调用与工具结果"
             "均为实测存在，则 `conversation_messages` 不必建表。")
    L.append("")

    L.append("### R-04 / 追加问题 22：软提示，还是升级为单写入者？")
    L.append("")
    L.append(f"- 同一 session 双客户端 attach：{_verdict(ctx, 'tui_ws.multi_attach')}")
    L.append(f"- 同 HERMES_HOME 双后端进程：{_verdict(ctx, 'native.two_processes')}")
    L.append(f"- 带外写入检测：{_verdict(ctx, 'oob.detection')}")
    L.append("")
    L.append("判据：只有当双客户端 attach 且事件**双向广播**为实测结果时，R-04 才可升级为"
             "单写入者；否则软提示方案保留，且其检测器必须按 Q5 实测到的信号（大概率是 "
             "`state.db-wal` 而非 `state.db`）实现。")
    L.append("")

    L.append("### R-07 / 追加问题 16：跨协议 session 互通是否成立？")
    L.append("")
    for path in ("tui_stdio", "tui_ws", "acp", "http_sse"):
        vis = _get(ctx, f"interop.visible.{path}")
        res = _get(ctx, f"interop.resume.{path}")
        if vis or res:
            L.append(
                f"- **{PATH_TITLES.get(path, path)}** → CLI 可见：{STATE_CN[vis.state] if vis else '未测'}"
                f"；`chat --resume`：{STATE_CN[res.state] if res else '未测'}"
            )
    L.append(f"- ACP 跨进程 load：{_verdict(ctx, 'acp.session.load.cross_process')}")
    L.append(f"- 反向（CLI → 协议）：{_verdict(ctx, 'interop.cli_created')}")
    L.append("")
    L.append("判据：§8.1『统一逻辑身份』只对**双向都成立**的路径成立。任何一条路径若其会话"
             "不落在同一 state.db，就不能作为 Card 链路，除非接受 Conversation 与 native "
             "session 之间多一层映射。")
    L.append("")

    L.append("### R-10 / 追加问题 17：进程拓扑")
    L.append("")
    L.append(f"- 双 `hermes serve`：{_verdict(ctx, 'native.two_processes')}")
    L.append(f"- `hermes serve` 启动与 ready sentinel：{_verdict(ctx, 'tui_ws.launch')}")
    L.append(f"- stdio 网关入口：{_verdict(ctx, 'tui_stdio.launch')}")
    L.append("")
    L.append("判据：若 stdio 网关没有对外命令入口（只有 `hermes serve` 的 WebSocket 面），"
             "则 R-10 的『每 Conversation 一进程』回退形态实际不可实现，常驻网关不是优选"
             "而是唯一形态。")
    L.append("")
    return L


def write_reports(ctx: Ctx, sources: list[dict], out_dir: Path,
                  unverified=None, observed=None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "probe-report.json"
    md = out_dir / "probe-report.md"
    js.write_text(
        json.dumps(build_json(ctx, sources, unverified, observed),
                   ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    md.write_text(build_markdown(ctx, sources, unverified) + "\n", encoding="utf-8")
    return md, js
