#!/usr/bin/env python3
"""Hermes HTTP Driver 的真机冒烟入口（Mac，用户侧验证）。

它做的事只有一件：**在一台真的装了 Hermes 的机器上，用 Driver 自己的代码跑一个
最小回合，并把翻译后的公共事件打出来。** 契约测试跑的是假 API server；这个脚本
跑的是 ``hermes gateway run``。两者验证的东西不同，缺一不可。

安全护栏（复用 ``scripts/probe/probelib/sandbox.py``，不另起一套）
------------------------------------------------------------------
* 默认 ``--home-mode isolated``：``HERMES_HOME`` 指向一个临时目录，用户真实
  ``~/.hermes`` 一个字节都不读、不写；
* ``--seed-config`` 是**唯一**允许读用户真实目录的入口，且带凭据扫描——
  看起来像密钥的配置会被拒绝拷进沙盒；
* ``.env`` 永远不拷贝；沙盒的 ``API_SERVER_KEY`` 由脚本现场生成，只写进沙盒的
  ``.env``，并登记进脱敏器（输出里出现次数为 0）；
* 每一步先打印将执行的命令；``--dry-run`` 时只打印不执行。

隔离沙盒**没有 provider 凭据**，所以真正的模型回合需要 ``--seed-config`` 指一份
能连上模型的 ``config.yaml``（不含密钥）。不给的话脚本只跑到「就绪 + 能力协商 +
建会话 + 历史」，并如实报告哪些项没跑成。

规格 §8 的未验证项 → ``--check`` 子项
-------------------------------------
==================== ============================================ ==========
``multiturn``        §8-① 多轮记忆：第二轮问「我刚才问了什么」        需 --live
``per-run-model``    §8-⑦ 能否按 run 指定模型 / 推理强度             需 --live
``approval``         §8-② 审批闭环 + effect_disposition 入库         需 --live
``reasoning``        §8-③ reasoning.available 的载荷语义             需 --live
``reconnect``        §8-④ SSE 重连语义（重放 / 只发新 / 404）        需 --live
``keepalive``        §8-⑤ SSE 心跳与空闲超时                         需 --live
``shapes``           §8-⑦ 若干响应体形状（多打几个 GET）             不需要
``profile-flag``     §8-⑧ ``hermes -p`` 与进程 env 覆盖 ``.env``      不需要
==================== ============================================ ==========

用法::

    python3 scripts/hermes_http_smoke.py --dry-run
    python3 scripts/hermes_http_smoke.py --seed-config ~/hermes-probe-config.yaml --live
    python3 scripts/hermes_http_smoke.py --check shapes,profile-flag
    python3 scripts/hermes_http_smoke.py --live --check multiturn,approval,reasoning
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "kernel"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "probe"))

from probelib import sandbox as sandbox_mod  # noqa: E402
from probelib.httpc import wait_for_http  # noqa: E402
from probelib.util import Options as ProbeOptions  # noqa: E402
from probelib.util import Ctx as ProbeCtx  # noqa: E402
from probelib.util import register_secret  # noqa: E402

from app.conversations.models import Conversation  # noqa: E402
from app.projects.models import AgentBinding, Project  # noqa: E402
from drivers.base import InteractionResponse, MessageInput  # noqa: E402
from drivers.hermes import redaction  # noqa: E402
from drivers.hermes.driver import HermesDriver  # noqa: E402
from drivers.hermes.history import read_history_from_db, read_schema_version  # noqa: E402
from drivers.hermes.oob_watcher import HermesOutOfBandWatcher  # noqa: E402
from drivers.hermes.supervisor import GatewayConfig, pick_free_port  # noqa: E402

BANNER = r"""
+--------------------------------------------------------------+
|  Hermes HTTP Driver 冒烟 / hermes_http_smoke                  |
|  隔离 HERMES_HOME · 一个最小回合 · 打印翻译后的公共事件       |
+--------------------------------------------------------------+
"""

PROMPT = "Reply with exactly the word HERMES-SMOKE-OK and nothing else."
MULTITURN_PROMPT = "What exactly did I ask you in my previous message? Quote it."
APPROVAL_PROMPT = (
    "Use the terminal/shell tool (not write_file, patch, or Python) to execute exactly "
    "this command: {command}. Do not substitute another tool or command."
)
REASONING_PROMPT = (
    "Think through this in three steps, then output ONLY the final number: "
    "what is 17 * 23 minus 91?"
)

#: 规格 §8 的编号，逐条对上。
CHECK_SPEC_ITEMS = {
    "multiturn": "§8-① 反向互通回灌 / 多轮记忆",
    "approval": "§8-② 审批闭环 + effect_disposition 入库",
    "reasoning": "§8-③ reasoning.available 的载荷语义",
    "reconnect": "§8-④ SSE 重连语义",
    "keepalive": "§8-⑤ SSE keepalive / 空闲超时",
    "shapes": "§8-⑦ 若干响应体形状 + per-run 模型选择 + active 列语义",
    "per-run-model": "§8-⑦ /v1/runs 能否按 run 指定模型 / 推理强度",
    "profile-flag": "§8-⑧ hermes -p 与进程 env 覆盖 .env",
}
LIVE_ONLY_CHECKS = {"multiturn", "approval", "reasoning", "reconnect", "keepalive", "per-run-model"}
CHECK_ORDER = (
    # Multiturn must run immediately after the baseline turn; otherwise its
    # "previous message" assertion observes whichever check happened last.
    "multiturn",
    "approval",
    "reasoning",
    "reconnect",
    "keepalive",
    "shapes",
    "per-run-model",
    "profile-flag",
)


# --------------------------------------------------------------------------- #
# 结果记账（三态，与探针报告同口径）
# --------------------------------------------------------------------------- #

MEASURED = "实测"
NOT_TESTED = "未测"
UNSUPPORTED = "不支持"


@dataclass
class Item:
    key: str
    title: str
    state: str = NOT_TESTED
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = {MEASURED: "[OK]", NOT_TESTED: "[--]", UNSUPPORTED: "[NO]"}[self.state]
        return f"  {mark} {self.key:<16} {self.title} — {self.state}" + (
            f"：{self.detail}" if self.detail else ""
        )


class Report:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def add(self, key: str, title: str) -> Item:
        item = Item(key=key, title=title)
        self.items.append(item)
        return item

    def exit_code(self) -> int:
        return 1 if any(i.state == UNSUPPORTED for i in self.items) else 0

    def render(self) -> str:
        lines = ["", "== 结果 =="]
        lines += [item.line() for item in self.items]
        unverified = [i for i in self.items if i.key in CHECK_SPEC_ITEMS]
        if unverified:
            lines += ["", "== 规格 §8 未验证项 =="]
            for item in unverified:
                lines.append(f"  {CHECK_SPEC_ITEMS[item.key]}：{item.state}"
                             + (f" — {item.detail}" if item.detail else ""))
        return "\n".join(lines)


def log(message: str = "") -> None:
    print(str(redaction.redact(message)), flush=True)


def step(message: str) -> None:
    print(f"\n== {message}", flush=True)


# --------------------------------------------------------------------------- #
# gateway 进程
# --------------------------------------------------------------------------- #


class Gateway:
    """真正的 ``hermes gateway run`` 子进程（只在冒烟脚本里出现）。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def poll(self) -> int | None:
        return self.proc.poll()

    def terminate(self) -> None:
        self.proc.terminate()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)


def spawn_gateway(hermes_bin: str, sandbox: Any, port: int, dry_run: bool) -> Gateway | None:
    argv = [hermes_bin, "gateway", "run"]
    log(f"  $ {' '.join(argv)}   # HERMES_HOME={sandbox.home}，端口 {port}")
    if dry_run:
        return None
    env = {
        **os.environ,
        **sandbox.env(
            {
                "API_SERVER_ENABLED": "true",
                "API_SERVER_KEY": sandbox.api_key,
                "API_SERVER_PORT": str(port),
                "API_SERVER_HOST": "127.0.0.1",
            }
        ),
    }
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(sandbox.workdir),
        env=env,
    )
    return Gateway(proc)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


async def run_turn(
    driver: HermesDriver,
    conversation: Conversation,
    prompt: str,
    *,
    label: str,
    timeout: float = 180.0,
    auto_deny_permissions: bool = False,
) -> tuple[list[Any], str]:
    """跑一个回合，**逐条打印翻译后的公共事件**，返回信封列表与拼出来的正文。"""
    runtime = await driver.start_runtime(conversation, "card")
    envelopes: list[Any] = []
    text_parts: list[str] = []
    log(f"\n  --- {label}：{prompt}")
    try:
        await driver.send_message(runtime, MessageInput(text=prompt))

        async def _collect() -> None:
            async for envelope in driver.events(runtime):
                envelopes.append(envelope)
                event = envelope.event
                if event.type == "message.delta":
                    text_parts.append(event.text)
                log(f"    {envelope.sequence:>3}  {_describe(envelope)}")
                if event.type == "permission.requested" and auto_deny_permissions:
                    # A real approval blocks the run.  Resolve it while the SSE
                    # consumer is alive; waiting until run_turn returns can never
                    # close the request/response loop.
                    await driver.resolve_interaction(
                        runtime,
                        event.request.request_id,
                        InteractionResponse(kind="permission", option_id="deny"),
                    )
                if event.type in ("run.completed", "run.failed", "run.interrupted"):
                    return

        await asyncio.wait_for(_collect(), timeout)
    finally:
        await driver.stop_runtime(runtime)
    completed = [e for e in envelopes if e.event.type == "message.completed"]
    text = completed[-1].event.text if completed else "".join(text_parts)
    return envelopes, text or ""


def _describe(envelope: Any) -> str:
    event = envelope.event
    kind = event.type
    if kind == "message.delta":
        return f"message.delta   msg={event.message_id} {event.text!r}"
    if kind == "message.completed":
        return f"message.completed msg={event.message_id} {(event.text or '')[:80]!r}"
    if kind == "tool.started":
        return f"tool.started    call={event.call_id} name={event.name} input={event.input}"
    if kind == "tool.completed":
        return f"tool.completed  call={event.call_id} isError={event.is_error} output={event.output}"
    if kind == "reasoning.status":
        return f"reasoning.status status={event.status} summary={(event.summary or '')[:60]!r}"
    if kind == "reasoning.delta":
        return f"reasoning.delta msg={event.message_id} {event.text[:60]!r}"
    if kind == "usage.updated":
        usage = event.usage
        return (
            "usage.updated   "
            f"in={usage.input_tokens} out={usage.output_tokens} total={usage.total_tokens} "
            f"contextUsed={usage.context_used}"
        )
    if kind == "permission.requested":
        return f"permission.requested req={event.request.request_id} {event.request.title!r}"
    if kind == "permission.resolved":
        return f"permission.resolved req={event.request_id} decision={event.decision}"
    if kind == "extension.event":
        return f"extension.event ns={event.namespace} name={event.name}"
    if kind == "diagnostic.notice":
        return f"diagnostic.notice [{event.level}] {event.message}"
    return f"{kind}  {event.model_dump(exclude={'type'})}"


async def main_async(args: argparse.Namespace) -> int:
    report = Report()
    checks = {c.strip() for c in (args.check or "").split(",") if c.strip()}
    unknown = checks - set(CHECK_SPEC_ITEMS)
    if unknown:
        log(f"未知的 --check 子项：{sorted(unknown)}；可选：{sorted(CHECK_SPEC_ITEMS)}")
        return 2

    hermes_bin = args.hermes_bin or shutil.which("hermes") or "hermes"
    if shutil.which(hermes_bin) is None and not args.dry_run:
        log(f"找不到 hermes 可执行文件（{hermes_bin}）。这个脚本要在装了 Hermes 的机器上跑。")
        return 2

    # --- 沙盒（复用 probe 的护栏） ------------------------------------- #
    step("沙盒准备（隔离 HERMES_HOME；用户真实 profile 不被读写）")
    probe_opts = ProbeOptions(
        dry_run=args.dry_run,
        live=args.live,
        home_mode=args.home_mode,
        profile_name=args.profile_name,
        keep=args.keep,
        seed_config=args.seed_config,
        hermes_bin=hermes_bin,
    )
    ctx = ProbeCtx(probe_opts)
    sandbox = sandbox_mod.Sandbox(ctx).build()
    ctx.sandbox = sandbox
    register_secret(sandbox.api_key)
    redaction.register_literal(sandbox.api_key)
    log(f"  HERMES_HOME = {sandbox.home}")
    log(f"  工作目录     = {sandbox.workdir}")
    for note in sandbox.notes:
        log(f"  note: {note}")

    port = pick_free_port() if not args.dry_run else 18642
    sandbox.write_api_server_env(port)

    step("启动 gateway")
    gateway = spawn_gateway(hermes_bin, sandbox, port, args.dry_run)
    if args.dry_run:
        log("\n  --dry-run：到此为止，什么都没执行。")
        log(report.render())
        return 0

    # ``adopted`` mode deliberately performs only one readiness probe.  The
    # gateway we just spawned needs a short startup window, so wait here before
    # handing it to the Driver; otherwise a normal cold start is misreported as
    # an immediate ConnectionRefusedError.
    health = wait_for_http(f"http://127.0.0.1:{port}/health", timeout=60.0)
    if not health.status:
        detail = health.brief()
        if gateway is not None and gateway.poll() is not None:
            stdout, stderr = gateway.proc.communicate()
            process_output = (stderr or stdout or b"").decode("utf-8", "replace")[-2000:]
            if process_output:
                detail += f"；gateway 输出：{process_output}"
        raise RuntimeError(f"gateway 60 秒内未就绪：{detail}")

    driver = HermesDriver(
        hermes_root=sandbox.home.parent,
        default_gateway=GatewayConfig(
            hermes_home=sandbox.home,
            port=port,
            profile="default",
            key_ref=f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY",
            mode="adopted",  # 进程是我们刚拉起的，但生命周期由本脚本管
        ),
        hermes_bin=hermes_bin,
    )

    try:
        return await _run_all(args, checks, report, driver, sandbox, port, hermes_bin)
    finally:
        if gateway is not None:
            gateway.terminate()
            try:
                gateway.wait(timeout=8)
            except Exception:  # noqa: BLE001
                pass
        step("清理")
        for path in sandbox.try_cleanup():
            log(f"  removed {path}")
        for hint in sandbox.cleanup_instructions():
            log(f"  手动清理：{hint}")


async def _run_all(
    args: argparse.Namespace,
    checks: set[str],
    report: Report,
    driver: HermesDriver,
    sandbox: Any,
    port: int,
    hermes_bin: str,
) -> int:
    # --- probe --------------------------------------------------------- #
    step("probe：就绪 + 能力协商")
    item = report.add("probe", "gateway 就绪与能力协商")
    result = await driver.probe()
    log(f"  state={result.state} version={result.version} installed={result.installed}")
    log(f"  message={result.message}")
    if result.state == "unavailable":
        item.state = UNSUPPORTED
        item.detail = result.message or "gateway 未就绪"
        log(report.render())
        return report.exit_code()
    item.state = MEASURED
    item.detail = f"state={result.state}, version={result.version}"

    capabilities = await driver.get_capabilities()
    log("  能力（公共契约）：" + json.dumps(capabilities.model_dump(mode="json"),
                                            ensure_ascii=False)[:600])

    # --- 会话与 Binding ------------------------------------------------ #
    project = Project.create(slug="smoke", display_name="smoke",
                             workspace_root=str(sandbox.workdir))
    binding = AgentBinding.create(
        project=project,
        backend=driver.backend_id,
        native_scope_ref="default",
        is_default=True,
        runtime_config={
            "api_server": {
                "host": "127.0.0.1",
                "port": port,
                "profile": "default",
                "hermes_home": str(sandbox.home),
                "key_ref": f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY",
                "mode": "adopted",
            }
        },
    )
    driver.register_binding(binding)

    step("建会话")
    session_item = report.add("session", "POST /api/sessions 返回确定的会话 id")
    from drivers.base import CreateSessionOptions

    session = await driver.create_native_session(
        binding, CreateSessionOptions(title="hermes-http-smoke")
    )
    log(f"  session id = {session.native_session_id}（形状判别只用于日志）")
    session_item.state = MEASURED
    session_item.detail = session.native_session_id
    conversation = Conversation.create(
        project_id=project.id, agent_binding_id=binding.id, title="smoke"
    ).evolve(native_session_id=session.native_session_id)

    # --- 最小回合 ------------------------------------------------------ #
    turn_item = report.add("turn", "一个最小回合 + 翻译后的公共事件")
    if not args.live:
        turn_item.detail = "未加 --live：不发起会计费的模型回合"
        log("\n  跳过模型回合（加 --live 才会真的调用模型）。")
    else:
        step("最小回合")
        try:
            envelopes, text = await run_turn(driver, conversation, PROMPT, label="第 1 轮")
            turn_item.state = MEASURED
            turn_item.detail = f"{len(envelopes)} 条公共事件，正文 {text[:40]!r}"
            report.add("events", "事件类型分布").state = MEASURED
            report.items[-1].detail = ", ".join(
                sorted({e.event.type for e in envelopes})
            )
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本不该因为一步失败就崩
            turn_item.state = NOT_TESTED
            turn_item.detail = f"{type(exc).__name__}: {exc}"

    # --- 历史 ----------------------------------------------------------- #
    step("历史（HTTP 优先，state.db 回退）")
    history_item = report.add("history", "GET /api/sessions/{id}/messages")
    history = await driver.load_native_history(binding, session.native_session_id)
    history_item.state = MEASURED
    history_item.detail = (
        f"{len(history.entries)} 条，complete={history.complete}, missing={history.missing}"
    )
    log(f"  {history_item.detail}")

    db_item = report.add("state_db", "state.db 只读回退与 schema 分档")
    schema = read_schema_version(sandbox.state_db)
    if schema is None:
        db_item.state = NOT_TESTED
        db_item.detail = "读不到 schema 版本"
    else:
        rows = read_history_from_db(sandbox.state_db, session.native_session_id)
        db_item.state = MEASURED
        db_item.detail = f"schema={schema}，直读到 {len(rows.entries)} 条"
    log(f"  {db_item.detail}")

    # --- 带外检测 ------------------------------------------------------- #
    step("带外检测器（AD-20）")
    oob_item = report.add("oob", "state.db-wal + SQL 回读，1s 内触发")
    watcher = HermesOutOfBandWatcher(hermes_home=sandbox.home)
    watcher.track(session.native_session_id)
    watcher.prime()
    argv = [hermes_bin, "sessions", "rename", session.native_session_id, "smoke-renamed"]
    log(f"  $ {' '.join(argv)}   # 模拟带外写入")
    started = time.monotonic()
    try:
        subprocess.run(  # noqa: S603
            argv,
            cwd=str(sandbox.workdir),
            env={**os.environ, **sandbox.env()},
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        oob_item.state = NOT_TESTED
        oob_item.detail = f"无法执行带外写入命令：{type(exc).__name__}: {exc}"
        log(f"  {oob_item.detail}")
    else:
        fired = False
        while time.monotonic() - started < 5.0:
            if watcher.poll():
                fired = True
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        oob_item.state = MEASURED if fired else NOT_TESTED
        oob_item.detail = f"{elapsed:.2f}s 内{'触发' if fired else '未触发'}（含写入命令本身耗时）"
        log(f"  {oob_item.detail}")

    # --- Open in CLI ---------------------------------------------------- #
    step("Open in CLI 命令构造")
    cli_item = report.add("cli", "build_external_cli_launch")
    spec = await driver.build_external_cli_launch(conversation)
    cli_item.state = MEASURED
    cli_item.detail = " ".join(spec.command)
    log(f"  {cli_item.detail}")
    log(f"  env_passthrough（只有变量名，AD-10）：{spec.env_passthrough}")

    # --- §8 的 --check 子项 --------------------------------------------- #
    for key in CHECK_ORDER:
        if key not in checks:
            continue
        await _run_check(key, args, report, driver, conversation, binding, sandbox, hermes_bin, port)

    log(report.render())
    return report.exit_code()


# --------------------------------------------------------------------------- #
# §8 未验证项
# --------------------------------------------------------------------------- #


async def _run_check(
    key: str,
    args: argparse.Namespace,
    report: Report,
    driver: HermesDriver,
    conversation: Conversation,
    binding: AgentBinding,
    sandbox: Any,
    hermes_bin: str,
    port: int,
) -> None:
    step(f"--check {key}（{CHECK_SPEC_ITEMS[key]}）")
    item = report.add(key, CHECK_SPEC_ITEMS[key])
    if key in LIVE_ONLY_CHECKS and not args.live:
        item.detail = "需要 --live（会真的调用模型、会计费）"
        log(f"  跳过：{item.detail}")
        return
    handler: Callable[..., Any] = {
        "multiturn": _check_multiturn,
        "approval": _check_approval,
        "reasoning": _check_reasoning,
        "reconnect": _check_reconnect,
        "keepalive": _check_keepalive,
        "shapes": _check_shapes,
        "per-run-model": _check_per_run_model,
        "profile-flag": _check_profile_flag,
    }[key]
    try:
        await handler(item, driver, conversation, binding, sandbox, hermes_bin, port)
    except Exception as exc:  # noqa: BLE001
        item.state = NOT_TESTED
        item.detail = f"{type(exc).__name__}: {exc}"
    log(f"  {item.state}：{item.detail}")


async def _check_multiturn(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-①：第二轮问「我刚才问了什么」，看回答是否引用了第一轮。"""
    _, second = await run_turn(driver, conversation, MULTITURN_PROMPT, label="第 2 轮（记忆）")
    remembered = "HERMES-SMOKE-OK" in second.upper()
    item.state = MEASURED
    item.detail = (
        "第二轮引用了第一轮内容 → 多轮记忆成立，AD-32 的主路径保持 /v1/runs"
        if remembered
        else "第二轮**没有**引用第一轮 → 疑似 issue #62732 残留，需按 §8-① 改回备用路径"
    )
    item.evidence.append(second[:200])


async def _check_approval(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-②：用必然命中危险规则的空操作，实时拒绝并验证未执行。"""
    sentinel = sandbox.workdir / "approval-must-stay-absent.txt"
    command = f"rm -f {sentinel}"
    prompt = APPROVAL_PROMPT.format(command=command)
    envelopes, _ = await run_turn(
        driver,
        conversation,
        prompt,
        label="审批回合（自动拒绝）",
        auto_deny_permissions=True,
    )
    requests = [e for e in envelopes if e.event.type == "permission.requested"]
    resolutions = [e for e in envelopes if e.event.type == "permission.resolved"]
    extensions = [
        e for e in envelopes
        if e.event.type == "extension.event" and "approval" in e.event.name
    ]
    if not requests and not extensions:
        item.state = NOT_TESTED
        item.detail = "模型未按要求调用 terminal，本轮无法判定审批链路"
        return
    denied = any(e.event.decision == "deny" for e in resolutions)
    safe = not sentinel.exists()
    item.state = MEASURED if requests and denied and safe else NOT_TESTED
    item.detail = (
        f"抓到 {len(requests)} 条 permission.requested / "
        f"{len(resolutions)} 条 permission.resolved；"
        f"自动拒绝={'成功' if denied else '未确认'}；"
        f"目标文件={'未生成' if safe else '意外生成'}"
    )
    for envelope in requests + resolutions + extensions:
        item.evidence.append(json.dumps(envelope.event.model_dump(mode="json"), ensure_ascii=False))
    # effect_disposition 是否入库（AD-34 后半）。
    from drivers.hermes.history import open_readonly

    with open_readonly(sandbox.state_db) as connection:
        rows = connection.execute(
            "SELECT id, effect_disposition FROM messages"
            " WHERE session_id = ? AND effect_disposition IS NOT NULL",
            (conversation.native_session_id,),
        ).fetchall()
    item.detail += f"；effect_disposition 入库行数 = {len(rows)}"


async def _check_reasoning(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-③：用一个思考与答案明显不同的提示，数 reasoning.available 出现几次。"""
    envelopes, text = await run_turn(driver, conversation, REASONING_PROMPT, label="推理回合")
    deltas = [e for e in envelopes if e.event.type == "reasoning.delta"]
    statuses = [e for e in envelopes if e.event.type == "reasoning.status"]
    if deltas:
        item.state = MEASURED
        item.detail = f"{len(deltas)} 条 reasoning.delta → 增量流成立，card.reasoning 可升 true"
    elif statuses:
        item.state = MEASURED
        summary = statuses[-1].event.summary or ""
        same = summary.strip() == text.strip()
        item.detail = (
            "唯一一条 reasoning.status，且内容等于答案 → 判定为「答案重复」，card.reasoning 应降 false"
            if same
            else "唯一一条 reasoning.status，内容与答案不同 → 完整思考摘要，card.reasoning 可升 true"
        )
    else:
        item.state = NOT_TESTED
        item.detail = "本轮没有产出任何 reasoning 事件（可能是模型或翻译器的降级分支）"


async def _check_reconnect(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-④：起一个 run，断开 SSE，重连，看第一个事件是不是从头重放。"""
    from drivers.hermes import reconnect as reconnect_mod
    from drivers.hermes.http_client import HermesHttpClient
    from drivers.hermes.credentials import resolve_credential_ref

    key = resolve_credential_ref(f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY")
    client = HermesHttpClient(base_url=f"http://127.0.0.1:{port}", api_key=key)
    created = await client.request_ok(
        "POST",
        "/v1/runs",
        body={"input": PROMPT, "session_id": conversation.native_session_id},
        headers={"X-Hermes-Session-Id": conversation.native_session_id},
    )
    run_id = created.json()["run_id"]

    first_frames: list[str] = []
    async for event in client.stream_sse(f"/v1/runs/{run_id}/events"):
        first_frames.append(event.data)
        if len(first_frames) >= 2:
            break  # 主动断开
    await asyncio.sleep(3.0)
    second_frames: list[str] = []
    try:
        async for event in client.stream_sse(f"/v1/runs/{run_id}/events"):
            second_frames.append(event.data)
            if len(second_frames) >= 2:
                break
    except Exception as exc:  # noqa: BLE001
        item.state = MEASURED
        item.detail = f"重连被拒（{exc}）→ §3.6 分支 (c)：SSE 是一次性的，POLLING 提为主路径"
        return
    if not first_frames or not second_frames:
        item.state = NOT_TESTED
        item.detail = "没有抓到足够的帧来判别"
        return
    behaviour = reconnect_mod.classify_replay(first_frames[0], second_frames[0])
    item.state = MEASURED
    item.detail = {
        reconnect_mod.ReplayBehaviour.FULL_REPLAY: "从头重放 → §3.6 分支 (a)，eventId 幂等去重足够",
        reconnect_mod.ReplayBehaviour.NEW_ONLY: "只发新事件 → §3.6 分支 (b)，每次重连后必须 RECONCILING",
    }.get(behaviour, f"判别结果：{behaviour}")


async def _check_keepalive(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-⑤：起一个长回合，观察空闲期有没有注释行心跳。"""
    from drivers.hermes.credentials import resolve_credential_ref
    from drivers.hermes.http_client import HermesHttpClient, SseParser

    key = resolve_credential_ref(f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY")
    client = HermesHttpClient(base_url=f"http://127.0.0.1:{port}", api_key=key)
    created = await client.request_ok(
        "POST",
        "/v1/runs",
        body={
            "input": "Sleep for 60 seconds using your shell tool, then say DONE.",
            "session_id": conversation.native_session_id,
        },
        headers={"X-Hermes-Session-Id": conversation.native_session_id},
    )
    run_id = created.json()["run_id"]
    parser = SseParser()
    gaps: list[float] = []
    last = time.monotonic()
    deadline = last + 90
    try:
        async for _event in client.stream_sse(f"/v1/runs/{run_id}/events"):
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            if now > deadline:
                break
    except Exception:  # noqa: BLE001
        pass
    item.state = MEASURED
    item.detail = (
        f"最大事件间隔 {max(gaps) if gaps else 0:.1f}s；"
        f"注释行（心跳）{len(parser.comments)} 条 → "
        + ("有心跳，可改用「2 个心跳周期无字节」判据" if parser.comments else "无心跳，保留 120s 判据")
    )


async def _check_shapes(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-⑦：多打几个 GET，把响应体完整落盘（不需要 --live）。"""
    from drivers.hermes.credentials import resolve_credential_ref
    from drivers.hermes.http_client import HermesHttpClient

    key = resolve_credential_ref(f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY")
    client = HermesHttpClient(base_url=f"http://127.0.0.1:{port}", api_key=key)
    out_dir = Path(sandbox.workdir) / "shapes"
    out_dir.mkdir(parents=True, exist_ok=True)
    collected: list[str] = []
    for name, path in (
        ("capabilities", "/v1/capabilities"),
        ("health_detailed", "/health/detailed"),
        ("skills", "/v1/skills"),
        ("toolsets", "/v1/toolsets"),
        ("model_options", "/api/model/options"),
        ("model_options_refresh", "/api/model/options?refresh=1"),
        ("messages_earliest", f"/api/sessions/{conversation.native_session_id}/messages?order=earliest"),
    ):
        response = await client.request("GET", path)
        target = out_dir / f"{name}.json"
        target.write_text(str(redaction.redact(response.body)), encoding="utf-8")
        collected.append(f"{name}={response.status}")
    item.state = MEASURED
    item.detail = "，".join(collected) + f"；完整响应体已落盘到 {out_dir}"
    log(f"  提示：把 {out_dir} 里的文件贴回规格 §8-⑦ 即可定案 endpoints 尾部与两个枚举端点。")


async def _check_per_run_model(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-⑦：``POST /v1/runs`` 加 ``model`` / ``reasoning`` 看是 400 / 被忽略 / 生效。"""
    from drivers.hermes.credentials import resolve_credential_ref
    from drivers.hermes.history import read_session_row
    from drivers.hermes.http_client import HermesHttpClient

    key = resolve_credential_ref(f"hermes-env:{sandbox.home}/.env#API_SERVER_KEY")
    client = HermesHttpClient(base_url=f"http://127.0.0.1:{port}", api_key=key)
    before = (read_session_row(sandbox.state_db, conversation.native_session_id) or {}).get("model")
    outcomes: list[str] = []
    for field_name, value in (("model", "gpt-4o-mini"), ("reasoning", "high"), ("provider", "openai")):
        response = await client.request(
            "POST",
            "/v1/runs",
            body={
                "input": "Say OK.",
                "session_id": conversation.native_session_id,
                field_name: value,
            },
            headers={"X-Hermes-Session-Id": conversation.native_session_id},
        )
        outcomes.append(f"{field_name}→HTTP {response.status}")
        await asyncio.sleep(2)
    after = (read_session_row(sandbox.state_db, conversation.native_session_id) or {}).get("model")
    item.state = MEASURED
    item.detail = (
        "，".join(outcomes)
        + f"；sessions.model {before!r} → {after!r}"
        + ("（生效）" if before != after else "（未变化 = 被忽略或不支持）")
    )


async def _check_profile_flag(item: Item, driver, conversation, binding, sandbox, hermes_bin, port) -> None:
    """§8-⑧：``hermes -p`` 与「进程 env 能否覆盖 .env」。"""
    results: list[str] = []
    for argv in (
        [hermes_bin, "-p", "default", "sessions", "list"],
        [hermes_bin, "--profile=default", "status"],
        [hermes_bin, "profile", "alias"],
    ):
        log(f"  $ {' '.join(argv)}")
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=str(sandbox.workdir),
            env={**os.environ, **sandbox.env()},
            capture_output=True,
            text=True,
            timeout=60,
        )
        results.append(f"{' '.join(argv[1:3])}→rc={proc.returncode}")
    # env 覆盖 .env：.env 里已写了 API_SERVER_PORT=<port>；再用 env 传另一个端口，
    # 看 gateway 最终监听哪个。这里只做「探测建议」，不真的再起一个进程——
    # 同一 HERMES_HOME 永不由本项目启动第二个 gateway（规格 §1.6，锁定）。
    item.state = MEASURED
    item.detail = (
        "；".join(results)
        + "；env 覆盖 .env 的验证需要单独起一个隔离 home 的 gateway（规格 §1.6 禁止在同一 home 上起第二个），"
          "请用 --home-mode isolated 单独跑一次并对比实际监听端口"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermes_http_smoke.py",
        description="在真实 Hermes 上跑一个最小回合，打印 Driver 翻译后的公共事件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--check 子项（对应规格 §8）：\n"
            + "\n".join(
                f"  {key:<14} {title}"
                + ("   [需 --live]" if key in LIVE_ONLY_CHECKS else "")
                for key, title in CHECK_SPEC_ITEMS.items()
            )
        ),
    )
    parser.add_argument("--live", action="store_true",
                        help="允许真的调用模型（会计费）。不加则只跑不花钱的部分。")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作。")
    parser.add_argument("--check", default="",
                        help="逗号分隔的 §8 子项，如 shapes,profile-flag。")
    parser.add_argument("--home-mode", default="isolated", choices=["isolated", "profile"],
                        help="isolated（默认）= 临时 HERMES_HOME；profile = 真实 ~/.hermes 下的 probe- 前缀 profile。")
    parser.add_argument("--profile-name", default="probe-smoke",
                        help="--home-mode profile 时的 profile 名（必须以 probe- 开头）。")
    parser.add_argument("--seed-config", default="",
                        help="拷进沙盒的 config.yaml（唯一允许读用户真实目录的入口；带凭据扫描）。")
    parser.add_argument("--hermes-bin", default="", help="hermes 可执行文件路径。")
    parser.add_argument("--keep", action="store_true", help="保留沙盒目录以便事后查看。")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    print(BANNER)
    if not args.live:
        print("提示：没有 --live，本次不会发起任何会计费的模型回合。\n")
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
