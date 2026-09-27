"""Terminal Launcher 与外部进程监视器（任务书第 2、3 件）。

不碰真实终端：``open`` 一律被一个假启动器替掉——要么什么都不做，要么用
``sh`` 把生成的脚本真跑一遍（这也是端到端用例的做法）。既不调用 macOS 的
``open``，也不读任何凭据。
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.memory import in_memory_repository_set
from drivers.base import CliLaunchSpec
from runtime.external_cli import (
    EXIT_FILENAME,
    EXIT_FILE_VAR,
    LAUNCH_ID_ENV_VAR,
    PID_FILENAME,
    PID_FILE_VAR,
    ExternalCliMonitor,
    TerminalLauncher,
    build_launch_script,
    launch_dir,
)
from runtime.lease_manager import LeaseManager

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
CONVERSATION = "conversation:22222222-2222-4222-8222-222222222222"


def _spec(command: tuple[str, ...] = ("mock-agent", "chat"), **changes) -> CliLaunchSpec:
    return CliLaunchSpec(
        command=command,
        env_passthrough=("PATH", "HOME"),
        title="演示会话",
        **changes,
    )


# --------------------------------------------------------------------------- #
# 脚本内容
# --------------------------------------------------------------------------- #


def test_script_carries_launch_id_pid_and_exit_trap(tmp_path: Path) -> None:
    """v1.0 §8.7 的关联 id + 外部进程识别 + 退出检测，三样都得在脚本里。"""
    script = build_launch_script(
        _spec(cwd="/tmp/work"),
        launch_id="launch:abc",
        pid_path=tmp_path / PID_FILENAME,
        exit_path=tmp_path / EXIT_FILENAME,
    )
    assert f"export {LAUNCH_ID_ENV_VAR}={shlex.quote('launch:abc')}" in script
    assert f"{PID_FILE_VAR}={shlex.quote(str(tmp_path / PID_FILENAME))}" in script
    assert f"{EXIT_FILE_VAR}={shlex.quote(str(tmp_path / EXIT_FILENAME))}" in script
    assert f'echo $$ > "${PID_FILE_VAR}"' in script
    assert f"trap 'echo $? > \"${EXIT_FILE_VAR}\"' EXIT" in script
    assert f"cd {shlex.quote('/tmp/work')} || exit 1" in script
    # `exec` 会让 EXIT trap 永不触发，退出码就写不出来——本实现刻意不用它。
    assert "exec " not in script


def test_script_quotes_command_and_never_writes_env_values(tmp_path: Path) -> None:
    """AD-10：命令全部 shlex 引用；env 只出现**名字**，不出现任何值。"""
    script = build_launch_script(
        _spec(command=("agent", "--title", "rm -rf /; echo pwned")),
        launch_id="launch:abc",
        pid_path=tmp_path / PID_FILENAME,
        exit_path=tmp_path / EXIT_FILENAME,
    )
    assert "'rm -rf /; echo pwned'" in script
    assert "# env passthrough: PATH HOME" in script
    # 变量名出现在注释里，值一个都不出现（脚本里没有 `NAME=` 形态的赋值，
    # 除了我们自己那两条 export）。
    assignments = [
        line for line in script.splitlines() if line.startswith("export ")
    ]
    assert len(assignments) == 2  # PATH 前缀 + KAUS_LAUNCH_ID
    assert "HOME=" not in script


# --------------------------------------------------------------------------- #
# 启动与降级
# --------------------------------------------------------------------------- #


async def test_missing_open_degrades_to_command_summary(tmp_path: Path) -> None:
    """``open`` 不在（非 macOS）→ 不抛，返回可复制的命令与 ``launched=False``。"""

    def no_opener(app: str, script_path: Path) -> tuple[bool, str | None]:
        return False, "本机没有 macOS 的 open 命令"

    launcher = TerminalLauncher(root=tmp_path, opener=no_opener)
    result = await launcher.launch(_spec(), conversation_id=CONVERSATION)
    assert result.launched is False
    assert result.command_summary == "mock-agent chat"
    assert result.reason and "open" in result.reason
    assert result.launch.status == "failed"
    assert result.launch.external_process_ref is None
    # 脚本仍然落了盘：用户可以自己去跑它。
    assert result.script_path.exists()


async def test_launch_records_pid_and_correlation_id(tmp_path: Path) -> None:
    """真跑一次脚本（用 ``sh`` 代替 ``open``）：pid 读得到，关联 id 就是 launch id。"""
    processes: list[subprocess.Popen] = []

    def sh_opener(app: str, script_path: Path) -> tuple[bool, str | None]:
        processes.append(subprocess.Popen(["sh", str(script_path)]))
        return True, None

    launcher = TerminalLauncher(root=tmp_path, opener=sh_opener, pid_timeout=5.0)
    result = await launcher.launch(
        _spec(command=("sh", "-c", "exit 0")), conversation_id=CONVERSATION
    )
    processes[0].wait(timeout=10)
    assert result.launched is True
    assert result.launch.external_process_ref is not None
    assert result.launch.correlation_id == result.launch.id
    directory = launch_dir(tmp_path, result.launch.id)
    assert (directory / PID_FILENAME).exists()
    # EXIT trap 真的写出了退出码。
    for _ in range(50):
        if (directory / EXIT_FILENAME).exists():
            break
        await asyncio.sleep(0.05)
    assert (directory / EXIT_FILENAME).read_text().strip() == "0"


# --------------------------------------------------------------------------- #
# 监视器
# --------------------------------------------------------------------------- #


async def _monitored(tmp_path: Path, *, pid: str | None, is_alive) -> tuple:
    repositories = in_memory_repository_set()
    leases = LeaseManager(repositories.leases, heartbeat_timeout=timedelta(seconds=90))
    launcher = TerminalLauncher(
        root=tmp_path, opener=lambda app, path: (True, None), pid_timeout=0.0
    )
    result = await launcher.launch(_spec(), conversation_id=CONVERSATION)
    launch = result.launch.evolve(external_process_ref=pid)
    await repositories.terminal_launches.save(launch)
    await leases.try_acquire_external_cli(
        CONVERSATION,
        owner_id=launch.id,
        metadata={"launchId": launch.id},
        at=T0,
    )
    seen: list = []
    monitor = ExternalCliMonitor(
        leases=leases,
        terminal_launches=repositories.terminal_launches,
        root=tmp_path,
        on_exit=lambda signal: _record(seen, signal),
        is_alive=is_alive,
    )
    return monitor, launch, seen, repositories


async def _record(bucket: list, signal) -> None:
    bucket.append(signal)


async def test_monitor_reads_exit_file_and_calls_back(tmp_path: Path) -> None:
    """``exit`` 文件出现 → 回调，退出码入 ``terminal_launches``。"""
    monitor, launch, seen, repositories = await _monitored(
        tmp_path, pid="1", is_alive=lambda pid: True
    )
    (launch_dir(tmp_path, launch.id) / EXIT_FILENAME).write_text("3\n")
    signals = await monitor.poll_once()
    assert len(signals) == 1 and signals[0].source == "exit-file"
    assert signals[0].exit_code == 3
    assert [s.conversation_id for s in seen] == [CONVERSATION]
    stored = await repositories.terminal_launches.get(launch.id)
    assert stored is not None and stored.status == "failed" and stored.exit_code == 3
    # 已经收过的启动不再重复回调。
    assert await monitor.poll_once() == ()


async def test_monitor_reports_pid_gone_without_exit_file(tmp_path: Path) -> None:
    """进程被 kill 掉没留退出码 → 照样进 reconciling，退出码为未知。"""
    monitor, launch, seen, repositories = await _monitored(
        tmp_path, pid="424242", is_alive=lambda pid: False
    )
    signals = await monitor.poll_once()
    assert len(signals) == 1 and signals[0].source == "pid-gone"
    assert signals[0].exit_code is None
    assert len(seen) == 1
    stored = await repositories.terminal_launches.get(launch.id)
    assert stored is not None and stored.status == "failed"


async def test_monitor_waits_for_pid_before_declaring_exit(tmp_path: Path) -> None:
    """pid 还没写出来 = 刚拉起，什么都不做（不能把「还没跑起来」当成「退了」）。"""
    monitor, _launch, seen, _repositories = await _monitored(
        tmp_path, pid=None, is_alive=lambda pid: False
    )
    assert await monitor.poll_once() == ()
    assert seen == []


async def test_terminal_preference_is_read_for_every_launch(tmp_path):
    choice = ["cmux"]
    opened = []
    def opener(app, script):
        opened.append(app)
        (script.parent / PID_FILENAME).write_text("1")
        return True, None
    launcher = TerminalLauncher(root=tmp_path, app_provider=lambda: choice[0], opener=opener)
    first = await launcher.launch(_spec(), conversation_id=CONVERSATION)
    choice[0] = "terminal"
    second = await launcher.launch(_spec(), conversation_id=CONVERSATION)
    assert opened == ["cmux", "terminal"]
    assert [first.launch.launcher, second.launch.launcher] == opened


async def test_monitor_keeps_live_external_writer_lease_fresh(tmp_path):
    monitor, launch, seen, repositories = await _monitored(tmp_path, pid="1", is_alive=lambda _: True)
    await monitor.poll_once()
    owner = await monitor._leases.describe(CONVERSATION)
    assert owner is not None and not owner.is_stale
    assert owner.owner_id == launch.id
    assert seen == []


def test_runtime_path_precedes_system_path_without_exposing_other_env(tmp_path):
    script = build_launch_script(_spec(path_prefix=("/a path/runtime/bin",)),
        launch_id="launch:runtime", pid_path=tmp_path / "pid", exit_path=tmp_path / "exit")
    assert "export PATH='/a path/runtime/bin:" in script


def test_cmux_uses_native_workspace_command_with_quoted_script(tmp_path, monkeypatch):
    from drivers.terminals import launcher as adapter
    app = tmp_path / "cmux.app"
    cli = app / "Contents/Resources/bin/cmux"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    cli.chmod(0o700)
    monkeypatch.setattr(adapter, "terminal_app_path", lambda _: app)
    monkeypatch.setattr(adapter.shutil, "which", lambda _: "/usr/bin/open")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="PONG", stderr="")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    script = tmp_path / "a ' $(literal).command"
    ok, reason = adapter.macos_open("cmux", script, cwd="/my project", title="Demo")
    assert ok and reason is None
    create = calls[-1]
    assert create[:2] == [str(cli), "new-workspace"]
    assert create[create.index("--cwd") + 1] == "/my project"
    assert shlex.split(create[create.index("--command") + 1]) == ["/bin/zsh", str(script)]
    assert calls[0] == ["open", "-a", str(app)]


def test_unknown_terminal_never_executes_arbitrary_command(tmp_path, monkeypatch):
    from drivers.terminals import launcher as adapter
    calls = []
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    ok, _ = adapter.macos_open("sh -c evil", tmp_path / "run.command")
    assert not ok and calls == []


def test_iterm_quotes_unicode_script_path_without_unicode_escape_corruption(tmp_path, monkeypatch):
    from drivers.terminals import launcher as adapter
    monkeypatch.setattr(adapter, "terminal_app_path", lambda _: tmp_path / "iTerm.app")
    monkeypatch.setattr(adapter.shutil, "which", lambda _: "/usr/bin/open")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    script = tmp_path / "中文项目 ' literal.command"
    ok, _ = adapter.macos_open("iterm2", script)
    assert ok
    source = calls[0][-1]
    assert "中文项目" in source
    assert "\\u4e2d" not in source
    assert "create window with default profile command" in source
