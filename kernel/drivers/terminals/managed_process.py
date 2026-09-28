"""Launch-scoped native entrypoint and verified terminal process shutdown.

cmux records this entrypoint as the executable to restore. Each launch can be
consumed once: reopening a saved terminal cannot bypass Kaus's session lease.
Only paths and process identity are stored here, never environment values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time


def process_identity(pid: int) -> dict | None:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "uid=,pgid=,lstart=,stat="],
        capture_output=True, text=True, timeout=3,
    )
    fields = result.stdout.strip().split()
    if result.returncode or len(fields) < 8 or fields[-1].startswith("Z"):
        return None
    return {"pid": pid, "uid": int(fields[0]), "pgid": int(fields[1]),
            "started": " ".join(fields[2:-1])}


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _group_active(pgid: int) -> bool:
    result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        return True
    return any(parts[0] == str(pgid) and not parts[1].startswith("Z")
               for line in result.stdout.splitlines() if len(parts := line.split()) == 2)


def enter(directory: Path, arguments: list[str]) -> int:
    config = json.loads((directory / "native.json").read_text())
    executable = config["executable"]
    # Version/help probes made by a terminal adapter must not consume a launch.
    if arguments not in (["--version"], ["--help"]):
        try:
            if (directory / "revoked").exists() or (directory / "exit").exists():
                raise FileExistsError
            claim = os.open(directory / "claimed", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(claim)
        except FileExistsError:
            print("会话已结束或已交接，请从 Kaus 重新打开。", file=sys.stderr)
            return 125
        # Isolate the agent and its tools from the terminal's own shell. Keep the
        # controlling tty so native interactive CLIs can still use /dev/tty.
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
        if os.isatty(0):
            previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            try:
                os.tcsetpgrp(0, os.getpgrp())
            finally:
                signal.signal(signal.SIGTTOU, previous)
        identity = process_identity(os.getpid())
        if identity is None:
            return 125
        _write_json(directory / "process.json", identity)
        if (directory / "revoked").exists():
            return 125
    environment = dict(os.environ)
    config_directory = config.get("config_directory")
    if config_directory:
        environment[config_directory[0]] = config_directory[1]
    # Descendant CLI launches must resolve the user's real binary, not reuse
    # this one-shot entrypoint. cmux has already captured the guarded executable.
    custom_path_key = config.get("custom_path_key")
    if custom_path_key:
        environment[custom_path_key] = executable
    if custom_path_key == "CMUX_CUSTOM_CLAUDE_PATH":
        # The shell entrypoint is a shim from cmux's perspective. This is the
        # actual native boundary, so child Claude sessions need a fresh wrapper
        # invocation rather than inheriting the shim's re-entry counter.
        for key in tuple(environment):
            if key.startswith("cmux_claude_wrapper_reexec_"):
                environment.pop(key, None)
    os.execvpe(executable, [executable, *arguments], environment)
    return 127  # pragma: no cover


def stop(directory: Path, pid_hint: str | None, *, timeout: float = 3.0) -> tuple[bool, str | None]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "revoked").touch(mode=0o600)
    try:
        saved = json.loads((directory / "process.json").read_text())
    except FileNotFoundError:
        # A new launch has not reached its native entrypoint yet. Revocation
        # is checked again after recording identity, before exec, closing the
        # race with an entrypoint that is still starting up.
        if (directory / "native.json").exists():
            return True, None
        # Older launch scripts can be stopped only when the recorded shell is
        # still the leader of its own group and is running this exact script.
        if not pid_hint or not pid_hint.isdigit():
            return False, "尚未确认终端进程，请稍后重试"
        saved = process_identity(int(pid_hint))
        if saved is None:
            if _group_active(int(pid_hint)):
                return False, "终端子进程仍在运行，请先在终端结束任务"
            return True, None
        command = subprocess.run(["/bin/ps", "-p", pid_hint, "-o", "command="],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        try:
            is_our_script = str(directory / "run.command") in shlex.split(command)
        except ValueError:
            is_our_script = False
        if not is_our_script:
            return False, "无法确认终端进程，请先退出终端中的 Agent"
    except (OSError, ValueError):
        return False, "无法确认终端进程，请先退出终端中的 Agent"
    current = process_identity(int(saved["pid"]))
    if current is None:
        if _group_active(int(saved["pgid"])):
            return False, "终端子进程仍在运行，请先在终端结束任务"
        return True, None
    if current != saved or saved["uid"] != os.getuid() or saved["pgid"] != saved["pid"]:
        return False, "无法确认终端进程，请先退出终端中的 Agent"
    pgid = int(saved["pgid"])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, "无法结束终端进程，请先退出终端中的 Agent"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # The entire group must exit, including tools that outlive the CLI.
            if not _group_active(pgid):
                return True, None
            time.sleep(0.05)
    return False, "终端尚未结束，请稍后重试"


if __name__ == "__main__":
    raise SystemExit(enter(Path(sys.argv[1]), sys.argv[2:]))
