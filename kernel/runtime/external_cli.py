"""Terminal Launcher 与外部进程监视器（v1.0 §8.5 / §8.7；Phase 4）。

分层（v1.0 §8.5）
-----------------
::

    Backend Driver          造 native command（`build_external_cli_launch`）
            ↓
    Terminal Launcher       选终端应用、写包装脚本、开窗（本模块）

终端应用不是 Agent，也不参与模型与能力决策：本模块只认识
:class:`~drivers.base.CliLaunchSpec` 这一个公共 DTO，不认识任何一家引擎。

包装脚本干的四件事
------------------
1. ``export KAUS_LAUNCH_ID=<launch_id>`` —— v1.0 §8.7 的 Launch Correlation ID。
   外部进程靠它与本次启动**确定性**对上号，禁止「开完终端再按最新 Session 猜」。
2. ``echo $$ > pid`` —— 外部进程识别。cmux / Terminal 的窗口句柄不可靠也不必要，
   §8.7 只要求确定性关联，pid 足够。
3. ``trap 'echo $? > exit' EXIT`` —— 退出检测与 ``terminal_launches.exit_status``。
4. 执行 Driver 给的命令，**全部 shlex 引用**，不做任何 shell 字符串拼接。

任务书原文的脚本骨架里 3 之后是 ``exec <command>``；这里**没有** ``exec``：
``exec`` 会用目标命令替换掉这个 shell，EXIT trap 从此不再触发，``exit`` 文件
永远写不出来——而第 3 件事与监视器、``exit_status`` 都指着它。去掉 ``exec``
是让这四件事同时成立的最小改法（副作用只有多留一个 shell 进程，它正好是我们
写进 ``pid`` 的那个）。

安全边界（v1.0 §16.6 / AD-10，非可协商）
-----------------------------------------
- 脚本里**不出现任何密钥值**。``env_passthrough`` 只写变量**名**（落在注释里
  给人看、落在 :class:`~app.runtimes.models.TerminalLaunch` 里给账本看），
  值由用户自己的 shell 提供，本模块从不读 ``.env``、不读凭据文件、
  不把 ``os.environ`` 的任何值写进文件。
- ``command_summary`` 同样只由已引用的 argv 拼成——Driver 已保证规格里没有 Secret。

降级（不抛异常）
----------------
打开失败返回 ``launched=False`` 与失败原因，交接层释放本次预留写权并保留站内对话。
``command_summary`` 保留作启动记录，不代表用户应绕过写权直接手工续接。
"""

from __future__ import annotations

import asyncio
import os
import pwd
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence
from uuid import uuid4

from app.ids import terminal_launch_id
from app.runtimes.models import TerminalLaunch
from app.runtimes.repository import TerminalLaunchRepository
from drivers.base import CliLaunchSpec
from runtime.lease_manager import LeaseManager

#: 缺省终端应用（`dashboard-config.json` 的 ``terminal.app`` 可覆盖）。
DEFAULT_TERMINAL_APP = "cmux"

#: v1.0 §8.7 的 Launch Correlation ID 在环境里的名字。
LAUNCH_ID_ENV_VAR = "KAUS_LAUNCH_ID"

#: 脚本内部用的两个路径变量（不 export：它们是脚本自己的记账，不给子进程看）。
PID_FILE_VAR = "KAUS_PID_FILE"
EXIT_FILE_VAR = "KAUS_EXIT_FILE"

#: 一次启动在 ``state/launches/<launch_id>/`` 下的三个文件。
SCRIPT_FILENAME = "run.command"
PID_FILENAME = "pid"
EXIT_FILENAME = "exit"

#: GUI 终端不继承登录 shell 的 PATH，这一段是旧启动器一直在用的兜底前缀。
DEFAULT_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

#: 拉起后等 ``pid`` 文件出现的上限（秒）。等不到就把 ``external_process_ref``
#: 留成 ``None``——不编造一个 pid（N §13.1）。
DEFAULT_PID_TIMEOUT = 3.0

#: 监视器周期（秒）。与接入层 60s 的维护任务并列，各跑各的。
DEFAULT_MONITOR_INTERVAL = 5.0

#: 脚本与其所在目录的权限：只有本用户能读能跑。
SCRIPT_MODE = 0o700
LAUNCH_DIR_MODE = 0o700


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def launch_dir(root: Path, launch_id: str) -> Path:
    """一次启动的工作目录：``<root>/<launch_id>/``。"""
    return Path(root) / launch_id


def quote_command(command: Sequence[str]) -> str:
    """argv → 一行**已引用**的命令串。这是本模块唯一一处「拼命令」。"""
    return " ".join(shlex.quote(str(part)) for part in command)


def build_launch_script(
    spec: CliLaunchSpec,
    *,
    launch_id: str,
    pid_path: Path,
    exit_path: Path,
    path_prefix: str = DEFAULT_PATH_PREFIX,
) -> str:
    """造 ``run.command`` 的内容。纯函数，可单测（见模块文档的四件事）。"""
    lines = [
        "#!/bin/zsh",
        "# Kaus Terminal Launcher（v1.0 §8.5）。本文件不含任何密钥：",
        "# 要透传的环境变量只列名字，值由你自己的 shell 提供（AD-10）。",
    ]
    if spec.env_passthrough:
        lines.append("# env passthrough: " + " ".join(spec.env_passthrough))
    prefix = os.pathsep.join((*spec.path_prefix, path_prefix))
    lines.append(f"export PATH={shlex.quote(prefix)}:$PATH")
    lines.append(f"export {LAUNCH_ID_ENV_VAR}={shlex.quote(launch_id)}")
    if spec.cwd:
        lines.append(f"cd {shlex.quote(str(spec.cwd))} || exit 1")
    # 两个路径先落成变量再用：trap 的动作本身是**单引号串**，把一个 shlex 引用过
    # 的路径直接塞进去，遇到路径里带单引号就会把这行拆坏。
    lines.append(f"{PID_FILE_VAR}={shlex.quote(str(pid_path))}")
    lines.append(f"{EXIT_FILE_VAR}={shlex.quote(str(exit_path))}")
    lines.append(f'echo $$ > "${PID_FILE_VAR}"')
    lines.append(f"trap 'echo $? > \"${EXIT_FILE_VAR}\"' EXIT")
    lines.append(quote_command(spec.command))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 打开终端
# --------------------------------------------------------------------------- #

#: 打开器契约：``(app, script_path) -> (是否真的打开了, 失败原因)``。
#: 抽成参数是为了让测试用一个假启动器（``sh -c <script>``）代替 macOS 的 ``open``。
Opener = Callable[[str, Path], "tuple[bool, str | None]"]


from drivers.terminals.launcher import macos_open


@dataclass(frozen=True)
class LaunchResult:
    """一次启动的结果。``launched=False`` 是**正常返回**，不是错误。"""

    launch: TerminalLaunch
    launched: bool
    command_summary: str
    script_path: Path
    reason: str | None = None


class TerminalLauncher:
    """把一份 :class:`~drivers.base.CliLaunchSpec` 变成一条 ``TerminalLaunch``。

    参数
    ----
    root:
        ``state/launches``。每次启动在它下面开一个以 ``launch_id`` 命名的目录。
    app:
        终端应用名（``dashboard-config.json`` 的 ``terminal.app``，缺省 cmux）。
    opener:
        打开器，缺省 :func:`macos_open`；测试传一个假启动器。
    """

    def __init__(
        self,
        *,
        root: Path,
        app: str = DEFAULT_TERMINAL_APP,
        opener: Opener = macos_open,
        pid_timeout: float = DEFAULT_PID_TIMEOUT,
        path_prefix: str = DEFAULT_PATH_PREFIX,
        clock: Callable[[], datetime] = _utcnow,
        app_provider: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.app = app or DEFAULT_TERMINAL_APP
        self._opener = opener
        self.pid_timeout = pid_timeout
        self.path_prefix = path_prefix
        self._clock = clock
        self._app_provider = app_provider

    def selected_app(self) -> str:
        return self._app_provider() if self._app_provider else self.app

    async def open_directory(self, cwd: Path, *, title: str = "Kaus") -> dict[str, object]:
        """A project terminal opens its directory without acquiring a chat lease."""
        app = self.selected_app()
        directory = self.root / ("workspace-" + uuid4().hex)
        directory.mkdir(parents=True, mode=LAUNCH_DIR_MODE)
        shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
        if not Path(shell).is_file() or not os.access(shell, os.X_OK):
            shell = "/bin/zsh"
        script = directory / SCRIPT_FILENAME
        script.write_text("#!/bin/zsh\ncd " + shlex.quote(str(cwd)) + " || exit 1\nexec " + shlex.quote(shell) + " -l\n", encoding="utf-8")
        _harden(script, SCRIPT_MODE)
        if self._opener is macos_open:
            opened, reason = await asyncio.to_thread(macos_open, app, script, cwd=str(cwd), title=title, directory_only=True)
        else:
            opened, reason = await asyncio.to_thread(self._opener, app, script)
        return {"launcher": app, "cwd": str(cwd), "launched": opened, "reason": reason}

    async def launch(
        self, spec: CliLaunchSpec, *, conversation_id: str, launch_uuid: str | None = None
    ) -> LaunchResult:
        """写脚本 → 开窗 → 读 pid。全程不抛，失败即降级。"""
        summary = quote_command(spec.command)
        # v1.0 §8.7：关联 id 就是 launch id 本身（脚本把它导进 KAUS_LAUNCH_ID）。
        # 所以 id 得先算出来，再拿去当 correlation_id。Driver 自己那个 correlation
        # 只作记录——它按会话取值，同一条会话启动两次就会撞 correlation 的唯一约束。
        launch_uuid = launch_uuid or uuid4().hex
        app = self.selected_app()
        launch = TerminalLaunch.create(
            conversation_id=conversation_id,
            launcher=app,
            command_summary=summary,
            correlation_id=terminal_launch_id(launch_uuid),
            env_passthrough=spec.env_passthrough,
            metadata=(
                {"driverCorrelationId": spec.correlation_id}
                if spec.correlation_id
                else {}
            ),
            launch_uuid=launch_uuid,
            launched_at=self._clock(),
        )

        directory = launch_dir(self.root, launch.id)
        directory.mkdir(parents=True, exist_ok=True)
        _harden(directory, LAUNCH_DIR_MODE)
        pid_path = directory / PID_FILENAME
        exit_path = directory / EXIT_FILENAME
        script_path = directory / SCRIPT_FILENAME
        script_path.write_text(
            build_launch_script(
                spec,
                launch_id=launch.id,
                pid_path=pid_path,
                exit_path=exit_path,
                path_prefix=self.path_prefix,
            ),
            encoding="utf-8",
        )
        _harden(script_path, SCRIPT_MODE)

        if self._opener is macos_open:
            opened, reason = await asyncio.to_thread(macos_open, app, script_path, cwd=spec.cwd, title=spec.title)
        else:
            opened, reason = await asyncio.to_thread(self._opener, app, script_path)
        if not opened and read_pid(pid_path) is not None:
            opened, reason = True, None
        if not opened:
            return LaunchResult(
                launch=launch.evolve(
                    status="failed",
                    exit_code=None,
                    exited_at=self._clock(),
                    metadata={**launch.metadata, "degraded": reason},
                ),
                launched=False,
                command_summary=summary,
                script_path=script_path,
                reason=reason,
            )
        pid = await self._await_pid(pid_path)
        return LaunchResult(
            launch=launch.evolve(external_process_ref=pid, status="running"),
            launched=True,
            command_summary=summary,
            script_path=script_path,
        )

    async def _await_pid(self, pid_path: Path) -> str | None:
        deadline = asyncio.get_running_loop().time() + self.pid_timeout
        while True:
            pid = read_pid(pid_path)
            if pid is not None:
                return pid
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)


def _harden(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - 某些文件系统不支持
        pass


def read_pid(pid_path: Path) -> str | None:
    """读 ``pid`` 文件。没有 / 空 / 不是数字 → ``None``（不编造）。"""
    try:
        raw = Path(pid_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or not raw.isdigit():
        return None
    return raw


def read_exit_code(exit_path: Path) -> int | None:
    """读 ``exit`` 文件。文件不在就是「还没退出」。"""
    try:
        raw = Path(exit_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        # 文件在但内容不是数字：进程确实退了，退出码不可知——按未知退出处理。
        return -1


def pid_alive(pid: str) -> bool:
    """pid 还在不在。``kill(pid, 0)`` 只探测，不发信号。"""
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:  # 进程在，只是不属于我们
        return True
    except OSError:  # pragma: no cover
        return False
    return True


# --------------------------------------------------------------------------- #
# 外部进程监视器（任务书第 3 件）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExitSignal:
    """监视器发现的一次外部退出。"""

    conversation_id: str
    launch: TerminalLaunch
    exit_code: int | None
    #: 判据：``exit-file``（读到了退出码）或 ``pid-gone``（进程没了但没留退出码）。
    source: str


class ExternalCliMonitor:
    """每 ``interval`` 秒扫一遍活跃的外部 lease，发现退出就回调。

    判据两条，顺序固定：先看 ``exit`` 文件（有它就有确定的退出码），
    再看 ``pid`` 是否还活着（进程被 kill -9 之类不会留下 exit 文件）。
    ``pid`` 还没写出来时**什么都不做**——那是「刚拉起还没跑起来」，
    不是「已经退出」。

    回调 ``on_exit`` 由接入层给，做的就是任务书第 4 件的「自动回 Card」校准。
    回调抛异常不会让监视器死掉：本轮记一句，下轮照跑。
    """

    def __init__(
        self,
        *,
        leases: LeaseManager,
        terminal_launches: TerminalLaunchRepository,
        root: Path,
        on_exit: Callable[[ExitSignal], Awaitable[None]],
        interval: float = DEFAULT_MONITOR_INTERVAL,
        is_alive: Callable[[str], bool] = pid_alive,
        clock: Callable[[], datetime] = _utcnow,
        log: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._leases = leases
        self._launches = terminal_launches
        self.root = Path(root)
        self._on_exit = on_exit
        self.interval = interval
        self._is_alive = is_alive
        self._clock = clock
        self._log = log
        self.rounds = 0
        self.last_error: str | None = None

    async def poll_once(self) -> tuple[ExitSignal, ...]:
        """扫一轮，返回本轮发现的退出。测试直接断言它，不必等 5s。"""
        signals: list[ExitSignal] = []
        for description in await self._leases.describe_all():
            if description.owner_type != "external-cli":
                continue
            launch_id = str(description.metadata.get("launchId") or "")
            if not launch_id:
                continue
            launch = await self._launches.get(launch_id)
            if launch is None or launch.status in ("exited", "failed"):
                continue
            signal = self._inspect(description.conversation_id, launch)
            if signal is None:
                pid = launch.external_process_ref or read_pid(launch_dir(self.root, launch.id) / PID_FILENAME)
                if pid is not None and self._is_alive(pid):
                    await self._leases.heartbeat(description.conversation_id, at=self._clock())
                continue
            await self._launches.save(
                launch.mark_exited(
                    exit_code=signal.exit_code if signal.exit_code is not None else -1,
                    at=self._clock(),
                )
            )
            signals.append(signal)
        for signal in signals:
            try:
                await self._on_exit(signal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 回调坏了不该拖死监视器
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[external-cli] 退出回调失败：{self.last_error}")
        self.rounds += 1
        return tuple(signals)

    def _inspect(
        self, conversation_id: str, launch: TerminalLaunch
    ) -> ExitSignal | None:
        directory = launch_dir(self.root, launch.id)
        code = read_exit_code(directory / EXIT_FILENAME)
        if code is not None:
            return ExitSignal(
                conversation_id=conversation_id,
                launch=launch,
                exit_code=code,
                source="exit-file",
            )
        pid = launch.external_process_ref or read_pid(directory / PID_FILENAME)
        if pid is None:
            # 还没写出 pid：刚拉起，什么都别做。
            return None
        if self._is_alive(pid):
            return None
        return ExitSignal(
            conversation_id=conversation_id,
            launch=launch,
            exit_code=None,
            source="pid-gone",
        )

    async def loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不得静默退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[external-cli] 监视器本轮失败：{self.last_error}")
            await asyncio.sleep(self.interval)


__all__ = [
    "DEFAULT_MONITOR_INTERVAL",
    "DEFAULT_PATH_PREFIX",
    "DEFAULT_PID_TIMEOUT",
    "DEFAULT_TERMINAL_APP",
    "EXIT_FILENAME",
    "ExitSignal",
    "ExternalCliMonitor",
    "LAUNCH_ID_ENV_VAR",
    "LaunchResult",
    "Opener",
    "PID_FILENAME",
    "SCRIPT_FILENAME",
    "TerminalLauncher",
    "build_launch_script",
    "launch_dir",
    "macos_open",
    "pid_alive",
    "quote_command",
    "read_exit_code",
    "read_pid",
]
