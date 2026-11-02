import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from drivers.base import CliLaunchSpec
from drivers.terminals import status_hook
from drivers.terminals.status_hooks import hook_arguments
from runtime.external_cli import TerminalLauncher
from runtime.terminal_status import TerminalStatusBoard

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CONV = "conversation:22222222-2222-4222-8222-222222222222"


def test_board_maps_events_and_trims_messages():
    board = TerminalStatusBoard(clock=lambda: T0)
    token = board.register("launch:1", "conversation:1")
    assert board.record("launch:1", token, "prompt")
    assert board.status("conversation:1").state == "working"
    assert board.record("launch:1", token, "notification", "  needs\\n your   permission " + "x" * 300)
    status = board.status("conversation:1")
    assert status.state == "waiting" and status.message.startswith("needs") and len(status.message) == 200
    assert board.record("launch:1", token, "stop", "ignored")
    assert board.status("conversation:1").message is None
    assert not board.record("launch:1", "wrong", "stop")
    assert not board.record("launch:2", token, "stop")
    board.release("conversation:1")
    assert board.status("conversation:1") is None and not board.record("launch:1", token, "stop")


def test_claude_hook_settings_only_report():
    flag, raw = hook_arguments("claude")
    settings = json.loads(raw)
    assert flag == "--settings" and set(settings) == {"hooks"}
    assert set(settings["hooks"]) == {"UserPromptSubmit", "Notification", "Stop"}
    assert "PermissionRequest" not in raw and "PreToolUse" not in raw
    command = settings["hooks"]["Notification"][0]["hooks"][0]["command"]
    assert command.endswith("status_hook.py notification")


def test_hook_script_posts_with_launch_token_and_stays_silent(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers[status_hook.TOKEN_HEADER],
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    env = {status_hook.URL_ENV: f"http://127.0.0.1:{server.server_port}/api/terminal-status/launch:1",
           status_hook.TOKEN_ENV: "launch-token", "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, status_hook.__file__, "notification"], env=env,
                            input='{"message": "Claude needs your permission", "session_id": "s"}',
                            capture_output=True, text=True, timeout=10)
    thread.join(timeout=10)
    server.server_close()
    assert result.returncode == 0 and result.stdout == ""
    assert received == [("/api/terminal-status/launch:1", "launch-token",
                         {"event": "notification", "message": "Claude needs your permission"})]
    unreachable = subprocess.run([sys.executable, status_hook.__file__, "stop"],
        env={**env, status_hook.URL_ENV: "http://127.0.0.1:9/x"}, input="{}", capture_output=True, text=True, timeout=10)
    assert unreachable.returncode == 0 and unreachable.stdout == ""


async def test_launcher_adds_hooks_and_token_only_with_a_board(tmp_path):
    board = TerminalStatusBoard()
    registered = []
    board.register = lambda launch_id, conversation_id: registered.append((launch_id, conversation_id)) or "launch-token"
    launcher = TerminalLauncher(root=tmp_path / "launches", opener=lambda app, path: (True, None),
                                pid_timeout=0.0, status_board=board, status_url="http://127.0.0.1:8877",
                                environment_timeout=0.1)
    spec = CliLaunchSpec(command=("claude", "--resume", "native"), status_hooks="claude")
    result = await launcher.launch(spec, conversation_id=CONV)
    script = result.script_path.read_text()
    assert "--settings" in script and "status_hook.py" in script
    assert result.launch.metadata["statusHooks"] == "on"
    # Nothing consumed the environment here, so the launch fails and the token is dropped.
    assert "launch-token" not in script and registered == [(result.launch.id, CONV)] and not board._launches
    plain = TerminalLauncher(root=tmp_path / "plain", opener=lambda app, path: (True, None), pid_timeout=0.0)
    result = await plain.launch(spec, conversation_id=CONV)
    assert "--settings" not in result.script_path.read_text() and "statusHooks" not in result.launch.metadata


async def test_terminal_app_wrapper_keeps_its_own_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.external_cli.terminal_wrapper", lambda spec, app: (Path("/x/cmux-claude-wrapper"), "KEY"))
    monkeypatch.setattr("runtime.external_cli.wrapper_keeps_caller_hooks", lambda spec, app, path=None: False)
    board = TerminalStatusBoard()
    launcher = TerminalLauncher(root=tmp_path, opener=lambda app, path: (True, None), pid_timeout=0.0,
                                status_board=board, status_url="http://127.0.0.1:8877")
    result = await launcher.launch(CliLaunchSpec(command=("claude",), status_hooks="claude"), conversation_id=CONV)
    assert result.launch.metadata["statusHooks"] == "terminal"
    assert not board._launches


async def test_merging_terminal_wrapper_still_gets_status_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.external_cli.terminal_wrapper", lambda spec, app: (Path("/x/cmux-claude-wrapper"), "KEY"))
    monkeypatch.setattr("runtime.external_cli.wrapper_keeps_caller_hooks", lambda spec, app, path=None: True)
    board = TerminalStatusBoard()
    board.register = lambda launch_id, conversation_id: "launch-token"
    launcher = TerminalLauncher(root=tmp_path, opener=lambda app, path: (True, None), pid_timeout=0.0,
                                status_board=board, status_url="http://127.0.0.1:8877", environment_timeout=0.1)
    result = await launcher.launch(CliLaunchSpec(command=("claude",), status_hooks="claude"), conversation_id=CONV)
    assert "--settings" in result.script_path.read_text()
    assert result.launch.metadata["statusHooks"] == "on"


def test_wrapper_merge_needs_its_merge_program(tmp_path, monkeypatch):
    from drivers.terminals import integrations
    monkeypatch.setattr(integrations, "terminal_wrapper", lambda spec, app: (Path("/x/wrapper"), "KEY"))
    spec = CliLaunchSpec(command=("claude",), terminal_integration="claude-code")
    assert not integrations.wrapper_keeps_caller_hooks(spec, "cmux", path=str(tmp_path))
    (tmp_path / "node").write_text("#!/bin/sh\n"); (tmp_path / "node").chmod(0o755)
    assert integrations.wrapper_keeps_caller_hooks(spec, "cmux", path=str(tmp_path))
    other = CliLaunchSpec(command=("codex",), terminal_integration="codex")
    assert not integrations.wrapper_keeps_caller_hooks(other, "cmux", path=str(tmp_path))


def test_waiting_callback_fires_once_per_transition_and_never_breaks_reports():
    seen = []
    board = TerminalStatusBoard(on_waiting=lambda status: seen.append(status.message))
    token = board.register("launch:1", CONV)
    assert board.record("launch:1", token, "notification", "first")
    assert board.record("launch:1", token, "notification", "again")
    assert board.record("launch:1", token, "prompt")
    assert board.record("launch:1", token, "notification", "second")
    assert seen == ["first", "second"]
    broken = TerminalStatusBoard(on_waiting=lambda status: 1 / 0)
    token = broken.register("launch:2", CONV)
    assert broken.record("launch:2", token, "notification")
    assert not broken.record("launch:2", "非ascii", "stop") and not broken.record("launch:2", token, ["stop"])


def test_notification_text_is_quoted_for_applescript(monkeypatch):
    from drivers.terminals import launcher as module
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kw: calls.append(args))
    module.macos_notify("Kaus", 'Run "rm" \\ now\nplease')
    assert calls == [["osascript", "-e", 'display notification "Run \\"rm\\" \\\\ now please" with title "Kaus"']]


async def test_focus_uses_the_launch_terminal_app(tmp_path):
    activated = []
    launcher = TerminalLauncher(root=tmp_path, opener=lambda app, path: (True, None), pid_timeout=0.0,
                                app="iterm2", activator=lambda app: activated.append(app) or True)
    result = await launcher.launch(CliLaunchSpec(command=("x",)), conversation_id=CONV)
    assert await launcher.focus(result.launch) is True and activated == ["iterm2"]


async def test_failed_launch_revokes_its_status_token(tmp_path):
    board = TerminalStatusBoard()
    launcher = TerminalLauncher(root=tmp_path, opener=lambda app, path: (False, "no terminal"), pid_timeout=0.0,
                                status_board=board, status_url="http://127.0.0.1:8877")
    result = await launcher.launch(CliLaunchSpec(command=("claude",), status_hooks="claude"), conversation_id=CONV)
    assert result.launched is False and not board._launches
