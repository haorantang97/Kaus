#!/usr/bin/env python3
"""Phase 3B 会话 API 的真机冒烟（Mac，用户侧验证）。

它做的事只有一件：**在一台真的装了 Agent 的机器上，把 Phase 3B 的 HTTP/SSE 接口
从头走一遍**——挂 router → 起一个只绑 127.0.0.1 的临时端口 → 建会话 → 发一句话 →
把 SSE 里的公共事件逐条打出来。

和 `hermes_http_smoke.py` 的分工
--------------------------------
那个脚本验的是 **Driver**（翻译对不对）；这个验的是 **接入层**（HTTP 形状、SSE
回放与实时、终态收敛）。两者验的东西不同，缺一不可。

`--driver native-http`：把网关也一起管起来
------------------------------------------
默认 `--home-mode isolated`：脚本自己开一个临时 `HERMES_HOME`、自己拉起
`hermes gateway run`、跑完自己收摊（沙盒护栏与 `--seed-config` 的凭据扫描直接复用
`scripts/probe/probelib/sandbox.py`，不另起一套）。已经有网关在跑就给
`--base-url`，脚本只连不管（规格 §1.6：同一 HERMES_HOME 永不由本项目起第二个）。

一次跑完三轮，末尾打一张验收表：

===============  =========================================================
第 1 轮           基础回合：发一句 → 打印公共事件 → 等终态
第 2 轮           多轮记忆：问「我刚才说了什么」，看回答里有没有第 1 轮的标记
第 3 轮           审批闭环：要求执行一条终端命令 → **实时 deny** →
                 验证 sentinel 文件没被创建
===============  =========================================================

凭据边界（AD-60 / AD-48，硬规矩）
---------------------------------
* **绝不读 `.env`**，也绝不读用户的任何凭据文件；
* 网关需要的模型凭据由**用户在命令前用进程环境变量给**，本脚本只负责不擦掉它们
  （子进程继承当前进程的环境）；`--model-key-env` 只用来在输出里点名「哪几个变量
  应该在」，**只打印变量名，不打印值**；
* 网关自己的 `API_SERVER_KEY` 在隔离模式下由沙盒现场生成，经进程环境变量交给
  Driver（`key_ref = credential-store:<变量名>`），配置里零明文；
* `--backend` 的地址与家目录来自命令行或 `dashboard-config.json` 的 `backends[]`，
  代码里不硬编码任何 agent 命令（AD-53）。

领域库是**临时的**：默认建在 `mktemp` 目录里，跑完即弃，不碰
`state/domain.sqlite3`，因此不会污染生产库。

鉴权（D-17 / AD-66）
--------------------
会话端点要 Origin 校验 + 本地 token。本脚本是**本机非浏览器客户端**：不带
`Origin`，因此只需要 token——它从接入层刚生成的那份 token 文件里读（和领域库同
一个临时目录，跑完即弃），逐个请求带 `Authorization: Bearer`，SSE 也走同一个头。
token 值本身不打印。

用法::

    python3 scripts/session_smoke.py --dry-run
    python3 scripts/session_smoke.py --driver native-http --dry-run

    # 真机（隔离沙盒，脚本自己起网关；模型凭据用进程环境变量给）：
    DEEPSEEK_API_KEY=... python3 scripts/session_smoke.py \\
        --driver native-http --live \\
        --seed-config ~/hermes-probe-config.yaml \\
        --model-key-env DEEPSEEK_API_KEY

    # 连一个已经在跑的网关（key 从进程环境变量取）：
    API_SERVER_KEY=... python3 scripts/session_smoke.py \\
        --driver native-http --live --base-url http://127.0.0.1:8899 --home ~/.hermes

    # 不接真机、只验接口形状：
    python3 scripts/session_smoke.py --driver mock
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "kernel"), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

BANNER = r"""
+--------------------------------------------------------------+
|  Phase 3B 会话 API 冒烟 / session_smoke                       |
|  临时领域库 · 本地端口 · 建会话 → 发一句 → 打印公共事件       |
+--------------------------------------------------------------+
"""

#: 终态事件：见到其中之一就收工（AD-55：终态一定是 run.* 收尾）。
TERMINAL_EVENTS = {"run.completed", "run.failed", "run.interrupted"}

#: 待答交互。默认**不替用户点同意**：真机上自动批准等于替人做决定。
INTERACTION_EVENTS = {
    "permission.requested": "permission",
    "question.requested": "question",
    "authentication.requested": "authentication",
}

#: 第 1 轮要模型原样吐回来的标记；第 2 轮就靠它判断有没有多轮记忆。
MARKER = "SESSION-SMOKE-OK"
DEFAULT_PROMPT = f"Reply with exactly {MARKER} and nothing else."
MULTITURN_PROMPT = "What exactly did I ask you in my previous message? Quote it."
APPROVAL_PROMPT = (
    "Use the terminal/shell tool (not write_file, patch, or Python) to execute exactly "
    "this command: {command}. Do not substitute another tool or command."
)

#: 隔离模式下把网关 key 交给 Driver 用的进程环境变量名（值现场生成，不落配置）。
GATEWAY_KEY_ENV = "SESSION_SMOKE_GATEWAY_KEY"


def pick_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--driver", default="mock", help="mock | native-http | acp")
    parser.add_argument("--backend-id", default=None, help="默认 backend:<driver 名>")
    parser.add_argument("--base-url", default=None, help="native-http：已在跑的网关地址（给了就不自己起）")
    parser.add_argument("--home", default=None, help="native-http：该引擎的家目录")
    parser.add_argument("--command", nargs="*", default=None, help="acp：agent 命令 argv")
    parser.add_argument("--project-slug", default="smoke")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=120.0, help="等终态事件的上限（秒）")
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--db", default=None, help="领域库路径；默认临时目录，跑完即弃")
    parser.add_argument(
        "--auto-answer",
        action="store_true",
        help="自动同意审批/回答提问（只在 mock 或你自己控制的沙盒里用；默认关）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    # --- native-http 的真机路径 ------------------------------------------ #
    parser.add_argument(
        "--live",
        action="store_true",
        help="native-http：允许真的调用模型（会计费）。不加就只跑到 probe + 建会话。",
    )
    parser.add_argument(
        "--home-mode",
        default="isolated",
        choices=["isolated", "profile"],
        help="isolated（默认）= 临时 HERMES_HOME，脚本自己起网关；profile = 真实 ~/.hermes 下的 probe- 前缀 profile。",
    )
    parser.add_argument("--profile-name", default="probe-session-smoke",
                        help="--home-mode profile 时的 profile 名（必须以 probe- 开头）。")
    parser.add_argument("--seed-config", default="",
                        help="拷进沙盒的 config.yaml（唯一允许读用户真实目录的入口；带凭据扫描）。")
    parser.add_argument("--hermes-bin", default="", help="hermes 可执行文件路径。")
    parser.add_argument("--keep", action="store_true", help="保留沙盒目录以便事后查看。")
    parser.add_argument(
        "--key-env",
        default=GATEWAY_KEY_ENV,
        help="网关 API key 所在的**环境变量名**（--base-url 模式下由你自己 export）。",
    )
    parser.add_argument(
        "--model-key-env",
        default="",
        help="逗号分隔的模型凭据**变量名**，只用于在输出里点名在/不在（绝不打印值）。",
    )
    parser.add_argument("--skip-approval", action="store_true", help="不跑第 3 轮审批。")
    parser.add_argument("--skip-multiturn", action="store_true", help="不跑第 2 轮多轮记忆。")
    parser.add_argument("--json", action="store_true", help="末尾追加一份机器可读的验收结果。")
    return parser.parse_args(argv)


def backend_config(args: argparse.Namespace) -> dict[str, Any]:
    """把命令行参数变成 AD-53 的一条 `backends[]` 配置。"""
    # 这个 id 只给本次临时领域库用（跑完即弃）；生产库里叫什么由
    # `dashboard-config.json` 说了算，不由这个脚本决定。
    default_id = "backend:hermes" if args.driver in ("native-http", "hermes-http") else f"backend:{args.driver.split('-')[0]}"
    backend_id = args.backend_id or default_id
    entry: dict[str, Any] = {"id": backend_id, "driver": args.driver}
    if args.base_url:
        entry["base_url"] = args.base_url
    if args.home:
        entry["home"] = args.home
    if args.command:
        entry["command"] = list(args.command)
    if args.driver in ("native-http", "hermes-http"):
        # 凭据只以**变量名**出现（AD-48）；值在请求时从进程环境变量读。
        entry["env_keys"] = [args.key_env]
        entry["key_ref"] = f"credential-store:{args.key_env}"
        entry["mode"] = "adopted"
        if args.hermes_bin:
            entry["hermes_bin"] = args.hermes_bin
    return {"features": {"session_host_v1": True, "session_host_background": False}, "backends": [entry]}


def describe(config: dict[str, Any], args: argparse.Namespace) -> str:
    entry = config["backends"][0]
    native = args.driver in ("native-http", "hermes-http")
    lines = [
        f"  backend        : {entry['id']}（driver={entry['driver']}）",
        f"  base_url       : {entry.get('base_url') or ('（脚本自己起网关，端口现场分配）' if native else '（不适用）')}",
        f"  home           : {entry.get('home') or ('（隔离沙盒，临时目录）' if native else '（不适用）')}",
        f"  project slug   : {args.project_slug}",
        f"  领域库          : {args.db or '临时目录（跑完即弃）'}",
        f"  prompt         : {args.prompt!r}",
        f"  自动答交互      : {'开（会替你点同意）' if args.auto_answer else '关'}",
        "  凭据            : 只走进程环境变量；本脚本不读 .env、不读任何凭据文件",
    ]
    if native:
        lines += [
            f"  home-mode      : {args.home_mode}"
            + ("（脚本自己拉起 hermes 网关，跑完收摊）" if not args.base_url else "（已给 --base-url：只连不管）"),
            f"  网关 key 变量   : {args.key_env}（只用名字；值不打印、不落盘）",
            f"  模型凭据变量    : {model_key_report(args) or '（未点名；子进程继承当前环境）'}",
            f"  计费            : {'--live 已开：会真的调用模型' if args.live else '未加 --live：不发起模型回合'}",
            f"  轮次            : 第 1 轮基础"
            + ("" if args.skip_multiturn else " + 第 2 轮多轮记忆")
            + ("" if args.skip_approval else " + 第 3 轮审批（实时 deny）"),
        ]
    return "\n".join(lines)


def model_key_report(args: argparse.Namespace) -> str:
    """只报「点名的这几个变量在不在」。**绝不打印值。**"""
    names = [n.strip() for n in (args.model_key_env or "").split(",") if n.strip()]
    if not names:
        return ""
    return "，".join(f"{name}={'已设置' if os.environ.get(name) else '未设置'}" for name in names)


# --------------------------------------------------------------------------- #
# 验收记账
# --------------------------------------------------------------------------- #

PASS = "通过"
FAIL = "未通过"
SKIPPED = "未测"


@dataclass
class Row:
    key: str
    title: str
    state: str = SKIPPED
    detail: str = ""

    def line(self) -> str:
        mark = {PASS: "[OK]", FAIL: "[XX]", SKIPPED: "[--]"}[self.state]
        return f"  {mark} {self.key:<12} {self.title:<22} {self.state}" + (
            f" — {self.detail}" if self.detail else ""
        )


@dataclass
class Sheet:
    """末尾那张验收表。`--json` 时同一份内容再吐一遍机器可读的。"""

    rows: list[Row] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def add(self, key: str, title: str) -> Row:
        row = Row(key=key, title=title)
        self.rows.append(row)
        return row

    def failed(self) -> bool:
        return any(row.state == FAIL for row in self.rows)

    def render(self) -> str:
        out = ["", "== 验收表 =="] + [row.line() for row in self.rows]
        out.append(f"  总耗时：{time.monotonic() - self.started:.1f}s")
        return "\n".join(out)

    def to_json(self) -> dict[str, Any]:
        return {
            "elapsedSeconds": round(time.monotonic() - self.started, 3),
            "passed": not self.failed(),
            "rows": [
                {"key": r.key, "title": r.title, "state": r.state, "detail": r.detail}
                for r in self.rows
            ],
        }


@dataclass
class Turn:
    """一轮的结果。`events` 是**公共信封**，不含任何 Backend 私有字段。"""

    label: str
    prompt: str
    events: list[dict] = field(default_factory=list)
    elapsed: float = 0.0
    terminal: str | None = None
    pending_interaction: str | None = None
    error: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for envelope in self.events:
            kind = envelope["event"]["type"]
            tally[kind] = tally.get(kind, 0) + 1
        return tally

    @property
    def text(self) -> str:
        completed = [e for e in self.events if e["event"]["type"] == "message.completed"]
        if completed:
            return completed[-1]["event"].get("text") or ""
        return "".join(
            e["event"].get("text") or ""
            for e in self.events
            if e["event"]["type"] == "message.delta"
        )


# --------------------------------------------------------------------------- #
# 一轮
# --------------------------------------------------------------------------- #


def run_turn(
    client: Any,
    conversation_id: str,
    prompt: str,
    args: argparse.Namespace,
    *,
    label: str,
    deny_permissions: bool = False,
) -> Turn:
    """发一句 → 逐条打印公共事件 → 等终态。返回这一轮的记账。

    `deny_permissions` 时在**流还开着**的时候就把审批答掉：等流结束再答永远闭不了环
    （请求-响应闭环在同一条 run 上，run 停在那儿等）。
    """
    import httpx

    turn = Turn(label=label, prompt=prompt)
    started = time.monotonic()
    print(f"\n--- {label}：{prompt}")
    accepted = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": prompt}
    )
    print(f"    POST messages → {accepted.status_code} {accepted.text}")
    if accepted.status_code != 202:
        turn.error = f"POST messages → {accepted.status_code}"
        turn.elapsed = time.monotonic() - started
        return turn

    # 从**本轮之前**的游标接着订阅：不带 ?after= 的话第二轮会把第一轮的事件
    # 整个重放一遍，然后在上一轮的 run.completed 上就收工了（看着像成功，其实
    # 这一轮一个事件都没看）。
    after = accepted.json().get("acceptedAfterSequence")
    query = "" if after is None else f"?after={after}"
    with client.stream(
        "GET",
        f"/api/conversations/{conversation_id}/events{query}",
        timeout=httpx.Timeout(None, read=args.timeout),
    ) as stream:
        for line in stream.iter_lines():
            if line.startswith(":"):
                print("    · keepalive")
                continue
            if not line.startswith("data: "):
                continue
            envelope = json.loads(line[len("data: ") :])
            event = envelope["event"]
            turn.events.append(envelope)
            print(
                f"    #{envelope['sequence']:>3} {event['type']:<24}"
                f" run={envelope.get('runId')}"
            )
            if event["type"] in TERMINAL_EVENTS:
                turn.terminal = event["type"]
                break
            kind = INTERACTION_EVENTS.get(event["type"])
            if kind is not None:
                request_id = event["request"]["requestId"]
                if deny_permissions and kind == "permission":
                    answered = client.post(
                        f"/api/conversations/{conversation_id}/interactions/{request_id}",
                        json={"kind": kind, "cancelled": True},
                    )
                    print(f"    ↩ 已实时拒绝 {request_id} → {answered.status_code}")
                elif args.auto_answer:
                    answered = client.post(
                        f"/api/conversations/{conversation_id}/interactions/{request_id}",
                        json={"kind": kind, "optionId": "allow", "text": "ok"},
                    )
                    print(f"    ↩ 已自动作答 {request_id} → {answered.status_code}")
                else:
                    print(
                        f"    ⏸ 停在待答{kind}（{request_id}）——"
                        "没开 --auto-answer，不替你做决定"
                    )
                    turn.pending_interaction = request_id
                    break
            if (
                len(turn.events) >= args.max_events
                or time.monotonic() - started > args.timeout
            ):
                break
    turn.elapsed = time.monotonic() - started
    return turn


# --------------------------------------------------------------------------- #
# native-http：沙盒 + 自己拉起的网关
# --------------------------------------------------------------------------- #


@dataclass
class LiveGateway:
    """隔离模式下由本脚本拉起的那个网关（`--base-url` 模式下这里是空壳）。"""

    base_url: str
    home: Path | None = None
    workdir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    process: subprocess.Popen | None = None
    sandbox: Any = None
    cleanup: Callable[[], None] = lambda: None

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except Exception:  # noqa: BLE001 - 收摊路径不许抛
                pass
        self.cleanup()


def start_isolated_gateway(args: argparse.Namespace) -> LiveGateway:
    """临时 `HERMES_HOME` + 自己拉起 `hermes gateway run`（复用探针的沙盒护栏）。

    沙盒的 API key 现场生成，写进**沙盒自己的**配置文件供网关读，同时经进程环境
    变量交给 Driver——本脚本这一侧从头到尾没有读过任何凭据文件。
    """
    import shutil

    sys.path.insert(0, str(REPO_ROOT / "scripts" / "probe"))
    from probelib import sandbox as sandbox_mod
    from probelib.httpc import wait_for_http
    from probelib.util import Ctx as ProbeCtx
    from probelib.util import Options as ProbeOptions
    from probelib.util import register_secret

    import hermes_http_smoke as http_smoke
    from drivers.hermes import redaction

    hermes_bin = args.hermes_bin or shutil.which("hermes") or "hermes"
    if shutil.which(hermes_bin) is None:
        raise RuntimeError(
            f"找不到 hermes 可执行文件（{hermes_bin}）。"
            "--driver native-http 的隔离模式要在装了 Hermes 的机器上跑；"
            "如果网关已经在别处跑着，用 --base-url 直接连它。"
        )

    options = ProbeOptions(
        dry_run=False,
        live=args.live,
        home_mode=args.home_mode,
        profile_name=args.profile_name,
        keep=args.keep,
        seed_config=args.seed_config,
        hermes_bin=hermes_bin,
    )
    ctx = ProbeCtx(options)
    sandbox = sandbox_mod.Sandbox(ctx).build()
    ctx.sandbox = sandbox
    register_secret(sandbox.api_key)
    redaction.register_literal(sandbox.api_key)
    print(f"  HERMES_HOME = {sandbox.home}")
    print(f"  工作目录     = {sandbox.workdir}")
    for note in sandbox.notes:
        print(f"  note: {note}")

    port = pick_free_port()
    sandbox.write_api_server_env(port)
    # Driver 侧只拿到**变量名**：值放进本进程的环境，config 里只有 credential-store 引用。
    os.environ[args.key_env] = sandbox.api_key

    print("  拉起网关（模型凭据由当前进程环境继承；本脚本不读也不打印它们）")
    process = http_smoke.spawn_gateway(hermes_bin, sandbox, port, False)
    health = wait_for_http(f"http://127.0.0.1:{port}/health", timeout=60.0)
    if not health.status:
        detail = health.brief()
        if process is not None and process.poll() is not None:
            stdout, stderr = process.proc.communicate()
            tail = (stderr or stdout or b"").decode("utf-8", "replace")[-2000:]
            detail += f"；gateway 输出：{tail}"
        raise RuntimeError(f"gateway 60 秒内未就绪：{detail}")

    def cleanup() -> None:
        for path in sandbox.try_cleanup():
            print(f"  removed {path}")
        for hint in sandbox.cleanup_instructions():
            print(f"  手动清理：{hint}")

    return LiveGateway(
        base_url=f"http://127.0.0.1:{port}",
        home=sandbox.home,
        workdir=Path(sandbox.workdir),
        process=getattr(process, "proc", None),
        sandbox=sandbox,
        cleanup=cleanup,
    )


def check_approval_outcome(turn: Turn, sentinel: Path) -> tuple[str, str]:
    """审批那一轮的判据：抓到请求 + 拒绝生效 + 目标文件没被创建。"""
    requested = [e for e in turn.events if e["event"]["type"] == "permission.requested"]
    resolved = [e for e in turn.events if e["event"]["type"] == "permission.resolved"]
    if not requested:
        return SKIPPED, "模型没有按要求调用终端工具，本轮判不了审批链路"
    denied = any(e["event"].get("decision") == "deny" for e in resolved)
    safe = not sentinel.exists()
    state = PASS if denied and safe else FAIL
    return state, (
        f"{len(requested)} 条 permission.requested / {len(resolved)} 条 permission.resolved；"
        f"拒绝={'生效' if denied else '未确认'}；"
        f"sentinel={'未生成（命令没跑）' if safe else '**被创建了**（命令跑了）'}"
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    native = args.driver in ("native-http", "hermes-http")
    config = backend_config(args)
    print(BANNER)
    print(describe(config, args))
    if args.dry_run:
        print("\n--dry-run：到此为止，什么都没执行。")
        return 0

    import httpx
    import uvicorn
    from fastapi import FastAPI

    import session_bootstrap
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.projects.models import AgentBinding, Backend, Project

    sheet = Sheet()
    gateway: LiveGateway | None = None
    sentinel = Path(tempfile.gettempdir()) / "session-smoke-approval-must-stay-absent.txt"
    if native and not args.base_url:
        print("\n[0] 起隔离沙盒与网关")
        gateway = start_isolated_gateway(args)
        config["backends"][0]["base_url"] = gateway.base_url
        config["backends"][0]["home"] = str(gateway.home)
        sentinel = gateway.workdir / "approval-must-stay-absent.txt"
        print(f"    网关就绪：{gateway.base_url}")

    temp_dir = None
    if args.db:
        db_path = Path(args.db).expanduser()
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="session-smoke-")
        db_path = Path(temp_dir.name) / "domain.sqlite3"

    backend_id = config["backends"][0]["id"]
    backend_key = backend_id.split(":", 1)[1]

    # --- 种一个最小领域：一个 Project + 一条 Binding -------------------- #
    import asyncio

    unit_of_work = SqliteUnitOfWork(db_path)
    repositories = unit_of_work.repositories

    async def seed() -> str:
        driver_kind = {"mock": "mock", "acp": "acp"}.get(args.driver, "native")
        await repositories.backends.save(
            Backend.create(key=backend_key, driver_kind=driver_kind)
        )
        project = await repositories.projects.save(
            Project.create(slug=args.project_slug, display_name=args.project_slug)
        )
        binding = await repositories.bindings.save(
            AgentBinding.create(
                project=project,
                backend=backend_key,
                display_name=f"{backend_key} 冒烟绑定",
                is_default=True,
            )
        )
        return binding.id

    binding_id = asyncio.run(seed())

    runtime = session_bootstrap.open_session_runtime(
        # `db_path` 在这里只用来定位 token 文件（库已经开好了）——给它，token 才会
        # 落在同一个临时目录里，而不是生产机的 `state/`。
        config=config,
        repositories=repositories,
        db_path=db_path,
    )
    for warning in runtime.warnings:
        print(f"!! 配置警告：{warning}")
    app = FastAPI()
    app.include_router(runtime.router)
    # 顺带把只读领域路由也挂上：`GET /api/backends/{id}` 就是「装了没、通没通、
    # 版本多少」的验收面，让用户在同一条命令里看到它，而不是自己再去点一次。
    from app.api.router import build_domain_router

    app.include_router(build_domain_router(repositories))

    port = pick_free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("!! 本地服务没起来", file=sys.stderr)
        return 2

    # D-17：token 由接入层在装配时写进 `runtime.token_path`（0600）。本脚本作为
    # 本机客户端直接读文件，而不是走 bootstrap 端点——少一次往返，也顺带证明了
    # 文件确实被生成出来了。值只用来发头，不打印。
    token = runtime.token_path.read_text(encoding="utf-8").strip()
    print(f"\n鉴权：Bearer token 已从 {runtime.token_path} 读到（值不打印）")

    exit_code = 0
    turns: list[Turn] = []
    try:
        with httpx.Client(
            base_url=base,
            timeout=30.0,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            # --- probe（native-http 的第 0 步：装没装、通没通、版本多少） --- #
            if native:
                probe_row = sheet.add("probe", "网关就绪 + 能力协商")
                probed = asyncio.run(runtime.maintenance.refresh_backends())
                stored = asyncio.run(repositories.backends.get(backend_id))
                probe_row.state = PASS if probed.get(backend_id) == "available" else FAIL
                probe_row.detail = (
                    f"probe_state={probed.get(backend_id)}, installed={stored.installed}, "
                    f"version={stored.version}"
                )
                print(f"\n[0b] probe → {probe_row.detail}")
                shown = client.get(f"/api/backends/{backend_id}")
                if shown.status_code == 200:
                    seen = shown.json()
                    print(
                        f"     GET /api/backends/{backend_id} → "
                        f"installed={seen['installed']} version={seen['version']} "
                        f"probeState={seen['probeState']}"
                    )
                if probe_row.state == FAIL:
                    print("!! 网关没就绪，后面的回合就不发了（免得把失败原因搅在一起）")
                    print(sheet.render())
                    return 1

            created = client.post(
                f"/api/projects/{args.project_slug}/conversations",
                json={"bindingId": binding_id, "title": "session smoke"},
            )
            print(f"\n[1] POST conversations → {created.status_code}")
            session_row = sheet.add("session", "建 Conversation")
            if created.status_code != 201:
                print(created.text)
                session_row.state = FAIL
                session_row.detail = created.text[:120]
                print(sheet.render())
                return 2
            conversation_id = created.json()["id"]
            session_row.state = PASS
            session_row.detail = conversation_id
            print(f"    conversationId = {conversation_id}")

            if native and not args.live:
                print("\n· 没加 --live：不发起会计费的模型回合，到此为止。")
                sheet.add("turn", "基础回合").detail = "需要 --live"
            else:
                print("\n[2] 回合")
                turns.append(
                    run_turn(client, conversation_id, args.prompt, args, label="第 1 轮")
                )
                if native and not args.skip_multiturn:
                    turns.append(
                        run_turn(
                            client, conversation_id, MULTITURN_PROMPT, args,
                            label="第 2 轮（多轮记忆）",
                        )
                    )
                if native and not args.skip_approval:
                    sentinel.unlink(missing_ok=True)
                    turns.append(
                        run_turn(
                            client,
                            conversation_id,
                            APPROVAL_PROMPT.format(command=f"touch {sentinel}"),
                            args,
                            label="第 3 轮（审批 · 实时拒绝）",
                            deny_permissions=True,
                        )
                    )

            print(f"\n[3] POST stop → {client.post(f'/api/conversations/{conversation_id}/stop').status_code}")
            snapshot = client.get(f"/api/conversations/{conversation_id}").json()
            print("[4] 会话快照：")
            print(json.dumps(snapshot["timeline"], ensure_ascii=False, indent=2))

            exit_code = summarize(sheet, turns, args, native=native, sentinel=sentinel)
            print(sheet.render())
            if args.json:
                payload = sheet.to_json()
                payload["conversationId"] = conversation_id
                payload["turns"] = [
                    {
                        "label": t.label,
                        "terminal": t.terminal,
                        "elapsedSeconds": round(t.elapsed, 3),
                        "eventCounts": t.counts,
                        "pendingInteraction": t.pending_interaction,
                    }
                    for t in turns
                ]
                print("\n== JSON ==")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        asyncio.run(runtime.aclose())
        unit_of_work.close()
        if temp_dir is not None:
            temp_dir.cleanup()
        if gateway is not None:
            print("\n[5] 收摊：停网关、清沙盒")
            gateway.stop()

    print("\n完成。" if exit_code == 0 else "\n完成（有未通过项）。")
    return exit_code


def summarize(
    sheet: Sheet,
    turns: list[Turn],
    args: argparse.Namespace,
    *,
    native: bool,
    sentinel: Path,
) -> int:
    """把每一轮的事实折成验收表。**只报观察到的，观察不到的写「未测」。**"""
    by_label = {turn.label: turn for turn in turns}
    first = turns[0] if turns else None

    if first is not None:
        row = sheet.add("turn", "基础回合到终态")
        row.state = PASS if first.terminal == "run.completed" else FAIL
        row.detail = (
            f"终态={first.terminal or '（没等到）'}，"
            f"{sum(first.counts.values())} 条公共事件，{first.elapsed:.1f}s"
        )
        if first.pending_interaction:
            row.state = SKIPPED
            row.detail += f"，停在待答交互 {first.pending_interaction}"

    multiturn = by_label.get("第 2 轮（多轮记忆）")
    row = sheet.add("multiturn", "多轮记忆")
    if multiturn is None:
        row.detail = "本次没跑（--skip-multiturn 或非 native-http / 非 --live）"
    else:
        remembered = MARKER in multiturn.text.upper()
        row.state = PASS if remembered else FAIL
        row.detail = (
            "第 2 轮引用了第 1 轮的标记"
            if remembered
            else f"第 2 轮没有引用第 1 轮：{multiturn.text[:80]!r}"
        )

    approval = by_label.get("第 3 轮（审批 · 实时拒绝）")
    row = sheet.add("approval", "审批闭环（实时 deny）")
    if approval is None:
        row.detail = "本次没跑（--skip-approval 或非 native-http / 非 --live）"
    else:
        row.state, row.detail = check_approval_outcome(approval, sentinel)

    row = sheet.add("reasoning", "reasoning 事件")
    reasoning = {
        kind: sum(t.counts.get(kind, 0) for t in turns)
        for kind in ("reasoning.status", "reasoning.delta")
    }
    if not turns:
        row.detail = "本次没跑回合"
    elif any(reasoning.values()):
        row.state = PASS
        row.detail = "，".join(f"{k}×{v}" for k, v in reasoning.items() if v)
    else:
        row.detail = "本次模型没吐 reasoning（规格 §8-③ 仍未判定）"

    row = sheet.add("events", "事件计数")
    tally: dict[str, int] = {}
    for turn in turns:
        for kind, count in turn.counts.items():
            tally[kind] = tally.get(kind, 0) + count
    if tally:
        row.state = PASS
        row.detail = "，".join(f"{k}×{v}" for k, v in sorted(tally.items()))
    else:
        row.detail = "本次没跑回合"

    row = sheet.add("elapsed", "耗时")
    row.state = PASS if turns else SKIPPED
    row.detail = "，".join(f"{t.label}={t.elapsed:.1f}s" for t in turns) or "—"

    del native, args
    return 1 if sheet.failed() else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
