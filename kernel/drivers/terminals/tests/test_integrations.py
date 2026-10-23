from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import sys
import time

import pytest

from drivers.base import CliLaunchSpec
from drivers.terminals import integrations, managed_process
from runtime.external_cli import TerminalLauncher


def executable(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)
    return path


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


@pytest.mark.parametrize("family", ["claude-code", "codex", "hermes", "amp"])
def test_wrapper_preserves_binary_arguments_config_and_cmux_context(tmp_path, monkeypatch, family):
    app = tmp_path / "cmux.app"
    name, key = integrations.CMUX_WRAPPERS[family]
    executable(app / "Contents/Resources/bin" / name,
        f"import os,sys\nos.execv(os.environ[{key!r}], [os.environ[{key!r}], '--cmux-hook', *sys.argv[1:]])\n")
    monkeypatch.setattr(integrations, "terminal_app_path", lambda _: app)
    output = tmp_path / "received.json"
    native = executable(tmp_path / "user's chosen binary", "import os,sys,json\n"
        f"open({str(output)!r},'w').write(json.dumps({{'args':sys.argv[1:],'config':os.getenv('CODEX_HOME'),"
        f"'surface':os.getenv('CMUX_SURFACE_ID'),'child_binary':os.getenv({key!r})}}))\n")
    config = str(tmp_path / "profile ' $literal")
    launch = tmp_path / "launch"
    launch.mkdir()
    spec = CliLaunchSpec(command=(str(native), "resume", "session-123", "a ' $(literal)"),
        terminal_integration=family, config_directory=("CODEX_HOME", config))
    command = integrations.prepare_command(spec, launch, "cmux")
    env = {**os.environ, "CMUX_SURFACE_ID": "surface-test"}
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == {
        "args": ["--cmux-hook", "resume", "session-123", "a ' $(literal)"],
        "config": config, "surface": "surface-test", "child_binary": str(native),
    }
    # cmux may restore its captured native entry; that old launch cannot start twice.
    repeated = subprocess.run([str(launch / "native-entry"), "resume", "session-123"],
                              capture_output=True, text=True, timeout=5)
    assert repeated.returncode == 125


@pytest.mark.parametrize("terminal", ["cmux", "terminal", "iterm2"])
def test_missing_vendor_wrapper_keeps_native_cli_working(tmp_path, monkeypatch, terminal):
    monkeypatch.setattr(integrations, "terminal_app_path", lambda _: None)
    output = tmp_path / "ran"
    native = executable(tmp_path / "agent", f"open({str(output)!r},'w').write('ok')\n")
    spec = CliLaunchSpec(command=(str(native),), terminal_integration="codex")
    command = integrations.prepare_command(spec, tmp_path, terminal)
    result = subprocess.run(command, capture_output=True, timeout=5)
    assert result.returncode == 0 and output.read_text() == "ok"


def test_config_directory_rejects_secret_environment_keys():
    with pytest.raises(ValueError):
        CliLaunchSpec(command=("agent",), config_directory=("OPENAI_API_KEY", "/secret"))


async def test_stopping_launch_stops_agent_and_tool_before_release(tmp_path):
    marker = tmp_path / "tool.pid"
    native = executable(tmp_path / "agent", "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"open({str(marker)!r},'w').write(str(p.pid))\ntime.sleep(60)\n")
    processes = []
    def opener(_, script):
        processes.append(subprocess.Popen(["/bin/sh", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return True, None
    launcher = TerminalLauncher(root=tmp_path / "launches", opener=opener)
    result = await launcher.launch(CliLaunchSpec(command=(str(native),)), conversation_id="conversation:22222222-2222-4222-8222-222222222222")
    try:
        wait_for(marker)
        tool_pid = int(marker.read_text())
        stopped, reason = await launcher.stop(result.launch)
        assert stopped, reason
        assert managed_process.process_identity(tool_pid) is None
        processes[0].wait(timeout=5)
        assert (result.script_path.parent / "revoked").exists()
        repeated = subprocess.run([str(result.script_path.parent / "native-entry")], capture_output=True, timeout=5)
        assert repeated.returncode == 125
    finally:
        for process in processes:
            if process.poll() is None:
                await launcher.stop(result.launch)
                process.terminate()
            process.wait(timeout=5)


def test_process_identity_mismatch_never_signals_unrelated_process(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        identity = managed_process.process_identity(process.pid)
        assert identity is not None
        identity["started"] = "old process generation"
        (tmp_path / "process.json").write_text(json.dumps(identity))
        stopped, _ = managed_process.stop(tmp_path, str(process.pid), timeout=0.05)
        assert not stopped and process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize('child_delay,timeout,expected', [(0.3, 2, True), (20, 0.05, False)])
def test_exited_native_leader_allows_bounded_child_cleanup_without_signals(tmp_path, monkeypatch, child_delay, timeout, expected):
    source = ('import os,subprocess,sys\n'
        'sys.stdin.readline()\n'
        f'subprocess.Popen([sys.executable,"-c","import time;time.sleep({child_delay})"],stdin=subprocess.DEVNULL)\n')
    process = subprocess.Popen([sys.executable, '-c', source], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    real_killpg = os.killpg
    try:
        identity = managed_process.process_identity(process.pid)
        assert identity is not None
        (tmp_path / 'process.json').write_text(json.dumps(identity))
        process.stdin.write(b'go\n'); process.stdin.flush()
        process.wait(timeout=5)
        assert managed_process._group_active(process.pid)
        signals = []
        monkeypatch.setattr(managed_process.os, 'killpg', lambda *args: signals.append(args))
        assert managed_process.stop(tmp_path, str(process.pid), timeout=timeout)[0] is expected
        assert not signals
        monkeypatch.setattr(managed_process.os, 'killpg', real_killpg)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            real_killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def test_revoke_before_delayed_launch_prevents_native_execution(tmp_path):
    output = tmp_path / "ran"
    native = executable(tmp_path / "agent", f"open({str(output)!r},'w').write('bad')\n")
    command = integrations.prepare_command(CliLaunchSpec(command=(str(native),)), tmp_path, "terminal")
    assert managed_process.stop(tmp_path, None) == (True, None)
    result = subprocess.run(command, capture_output=True, timeout=5)
    assert result.returncode == 125 and not output.exists()


def test_guard_preserves_foreground_terminal_input(tmp_path):
    native = executable(tmp_path / "agent", "import os,sys\n"
        "assert os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()\n"
        "print('READY', flush=True)\nprint('RECEIVED:' + input(), flush=True)\n")
    command = integrations.prepare_command(CliLaunchSpec(command=(str(native),)), tmp_path, "terminal")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("/bin/sh", ["/bin/sh", "-c", shlex.join(command) + "; exit $?"])
    received = b""
    try:
        deadline = time.monotonic() + 5
        while b"READY" not in received and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                received += os.read(fd, 4096)
        assert b"READY" in received
        os.write(fd, b"test-input\n")
        while b"RECEIVED:test-input" not in received and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                received += os.read(fd, 4096)
        assert b"RECEIVED:test-input" in received
    finally:
        managed_process.stop(tmp_path, str(pid), timeout=0.1)
        os.close(fd)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
