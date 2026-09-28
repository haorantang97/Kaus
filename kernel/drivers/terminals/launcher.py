"""Installed macOS terminal adapters; no arbitrary launcher commands."""
from __future__ import annotations
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import stat
import time

def terminal_app_id(app: str) -> str:
    aliases = {"terminal": "terminal", "terminal.app": "terminal", "cmux.app": "cmux",
               "iterm": "iterm2", "iterm.app": "iterm2", "iterm2.app": "iterm2"}
    return aliases.get(app.lower(), app.lower())


def terminal_app_path(app: str) -> Path | None:
    names = {"cmux": "cmux.app", "terminal": "Terminal.app", "iterm2": "iTerm.app"}
    name = names.get(terminal_app_id(app))
    if name is None:
        return None
    for root in (Path("/System/Applications/Utilities"), Path("/Applications"), Path.home() / "Applications"):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def installed_terminals() -> list[dict[str, object]]:
    return [{"id": key, "label": label, "installed": terminal_app_path(key) is not None}
            for key, label in (("cmux", "cmux"), ("terminal", "Terminal"), ("iterm2", "iTerm2"))]


def cmux_environment() -> dict[str, str]:
    """Use the running app's advertised socket, preserving its access policy."""
    environment = dict(os.environ)
    for root in (Path.home() / ".local/state/cmux", Path.home() / "Library/Application Support/cmux"):
        marker = root / "last-socket-path"
        try:
            info = marker.stat()
            if info.st_uid != os.getuid() or info.st_size > 4096:
                continue
            target = Path(marker.read_text(encoding="utf-8").strip())
            if not target.is_absolute() or target.parent.resolve() not in (root.resolve(), Path("/private/tmp")):
                continue
            socket_info = target.stat()
            if socket_info.st_uid == os.getuid() and stat.S_ISSOCK(socket_info.st_mode):
                environment["CMUX_SOCKET_PATH"] = str(target)
                break
        except (OSError, ValueError):
            continue
    return environment


def macos_open(app: str, script_path: Path, *, cwd: str | None = None,
               title: str | None = None, directory_only: bool = False) -> tuple[bool, str | None]:
    """Launch only a supported terminal; cmux uses its native workspace API."""
    if shutil.which("open") is None:
        return False, "当前系统不支持打开本地终端"
    app_id = terminal_app_id(app)
    app_path = terminal_app_path(app_id)
    if app_path is None:
        return False, "终端未安装，请在设置中选择可用的终端"
    try:
        if app_id == "cmux":
            cli = app_path / "Contents/Resources/bin/cmux"
            if not cli.is_file() or not os.access(cli, os.X_OK):
                return False, "cmux 的命令行工具不可用"
            subprocess.run(["open", "-a", str(app_path)], check=True, capture_output=True, text=True, timeout=10)
            # Only readiness is retried. Never retry workspace creation: a delayed
            # response must not create a second writer of the same native session.
            for attempt in range(20):
                environment = cmux_environment()
                ready = subprocess.run([str(cli), "ping"], capture_output=True, text=True, timeout=2, env=environment)
                if ready.returncode == 0:
                    break
                if "access denied" in (ready.stderr + ready.stdout).lower():
                    return False, "cmux 禁止外部连接，请在 cmux 设置中允许外部程序连接"
                if attempt == 19:
                    return False, "无法连接 cmux，请检查其 Socket 访问设置"
                time.sleep(0.1)
            command = [str(cli), "new-workspace", "--name", title or "Kaus"]
            if cwd:
                command.extend(("--cwd", cwd))
            if not directory_only:
                command.extend(("--command", shlex.join(("/bin/zsh", str(script_path)))))
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=10, env=environment)
        elif app_id == "iterm2":
            # iTerm's documented scripting API opens a fresh session; it never
            # types a command into an existing user-owned session.
            import json
            command = shlex.join(("/bin/zsh", str(script_path)))
            script = 'tell application "iTerm"\nactivate\ncreate window with default profile command ' + json.dumps(command, ensure_ascii=False) + '\nend tell'
            subprocess.run(["/usr/bin/osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
        else:
            subprocess.run(["open", "-a", str(app_path), str(script_path)],
                           check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError:
        return False, f"无法打开 {app}，请检查终端是否可用"
    except subprocess.TimeoutExpired:
        return False, f"打开 {app} 超时，请自己复制命令到终端里跑"
    except OSError as exc:  # pragma: no cover - 极少见（fork 失败之类）
        return False, f"打开 {app} 失败：{type(exc).__name__}: {exc}"
    return True, None
