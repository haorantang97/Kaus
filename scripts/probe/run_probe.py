#!/usr/bin/env python3
"""Hermes 协议探针 — 在真实安装的 Hermes 上回答架构决策所需的六组问题。

设计约束（见 scripts/probe/README.md）：

* 只用标准库；`websockets` 可选，缺失时自动降级到内置 RFC6455 客户端。
* 只在隔离的 HERMES_HOME 或 `probe-` 前缀的专用 profile 中运行；
  绝不读写用户真实 profile 的会话与密钥。
* 每一步先打印将执行的命令；`--dry-run` 时只打印不执行。
* 任何一步失败都打印可读原因并继续下一项；结果分 实测 / 未测 / 不支持 三态。

输出：probe-report.md + probe-report.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probelib import (  # noqa: E402
    checks_acp,
    checks_gateway,
    checks_http,
    checks_native,
    discovery,
    report,
    sandbox as sandbox_mod,
    sources as sources_mod,
)
from probelib.util import (  # noqa: E402
    MEASURED,
    NOT_TESTED,
    STATE_CN,
    UNSUPPORTED,
    Ctx,
    Options,
    ProbeError,
    run_check,
)

BANNER = r"""
+--------------------------------------------------------------+
|  Hermes 协议探针 / protocol-probe                             |
|  三条路径 · 跨协议互通 · 历史完整度 · 多端接入 · 带外写入      |
+--------------------------------------------------------------+
"""


def parse_args(argv: list[str]) -> Options:
    p = argparse.ArgumentParser(
        prog="run_probe.py",
        description="探测本机 Hermes 的三条结构化协议路径，产出决策用实测报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型用法：\n"
            "  python3 scripts/probe/run_probe.py --dry-run        # 先看会做什么\n"
            "  python3 scripts/probe/run_probe.py                  # 实跑，不花 token\n"
            "  python3 scripts/probe/run_probe.py --live           # 加上真实回合（会计费）\n"
            "  python3 scripts/probe/run_probe.py --self-test      # 离线自检，不碰真 Hermes\n"
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将执行的命令与请求，不执行任何一步")
    p.add_argument("--live", action="store_true",
                   help="允许会真实调用模型的步骤（文本流 / tool 事件 / 权限请求 / 反向互通）。"
                        "会产生 token 费用")
    p.add_argument("--home-mode", choices=["isolated", "profile"], default="isolated",
                   help="isolated（默认）= 临时 HERMES_HOME；profile = 在真实 ~/.hermes 下建 probe- 前缀 profile")
    p.add_argument("--profile-name", default="probe-sandbox",
                   help="profile 模式下的沙盒 profile 名（必须以 probe- 开头）")
    p.add_argument("--seed-config", default="",
                   help="显式指定一个 config.yaml 拷进沙盒（探针会先扫描它是否含凭据，"
                        "含则拒绝）。不传则沙盒无模型配置，--live 步骤会报『未测』")
    p.add_argument("--hermes-bin", default="", help="hermes 可执行文件路径（默认自动解析）")
    p.add_argument("--tui-stdio-cmd", default="",
                   help="覆盖 TUI Gateway 的 stdio 启动命令（文档未给出该命令，探针默认逐个试候选）")
    p.add_argument("--no-http", action="store_true", help="跳过 OpenAI 兼容 HTTP+SSE 路径")
    p.add_argument("--no-two-processes", action="store_true",
                   help="跳过『同一 HERMES_HOME 上并存两个后端进程』的检查")
    p.add_argument("--timeout", type=float, default=25.0, help="单条命令默认超时（秒）")
    p.add_argument("--out", default=".", help="报告输出目录（默认当前目录）")
    p.add_argument("--keep", action="store_true", help="结束后保留沙盒目录（默认自动删除临时目录）")
    p.add_argument("--only", default="",
                   help="逗号分隔，只跑指定检查组："
                        "env,tui_stdio,tui_ws,acp,http,native,interop,oob,two_processes")
    p.add_argument("--self-test", action="store_true",
                   help="用内置假 Hermes 跑一遍全流程（离线自检，不接触真实 Hermes）")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    opts = Options(
        dry_run=a.dry_run,
        live=a.live,
        timeout=a.timeout,
        home_mode=a.home_mode,
        profile_name=a.profile_name,
        keep=a.keep,
        enable_http=not a.no_http,
        allow_two_processes=not a.no_two_processes,
        seed_config=a.seed_config,
        hermes_bin=a.hermes_bin,
        tui_stdio_cmd=a.tui_stdio_cmd,
        out_dir=a.out,
        verbose=a.verbose,
        only=[s.strip() for s in a.only.split(",") if s.strip()],
    )
    opts.self_test = a.self_test  # type: ignore[attr-defined]
    return opts


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    print(BANNER)

    if getattr(opts, "self_test", False):
        fake = Path(__file__).resolve().parent / "fixtures" / "fake_hermes.py"
        if not fake.is_file():
            print(f"!! 找不到自检 fixture: {fake}")
            return 2
        opts.hermes_bin = str(fake)
        opts.home_mode = "isolated"
        os.environ["HERMES_PROBE_SELFTEST"] = "1"
        print(f"自检模式：hermes = {fake}（假实现，不接触真实 Hermes）\n")

    ctx = Ctx(opts)
    ctx.log(f"模式: {'DRY-RUN' if opts.dry_run else '实跑'}"
            f" | live={'开' if opts.live else '关（不产生 token 费用）'}"
            f" | home-mode={opts.home_mode}")

    # ---- sandbox ----------------------------------------------------
    ctx.step("沙盒准备（隔离 HERMES_HOME；用户真实 profile 不被读写）")
    try:
        ctx.sandbox = sandbox_mod.Sandbox(ctx).build()
    except ProbeError as exc:
        ctx.log(f"!! 沙盒无法建立: {exc}")
        return 2
    ctx.log(f"  HERMES_HOME = {ctx.sandbox.home}")
    ctx.log(f"  工作目录     = {ctx.sandbox.workdir}")
    for note in ctx.sandbox.notes:
        ctx.log(f"  note: {note}")

    session_ids: dict[str, str | None] = {}

    # ---- Q6 environment ---------------------------------------------
    run_check(ctx, "env", lambda: discovery.probe_environment(ctx))

    # ---- Q1 three paths ---------------------------------------------
    def _tui_stdio() -> None:
        session_ids["tui_stdio"] = checks_gateway.check_tui_stdio(ctx).get("session_id")

    def _tui_ws() -> None:
        session_ids["tui_ws"] = checks_gateway.check_tui_ws(ctx).get("session_id")

    def _acp() -> None:
        session_ids["acp"] = checks_acp.check_acp(ctx).get("session_id")

    def _http() -> None:
        session_ids["http_sse"] = checks_http.check_http_sse(ctx).get("session_id")

    run_check(ctx, "tui_stdio", _tui_stdio)
    run_check(ctx, "tui_ws", _tui_ws)
    run_check(ctx, "acp", _acp)
    run_check(ctx, "http", _http)

    # ---- Q3 native store --------------------------------------------
    run_check(ctx, "native", lambda: checks_native.check_native_store(ctx, session_ids))

    # ---- Q2 interop ---------------------------------------------------
    run_check(ctx, "interop", lambda: checks_native.check_interop(ctx, session_ids))

    # ---- Q5 out-of-band ----------------------------------------------
    oob_sid = next((s for s in session_ids.values() if s), None)
    run_check(ctx, "oob", lambda: checks_native.check_out_of_band(ctx, oob_sid))

    # ---- Q4 two processes ---------------------------------------------
    run_check(ctx, "two_processes", lambda: checks_native.check_two_gateways(ctx))

    # ---- report -------------------------------------------------------
    ctx.step("生成报告")
    out_dir = Path(opts.out_dir).expanduser().resolve()
    md, js = report.write_reports(
        ctx,
        sources_mod.SOURCES,
        out_dir,
        unverified=sources_mod.UNVERIFIED,
        observed=sources_mod.OBSERVED,
    )
    ctx.log(f"  {md}")
    ctx.log(f"  {js}")

    counts = {MEASURED: 0, NOT_TESTED: 0, UNSUPPORTED: 0}
    for r in ctx.results:
        counts[r.state] = counts.get(r.state, 0) + 1
    ctx.step("结果概览")
    for state in (MEASURED, UNSUPPORTED, NOT_TESTED):
        ctx.log(f"  {STATE_CN[state]}: {counts[state]}")
    for r in ctx.results:
        if r.state == UNSUPPORTED:
            ctx.log(f"  [NO] {r.id}: {r.reason[:160]}")

    # ---- cleanup ------------------------------------------------------
    ctx.step("清理")
    removed = ctx.sandbox.try_cleanup()
    for p in removed:
        ctx.log(f"  已删除 {p}")
    hints = ctx.sandbox.cleanup_instructions()
    if opts.keep or opts.home_mode == "profile":
        ctx.log("  仍需手动清理：")
        for h in hints:
            ctx.log(f"    {h}")
    ctx.log(f"\n用时 {round(time.time() - ctx.started, 1)}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n中断。已完成的部分未写入报告；重跑即可。")
        raise SystemExit(130) from None
