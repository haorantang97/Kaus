#!/usr/bin/env python3
"""对**真实** ACP agent 跑一次最小回合，打印翻译后的公共事件。

这是 Generic ACP Driver（``kernel/drivers/acp/``）的**真机验证入口**：契约测试
用的是假 agent，能证明形状对，不能证明真 agent 的报文长这样。这个脚本把同一个
Driver 接到真的 ``hermes acp`` 上跑一轮，把
:class:`~runtime.event_envelope.AgentEventEnvelope` 逐条打出来。

    # 隔离沙盒（默认）——不碰你的 ~/.hermes；需要 --seed-config 才有 provider
    python scripts/acp_smoke.py --seed-config ~/some-config-without-secrets.yaml

    # 只看它打算做什么，不真的跑
    python scripts/acp_smoke.py --dry-run

    # 换一个 ACP agent（本脚本对 agent 无偏好，只要它说 ACP）
    python scripts/acp_smoke.py --agent-command "my-agent acp"

安全护栏（照 ``scripts/probe/probelib/sandbox.py`` 的口径）
--------------------------------------------------------
1. 默认 ``--home-mode isolated``：``HERMES_HOME`` 指向 ``$TMPDIR`` 下一个全新目录，
   并断言它**不等于也不位于**真实 home 之内；
2. 除了 ``--seed-config`` 明确点名的那一个文件，脚本不读真实 home 下的任何文件；
3. ``.env``（凭据）永不复制；``--seed-config`` 的内容做一次机密扫描，命中即拒绝；
4. 想在真实 home 上跑必须同时给 ``--home-mode real`` 与 ``--allow-real-home``，
   脚本会先打印「这会在你的真实 state.db 里建一个会话」再执行；
5. 只清理自己在临时目录下创建的东西；``--keep`` 保留现场。

权限
----
默认 ``--permission deny``：真机冒烟不该在你的机器上执行未经确认的动作。
需要走通「允许」分支时显式 ``--permission allow``。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
if str(KERNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(KERNEL_ROOT))

from app.conversations.models import Conversation  # noqa: E402
from app.projects.models import AgentBinding, Project  # noqa: E402
from drivers.acp.client import AcpAgentSpec  # noqa: E402
from drivers.acp.driver import AcpDriver  # noqa: E402
from drivers.base import (  # noqa: E402
    CreateSessionOptions,
    InteractionResponse,
    MessageInput,
    UnsupportedCapabilityError,
)
from runtime.event_envelope import AgentEventEnvelope  # noqa: E402
from runtime.event_reducer import TimelineState, reduce_events  # noqa: E402

DEFAULT_AGENT_COMMAND = "hermes acp"
DEFAULT_PROMPT = (
    "Reply with exactly the word ACP-SMOKE-OK and nothing else. Do not use any tools."
)

_SECRETISH = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|credential|authorization)\s*[:=]\s*\S{6,}"
)


class SmokeError(RuntimeError):
    """护栏拒绝执行。"""


# --------------------------------------------------------------------------- #
# 沙盒
# --------------------------------------------------------------------------- #


def real_agent_home() -> Path:
    """用户真实的 agent home（``HERMES_HOME``，缺省 ``~/.hermes``）。"""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))).expanduser()


class Sandbox:
    """一次冒烟用的隔离目录，以及不许越过的那几条线。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.mode: str = args.home_mode
        self.dry_run: bool = args.dry_run
        self.keep: bool = args.keep
        self.real_home = real_agent_home()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.base = Path(tempfile.gettempdir()) / f"acp-smoke-{stamp}-{secrets.token_hex(3)}"
        self.workdir = self.base / "work"
        self.home: Path | None = None
        self.notes: list[str] = []

    def build(self, seed_config: str | None, allow_real_home: bool) -> "Sandbox":
        if self.mode == "isolated":
            self.home = self.base / "agent-home"
        elif self.mode == "real":
            if not allow_real_home:
                raise SmokeError(
                    "--home-mode real 会在你的真实 home 里创建会话；"
                    "确认后请同时加上 --allow-real-home"
                )
            self.home = None  # 继承当前环境，不覆盖 HERMES_HOME
            self.notes.append(
                f"在真实 home {self.real_home} 上运行：本次会话会真的落到你的原生存储里"
            )
        else:  # pragma: no cover - argparse 已限制取值
            raise SmokeError(f"未知的 --home-mode {self.mode!r}")
        self._assert_safe()
        if not self.dry_run:
            self.workdir.mkdir(parents=True, exist_ok=True)
            if self.home is not None:
                self.home.mkdir(parents=True, exist_ok=True)
        self._seed(seed_config)
        return self

    def _assert_safe(self) -> None:
        if self.home is None:
            return
        # macOS exposes the same temp tree as both /var/... and
        # /private/var/....  Resolve even before the leaf exists so the safety
        # comparison does not reject a legitimate mkdtemp path.
        real = self.real_home.resolve(strict=False)
        home = self.home.resolve(strict=False)
        if home == real:
            raise SmokeError(f"沙盒 home 落到了真实 home {real} —— 拒绝执行")
        try:
            home.relative_to(real)
        except ValueError:
            pass
        else:
            raise SmokeError(f"沙盒 home {home} 位于真实 home {real} 之内 —— 拒绝执行")
        if not str(home).startswith(str(Path(tempfile.gettempdir()).resolve())):
            raise SmokeError(f"沙盒 home {home} 不在临时目录下 —— 拒绝执行")

    def _seed(self, seed_config: str | None) -> None:
        if seed_config is None:
            if self.mode == "isolated":
                self.notes.append(
                    "没给 --seed-config：隔离 home 里没有任何 provider 配置，"
                    "session/new 很可能会因『没有可用模型』失败。这是环境问题，不是 Driver 问题。"
                )
            return
        path = Path(seed_config).expanduser()
        if not path.is_file():
            raise SmokeError(f"--seed-config {path} 不存在")
        text = path.read_text(encoding="utf-8", errors="replace")
        hit = _SECRETISH.search(text)
        if hit:
            raise SmokeError(
                f"--seed-config {path} 看起来含有凭据（命中 {hit.group(1)!r}），拒绝复制进沙盒"
            )
        if self.home is None:
            self.notes.append("--home-mode real 下忽略 --seed-config（不改你的真实配置）")
            return
        if self.dry_run:
            self.notes.append(f"[dry-run] 会把 {path} 复制成 {self.home / 'config.yaml'}")
            return
        target = self.home / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        self.notes.append(f"已把 {path} 复制成 {target}（机密扫描通过）")

    def env(self) -> dict[str, str]:
        env = {"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"}
        if self.home is not None:
            env["HERMES_HOME"] = str(self.home)
        return env

    def cleanup(self) -> list[str]:
        if self.keep or self.dry_run:
            return [f"现场保留在 {self.base}（手工清理：rm -rf {self.base}）"]
        temp_root = str(Path(tempfile.gettempdir()).resolve())
        resolved = self.base.resolve() if self.base.exists() else self.base
        if not str(resolved).startswith(temp_root):  # pragma: no cover - 防御
            return [f"拒绝删除临时目录之外的路径：{resolved}"]
        shutil.rmtree(resolved, ignore_errors=True)
        return [f"已清理 {resolved}"]

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "agentHome": str(self.home) if self.home else "<inherited>",
            "workdir": str(self.workdir),
            "realHomeUntouched": str(self.real_home) if self.home else None,
            "user": getpass.getuser(),
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


class Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def section(self, title: str) -> None:
        if not self.as_json:
            print(f"\n=== {title} ===", flush=True)

    def line(self, text: str) -> None:
        if not self.as_json:
            print(text, flush=True)

    def envelope(self, envelope: AgentEventEnvelope) -> None:
        payload = envelope.model_dump(by_alias=True, mode="json")
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        event = envelope.event
        detail = _summarise(event)
        print(
            f"[{envelope.sequence:>3}] {event.type:<22} "
            f"run={envelope.run_id or '-'} {detail}",
            flush=True,
        )


def _summarise(event: Any) -> str:
    """一行摘要。故意只用公共字段——公共事件本身就该够看。"""
    payload = event.model_dump(by_alias=True, mode="json")
    payload.pop("type", None)
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= 160 else text[:157] + "..."


# --------------------------------------------------------------------------- #
# 冒烟
# --------------------------------------------------------------------------- #


async def run_smoke(args: argparse.Namespace, sandbox: Sandbox, printer: Printer) -> int:
    command = tuple(shlex.split(args.agent_command))
    spec = AcpAgentSpec(
        command=command,
        cwd=str(sandbox.workdir),
        env=sandbox.env(),
        inherit_env=True,
    )
    driver = AcpDriver(
        spec,
        backend_key=args.backend_key,
        default_cwd=str(sandbox.workdir),
        call_timeout=args.call_timeout,
        prompt_timeout=args.prompt_timeout,
    )

    printer.section("1/5 probe")
    probe = await driver.probe()
    printer.line(
        f"state={probe.state} installed={probe.installed} "
        f"agentVersion={probe.version!r} driverVersion={probe.driver_version}"
    )
    if probe.message:
        printer.line(f"note: {probe.message}")
    if probe.state == "unavailable":
        printer.line("agent 起不来，后面的步骤没有意义。")
        return 2
    capabilities = probe.capabilities
    printer.line(
        "sessions: "
        + json.dumps(capabilities.sessions.model_dump(by_alias=True), ensure_ascii=False)
    )
    printer.line(
        "card:     "
        + json.dumps(capabilities.card.model_dump(by_alias=True), ensure_ascii=False)
    )
    verdict = driver.session_discovery_support()
    printer.line(f"sessions.list 判定: {verdict.level.value} — {verdict.reason}")

    project = Project.create(
        slug=args.project_slug,
        display_name=args.project_slug,
        workspace_root=str(sandbox.workdir),
    )
    binding = AgentBinding.create(
        project=project, backend=driver.backend_id, native_scope_ref="acp-smoke"
    )

    printer.section("2/5 session/new")
    session = await driver.create_native_session(
        binding, CreateSessionOptions(title="acp smoke", workspace_root=str(sandbox.workdir))
    )
    printer.line(f"nativeSessionId={session.native_session_id}")
    catalog = await driver.get_model_catalog(binding)
    printer.line(
        f"模型目录（来自 session/new 的 availableModels）：{len(catalog.models)} 个"
        + (f"，例如 {catalog.models[0].model_id}" if catalog.models else "")
    )

    printer.section("3/5 session/list 核对（不可作发现）")
    listed = await driver.list_native_sessions(binding)
    report = driver.discovery_reports[binding.id]
    printer.line(f"Driver 账本: {[s.native_session_id for s in listed]}")
    printer.line(
        f"核对: confirmed={list(report.confirmed)} ledgerOnly={list(report.ledger_only)} "
        f"agentOnly={list(report.agent_only)} error={report.method_error}"
    )

    printer.section("4/5 跨进程 session/resume + 一个回合")
    conversation = Conversation.create(
        project_id=project.id,
        agent_binding_id=binding.id,
        title="acp smoke",
    ).model_copy(update={"native_session_id": session.native_session_id})

    runtime = await driver.start_runtime(conversation, "card")
    printer.line(
        f"runtime={runtime.runtime_id} resumed={runtime.metadata.get('resumed')} "
        f"（上一个 session/new 的进程已经退出，这是另一个进程）"
    )
    collected: list[AgentEventEnvelope] = []

    async def pump() -> None:
        async for envelope in driver.events(runtime):
            collected.append(envelope)
            printer.envelope(envelope)
            if envelope.event.type == "permission.requested":
                await _answer_permission(driver, runtime, envelope, args.permission, printer)
            if envelope.event.type in {"run.completed", "run.interrupted", "run.failed"}:
                return

    try:
        await driver.send_message(runtime, MessageInput(text=args.prompt))
        try:
            await asyncio.wait_for(pump(), args.prompt_timeout + 30)
        except asyncio.TimeoutError:
            printer.line(f"!! 在 {args.prompt_timeout + 30:g}s 内没有等到 run 终态")
            return 3
    finally:
        await driver.stop_runtime(runtime)

    printer.section("5/5 归并结果")
    state = reduce_events(TimelineState.initial(conversation.id), collected)
    printer.line(f"runState={state.run_state} 事件数={len(collected)}")
    for item in state.items:
        summary = getattr(item, "text", None) or getattr(item, "name", None) or item.kind
        printer.line(f"  - {item.kind:<12} {str(summary)[:100]!r} terminal={item.is_terminal}")
    if state.usage is not None:
        printer.line(
            "usage: "
            + json.dumps(
                state.usage.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False
            )
            + "  ← 只有实测有来源的字段；token 用量在 ACP 上没有来源，故缺席"
        )

    # 明确不支持的两项，真机上也要看得见它们抛的是显式异常而不是空对象。
    printer.section("显式不支持项")
    for label, coroutine in (
        ("load_native_history", driver.load_native_history(binding, session.native_session_id)),
        ("build_external_cli_launch", driver.build_external_cli_launch(conversation)),
    ):
        try:
            await coroutine
        except UnsupportedCapabilityError as exc:
            printer.line(f"{label}: UnsupportedCapabilityError — {str(exc)[:120]}")
        else:  # pragma: no cover - 不该发生
            printer.line(f"!! {label} 没有抛 UnsupportedCapabilityError")
            return 4

    return 0 if state.run_state == "completed" else 1


async def _answer_permission(
    driver: AcpDriver,
    runtime: Any,
    envelope: AgentEventEnvelope,
    policy: str,
    printer: Printer,
) -> None:
    request = envelope.event.request
    options = list(request.options)
    printer.line(
        "  权限请求，选项："
        + ", ".join(f"{o.option_id}({o.kind})" for o in options)
    )
    wanted = "allow" if policy == "allow" else "deny"
    chosen = None
    for option in options:
        kind = (option.kind or "").lower()
        if wanted == "allow" and kind.startswith("allow"):
            chosen = option.option_id
            break
        if wanted == "deny" and (kind.startswith("reject") or kind.startswith("deny")):
            chosen = option.option_id
            break
    if chosen is None and options:
        chosen = options[0].option_id
    if chosen is None:
        printer.line("  !! agent 没给任何选项，按取消处理")
        await driver.resolve_interaction(
            runtime, request.request_id, InteractionResponse(kind="permission", cancelled=True)
        )
        return
    printer.line(f"  回传 optionId={chosen}（--permission {policy}）")
    await driver.resolve_interaction(
        runtime, request.request_id, InteractionResponse(kind="permission", option_id=chosen)
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对真实 ACP agent 跑一次最小回合并打印翻译后的公共事件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent-command",
        default=DEFAULT_AGENT_COMMAND,
        help=(
            f"以 ACP 模式拉起 agent 的命令（默认 {DEFAULT_AGENT_COMMAND!r}）。"
            "子进程的 cwd 是沙盒工作目录，所以命令里的脚本路径请用绝对路径。"
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="发给 agent 的这一轮内容")
    parser.add_argument(
        "--home-mode",
        choices=("isolated", "real"),
        default="isolated",
        help="isolated（默认）= 临时目录里的全新 HERMES_HOME；real = 用你当前环境的 home",
    )
    parser.add_argument(
        "--allow-real-home",
        action="store_true",
        help="--home-mode real 的显式确认：本次会话会真的写进你的原生存储",
    )
    parser.add_argument(
        "--seed-config",
        default=None,
        help="复制进隔离 home 的 config.yaml（会做机密扫描；含凭据即拒绝）",
    )
    parser.add_argument(
        "--permission",
        choices=("deny", "allow"),
        default="deny",
        help="收到 session/request_permission 时怎么答（默认 deny）",
    )
    parser.add_argument("--project-slug", default="acp-smoke")
    parser.add_argument("--backend-key", default="acp")
    parser.add_argument("--call-timeout", type=float, default=60.0)
    parser.add_argument("--prompt-timeout", type=float, default=180.0)
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--json", action="store_true", help="只打印事件的 JSON 行")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    printer = Printer(args.json)
    try:
        sandbox = Sandbox(args).build(args.seed_config, args.allow_real_home)
    except SmokeError as exc:
        print(f"拒绝执行：{exc}", file=sys.stderr)
        return 2

    printer.section("0/5 沙盒")
    printer.line(json.dumps(sandbox.describe(), ensure_ascii=False, indent=2))
    printer.line(f"agent 命令: {args.agent_command}")
    printer.line(f"开始时间: {datetime.now().isoformat(timespec='seconds')}")

    if args.dry_run:
        printer.line("\n[dry-run] 会执行：probe → session/new → session/list 核对 →")
        printer.line("[dry-run] 另起一个进程 session/resume → 一个回合 → 归并并打印事件")
        return 0

    try:
        return asyncio.run(run_smoke(args, sandbox, printer))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要把失败原样报出来
        print(f"\n失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        for note in sandbox.cleanup():
            print(note, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
