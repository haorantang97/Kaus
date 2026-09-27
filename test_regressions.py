#!/usr/bin/env python3.11
import asyncio
import json
import os
import pty
import select
import sqlite3
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class DashboardRegressionTests(unittest.TestCase):
    def test_terminal_response_is_delivered_without_echo(self):
        master_fd, slave_fd = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] |= termios.ECHO
            attrs[3] &= ~termios.ICANON
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

            runtime = object.__new__(server._PtyRuntime)
            runtime.closed = False
            runtime.master_fd = master_fd
            runtime.write_lock = threading.Lock()

            response = "\x1b]11;rgb:1616/1818/0f0f\x1b\\"
            self.assertTrue(runtime.write(response, suppress_echo=True))
            self.assertEqual(os.read(slave_fd, len(response)), response.encode())
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            self.assertEqual(ready, [])
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_pty_write_retries_partial_nonblocking_writes(self):
        runtime = object.__new__(server._PtyRuntime)
        runtime.closed = False
        runtime.master_fd = 123
        runtime.write_lock = threading.Lock()
        chunks: list[bytes] = []

        def partial_write(_fd, data):
            raw = bytes(data)
            n = min(7, len(raw))
            chunks.append(raw[:n])
            return n

        payload = "长粘贴-" + ("abcdefg" * 20)
        with patch.object(server.os, "write", side_effect=partial_write):
            self.assertTrue(runtime.write(payload))
        self.assertEqual(b"".join(chunks), payload.encode())

    def test_revert_requires_profile_confirmation(self):
        client = TestClient(server.app)
        config_path = server._profile_dir("default") / "config.yaml"
        before = config_path.read_bytes()
        response = client.post("/api/revert-config", json={"child": "default"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config_path.read_bytes(), before)

    def test_revert_restores_valid_mapping(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "profiles").mkdir()
            (home / "config.yaml").write_text("model: current\n", encoding="utf-8")
            (home / "config.yaml.dashbak").write_text("model: restored\n", encoding="utf-8")
            with patch.object(server, "HERMES_HOME", home), patch.object(server, "PROFILES_DIR", home / "profiles"):
                response = client.post(
                    "/api/revert-config",
                    json={"child": "default", "confirm": "default"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(server._read_config("default"), {"model": "restored"})
                self.assertFalse((home / "config.yaml.dashbak").exists())

    def test_update_health_has_explicit_post_route(self):
        methods = {
            method
            for route in server.app.routes
            if getattr(route, "path", None) == "/api/update-health/run"
            for method in (getattr(route, "methods", None) or set())
        }
        self.assertIn("POST", methods)

    def test_dashboard_event_scopes_classify_common_changes(self):
        before = {
            "state": {"hierarchy.json": ["file", 1, 1], "skill-blocklist.json": ["file", 1, 1]},
            "root_config": {},
            "profiles": {},
            "profile_files": {},
            "memories": {},
            "skills": {},
        }
        network_after = {**before, "state": {**before["state"], "hierarchy.json": ["file", 2, 1]}}
        network_event = server._dashboard_event_from_signatures(before, network_after)
        self.assertIn("network", network_event["scopes"])
        self.assertIn("profiles", network_event["scopes"])

        skills_after = {**before, "skills": {"coding": {"gstack/SKILL.md": ["file", 2, 10]}}}
        skills_event = server._dashboard_event_from_signatures(before, skills_after)
        self.assertIn("skills", skills_event["scopes"])
        self.assertIn("warehouse", skills_event["scopes"])

    def test_state_version_route_is_available(self):
        client = TestClient(server.app)
        response = client.get("/api/state-version")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertRegex(body["version"], r"^[0-9a-f]{20}$")

    def test_model_levers_soft_inherit_from_parent_without_duplicate_options(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            child = profiles / "child"
            profiles.mkdir()
            child.mkdir()
            (root / "config.yaml").write_text(
                "model:\n  default: gpt-5.5\n  provider: openai-codex\n",
                encoding="utf-8",
            )
            (child / "config.yaml").write_text("", encoding="utf-8")
            hierarchy = root / "hierarchy.json"
            hierarchy.write_text(json.dumps({"child": "default"}), encoding="utf-8")
            model_options = root / "model-options.json"
            model_options.write_text(
                json.dumps([
                    {"default": "gpt-5.5", "provider": "openai-codex"},
                    {"default": "gpt-5.5", "provider": "openai-codex"},
                    {"default": "deepseek-v4-flash", "provider": "deepseek"},
                ]),
                encoding="utf-8",
            )

            with (
                patch.object(server, "HERMES_HOME", root),
                patch.object(server, "PROFILES_DIR", profiles),
                patch.object(server, "HIERARCHY_FILE", hierarchy),
                patch.object(server, "MODEL_OPTIONS_FILE", model_options),
            ):
                inherited = client.get("/api/agent/child/levers")
                self.assertEqual(inherited.status_code, 200)
                body = inherited.json()
                self.assertEqual(body["model"]["default"], "gpt-5.5")
                self.assertEqual(body["model"]["provider"], "openai-codex")
                self.assertFalse(body["model_own"])
                self.assertEqual([m["default"] for m in body["known_models"]], ["gpt-5.5", "deepseek-v4-flash"])

                changed = client.post("/api/agent/child/levers", json={"model": "deepseek-v4-flash"})
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(changed.json()["effective_model"]["default"], "deepseek-v4-flash")
                self.assertEqual(changed.json()["effective_model"]["provider"], "deepseek")
                self.assertEqual(server._read_config("child")["model"]["default"], "deepseek-v4-flash")
                self.assertEqual(server._read_config("child")["model"]["provider"], "deepseek")

                reset = client.post("/api/agent/child/levers", json={"model": "__inherit__"})
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(reset.json()["effective_model"]["default"], "gpt-5.5")
                self.assertEqual(reset.json()["effective_model"]["provider"], "openai-codex")
                self.assertNotIn("model", server._read_config("child"))

    def test_approvals_lever_uses_three_real_local_modes(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            child = profiles / "child"
            profiles.mkdir()
            child.mkdir()
            (root / "config.yaml").write_text("", encoding="utf-8")
            (child / "config.yaml").write_text("", encoding="utf-8")
            hierarchy = root / "hierarchy.json"
            hierarchy.write_text(json.dumps({"child": "default"}), encoding="utf-8")

            with (
                patch.object(server, "HERMES_HOME", root),
                patch.object(server, "PROFILES_DIR", profiles),
                patch.object(server, "HIERARCHY_FILE", hierarchy),
                patch.object(server, "MODEL_OPTIONS_FILE", root / "model-options.json"),
                patch.object(server, "_has_resumable_session", return_value=False),
            ):
                initial = client.get("/api/agent/child/levers")
                self.assertEqual(initial.status_code, 200)
                self.assertEqual(initial.json()["approvals_mode"], "manual")
                self.assertFalse(initial.json()["approvals_own"])

                smart = client.post("/api/agent/child/levers", json={"approvals_mode": "smart"})
                self.assertEqual(smart.status_code, 200)
                self.assertEqual(server._read_config("child")["approvals"]["mode"], "smart")
                self.assertNotIn("--yolo", server._chat_argv("child", fresh=True))

                off = client.post("/api/agent/child/levers", json={"approvals_mode": "off"})
                self.assertEqual(off.status_code, 200)
                self.assertEqual(server._read_config("child")["approvals"]["mode"], "off")
                # Do not freeze process-level YOLO in argv; Hermes hot-reads config.yaml
                # so switching back to manual/smart does not require restarting the PTY.
                self.assertNotIn("--yolo", server._chat_argv("child", fresh=True))

                manual = client.post("/api/agent/child/levers", json={"approvals_mode": "manual"})
                self.assertEqual(manual.status_code, 200)
                self.assertNotIn("approvals", server._read_config("child"))
                self.assertEqual(client.get("/api/agent/child/levers").json()["approvals_mode"], "manual")

    def test_pty_runtime_id_must_belong_to_profile(self):
        self.assertTrue(server._pty_runtime_id_matches_profile("web-automation:abc", "web-automation"))
        self.assertTrue(server._pty_runtime_id_matches_profile("default:continue", "default"))
        self.assertFalse(server._pty_runtime_id_matches_profile("memoris:abc", "web-automation"))
        self.assertFalse(server._pty_runtime_id_matches_profile("web-automation:abc", "web"))

    def test_default_chat_argv_uses_explicit_profile_namespace(self):
        with patch.object(server, "_has_resumable_session", return_value=False):
            argv = server._chat_argv("default", resume_sid="20260611_131228_3ddd03")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "default")
        self.assertIn("--resume", argv)

    def test_kanban_create_preserves_board_and_worker_options(self):
        client = TestClient(server.app)
        calls: list[list[str]] = []

        def fake_run_hermes(args, timeout=30, **_kwargs):
            calls.append(args)
            return 0, json.dumps({"id": "t_test1234", "status": "triage"}), ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = root / "profiles"
            worker = profiles / "web-auto-screening"
            worker.mkdir(parents=True)
            with (
                patch.object(server, "HERMES_HOME", root),
                patch.object(server, "PROFILES_DIR", profiles),
                patch.object(server, "run_hermes", side_effect=fake_run_hermes),
            ):
                response = client.post(
                    "/api/kanban/create?board=web-auto",
                    json={
                        "title": "Stage 1 screening",
                        "assignee": "web-auto-screening",
                        "tenant": "bolton",
                        "workspace_kind": "dir",
                        "workspace_path": "/Users/example/workspace",
                        "idempotency_key": "run-1:stage-1",
                        "max_runtime": "2h",
                        "goal_mode": True,
                        "triage": True,
                    },
                )
        self.assertEqual(response.status_code, 200)
        argv = calls[0]
        self.assertEqual(argv[:3], ["kanban", "--board", "web-auto"])
        self.assertIn("--tenant", argv)
        self.assertIn("bolton", argv)
        self.assertIn("--workspace", argv)
        self.assertIn("dir:/Users/example/workspace", argv)
        self.assertIn("--idempotency-key", argv)
        self.assertIn("run-1:stage-1", argv)
        self.assertIn("--max-runtime", argv)
        self.assertIn("2h", argv)
        self.assertIn("--goal", argv)

    def test_kanban_gateway_status_does_not_treat_other_profile_as_gateway(self):
        client = TestClient(server.app)
        output = "\n".join([
            "✗ Gateway is not running",
            "",
            "Other profiles:",
            "  ✓ default          — PID 39616",
        ])
        with patch.object(server, "run_hermes", return_value=(0, output, "")):
            response = client.get("/api/kanban/gateway")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["running"])

    def test_terminal_lab_ws_is_thin_pty_bridge(self):
        client = TestClient(server.app)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as script:
            script.write(
                "import fcntl, struct, sys, termios\n"
                "sys.stdout.buffer.write(b'READY\\n')\n"
                "sys.stdout.buffer.flush()\n"
                "data = sys.stdin.buffer.readline()\n"
                "rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b'\\0' * 8))\n"
                "sys.stdout.buffer.write(b'ECHO:' + data)\n"
                "sys.stdout.buffer.write(f'SIZE:{rows}x{cols}\\n'.encode())\n"
                "sys.stdout.buffer.flush()\n"
            )
            script_path = Path(script.name)
        try:
            with (
                patch.dict(os.environ, {"HERMES_CHAT_CMD": f"/opt/homebrew/bin/python3.11 {script_path}"}),
                patch.object(server, "build_profiles", return_value=([{"name": "default"}], "test", None)),
                patch.object(server, "effective_killed", return_value=False),
            ):
                with client.websocket_connect("/ws/terminal-lab/default?new=1&rows=24&cols=80") as ws:
                    ws.receive_bytes()
                    ws.send_bytes(b"\x1b[RESIZE:12;34]")
                    ws.send_text("terminal-lab-echo 中文\n")
                    received = b""
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and b"SIZE:34x12" not in received:
                        try:
                            received += ws.receive_bytes()
                        except Exception:
                            break
                    self.assertIn("ECHO:terminal-lab-echo 中文".encode(), received)
                    self.assertIn(b"SIZE:34x12", received)
        finally:
            script_path.unlink(missing_ok=True)

    def test_terminal_lab_ws_streams_long_output_without_runtime_replay(self):
        client = TestClient(server.app)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as script:
            script.write(
                "import sys\n"
                "for i in range(1200):\n"
                "    sys.stdout.write(f'line-{i:04d} 中文 long-output-check\\\\n')\n"
                "sys.stdout.flush()\n"
            )
            script_path = Path(script.name)
        try:
            with (
                patch.dict(os.environ, {"HERMES_CHAT_CMD": f"/opt/homebrew/bin/python3.11 {script_path}"}),
                patch.object(server, "build_profiles", return_value=([{"name": "default"}], "test", None)),
                patch.object(server, "effective_killed", return_value=False),
            ):
                with client.websocket_connect("/ws/terminal-lab/default?new=1&rows=24&cols=80") as ws:
                    received = b""
                    deadline = time.monotonic() + 5
                    last_line = "line-1199 中文 long-output-check".encode()
                    while time.monotonic() < deadline and last_line not in received:
                        received += ws.receive_bytes()
                    self.assertIn(b"line-0000", received)
                    self.assertIn(last_line, received)
        finally:
            script_path.unlink(missing_ok=True)

    def test_terminal_lab_ws_close_terminates_child_process(self):
        client = TestClient(server.app)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as script:
            pid_path = Path(script.name + ".pid")
            script.write(
                "import os, pathlib, sys, time\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
                "sys.stdout.write('READY\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n"
            )
            script_path = Path(script.name)
        try:
            with (
                patch.dict(os.environ, {"HERMES_CHAT_CMD": f"/opt/homebrew/bin/python3.11 {script_path}"}),
                patch.object(server, "build_profiles", return_value=([{"name": "default"}], "test", None)),
                patch.object(server, "effective_killed", return_value=False),
            ):
                with client.websocket_connect("/ws/terminal-lab/default?new=1&rows=24&cols=80") as ws:
                    self.assertIn(b"READY", ws.receive_bytes())
                    pid = int(pid_path.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            script_path.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)

    def test_terminal_lab_v1_does_not_reuse_enhanced_runtime_stack(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        body = source.split('async def ws_terminal_lab(ws: WebSocket, name: str):', 1)[1].split('@app.get("/")', 1)[0]
        self.assertIn("pty.openpty()", body)
        self.assertIn("subprocess.Popen", body)
        self.assertNotIn("_get_or_start_pty_runtime", body)
        self.assertNotIn("_start_screen_pty_runtime", body)
        self.assertNotIn("_load_pty_replay", body)
        self.assertNotIn("runtime.attach", body)
        self.assertNotIn("terminal_response", body)

        terminal_lab = Path(server.HERE, "web/src/components/TerminalLab.tsx").read_text(encoding="utf-8")
        self.assertIn("/ws/terminal-lab/", terminal_lab)
        self.assertNotIn("ChatTerminal", terminal_lab)
        self.assertNotIn("ChatWindows", terminal_lab)
        self.assertNotIn("/api/pty/runtime", terminal_lab)
        self.assertNotIn("URL_RE", terminal_lab)

    def test_pty_file_preview_allows_temp_text_and_blocks_dashboard_files(self):
        client = TestClient(server.app)
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir="/tmp", delete=False, encoding="utf-8") as tmp:
            tmp.write('{"ok": true, "msg": "hello"}')
            tmp_path = Path(tmp.name)
        try:
            allowed = client.get("/api/pty/file", params={"path": str(tmp_path)})
            self.assertEqual(allowed.status_code, 200)
            self.assertIn("text/html", allowed.headers.get("content-type", ""))
            self.assertIn("&quot;msg&quot;: &quot;hello&quot;", allowed.text)
        finally:
            tmp_path.unlink(missing_ok=True)

        forbidden = client.get("/api/pty/file", params={"path": str(Path(server.HERE) / "hierarchy.json")})
        self.assertEqual(forbidden.status_code, 403)

    def test_pty_upload_writes_to_outputs_and_is_previewable(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "outputs"
            uploads = outputs / "uploads"
            with patch.object(server, "PTY_OUTPUT_DIR", outputs), patch.object(server, "PTY_UPLOAD_DIR", uploads):
                response = client.post(
                    "/api/pty/upload",
                    data={"runtime_id": "default:test-upload"},
                    files={"file": ("note.txt", b"hello upload", "text/plain")},
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                uploaded = Path(body["path"])
                self.assertTrue(uploaded.is_file())
                self.assertEqual(uploaded.read_text(encoding="utf-8"), "hello upload")
                self.assertTrue(body["previewable"])
                self.assertTrue(uploaded.resolve().is_relative_to(uploads.resolve()))

    def test_pty_upload_accepts_common_media_without_previewing_it(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "outputs"
            uploads = outputs / "uploads"
            with patch.object(server, "PTY_OUTPUT_DIR", outputs), patch.object(server, "PTY_UPLOAD_DIR", uploads):
                response = client.post(
                    "/api/pty/upload",
                    data={"runtime_id": "web-automation:test-upload"},
                    files={"file": ("clip.mov", b"fake mov bytes", "video/quicktime")},
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                uploaded = Path(body["path"])
                self.assertTrue(uploaded.is_file())
                self.assertEqual(uploaded.suffix, ".mov")
                self.assertFalse(body["previewable"])
                self.assertTrue(uploaded.resolve().is_relative_to(uploads.resolve()))

    def test_pty_replay_drops_dynamic_hermes_status_frames(self):
        raw = (
            b"before\r\n"
            b"\x1b[48;5;235m \xe2\x9a\x95 gpt-5.5 \xe2\x94\x82 46.9K/272K \xe2\x94\x82 "
            b"[\xe2\x96\x88\xe2\x96\x88\xe2\x96\x91\xe2\x96\x91] 17% \xe2\x94\x82 1h 13m \xe2\x94\x82 "
            b"\xe2\x8f\xb1 43s \x1b[0m\r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw)
        self.assertIn(b"before", clean)
        self.assertIn(b"after", clean)
        self.assertNotIn(b"gpt-5.5", clean)
        self.assertNotIn(b"46.9K/272K", clean)

    def test_pty_replay_neutralizes_screen_reset_controls(self):
        raw = (
            b"first answer\r\n"
            b"\x1b[2J\x1b[H"
            b"second answer\r\n"
            b"\x1b[1;1H"
            b"web-automation > pending"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        self.assertIn(b"first answer", clean)
        self.assertIn(b"second answer", clean)
        self.assertIn(b"web-automation > pending", clean)
        self.assertNotIn(b"\x1b[2J", clean)
        self.assertNotIn(b"\x1b[H", clean)
        self.assertNotIn(b"\x1b[1;1H", clean)
        self.assertIn(b"first answer\r\n\r\nsecond answer", clean)

    def test_pty_replay_drops_reflecting_progress_frames(self):
        sep = ("\u2500" * 80).encode()
        raw = (
            b"before\r\n"
            + sep
            + b"\r\n\xe2\x9a\x95 \xe2\x9d\xaf msg=interrupt \xc2\xb7 /queue \xc2\xb7 /bg \xc2\xb7 /steer \xc2\xb7 Ctrl+C cancel\r\n"
            + sep
            + b"\r\n\xe0\xb2\xa0_\xe0\xb2\xa0 reflecting...\r\n"
            + sep
            + b"\r\n\xe0\xb2\xa0_\xe0\xb2\xa0 reflecting...\r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("before", visible)
        self.assertIn("after", visible)
        self.assertNotIn("reflecting", visible)
        self.assertNotIn("msg=interrupt", visible)
        self.assertNotIn("\u2500" * 12, visible)

    def test_pty_replay_drops_mangled_progress_menu_frames(self):
        raw = (
            b"before\r\n"
            b"default \xe2\x9d\xaf errupt \xc2\xb7 /queue \xc2\xb7 /bg \xc2\xb7 /steer \xc2\xb7 Ctrl+C cancel\r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "default")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("before", visible)
        self.assertIn("after", visible)
        self.assertNotIn("/queue", visible)
        self.assertNotIn("/steer", visible)

    def test_pty_replay_collapses_prompt_only_redraw_rows(self):
        raw = (
            b"real output\r\n"
            b"web-automation >\r\n"
            b"\x1b[32mweb-automation >\x1b[0m\r\n"
            b"web-automation >\r\n"
            b"web-automation > run command\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        self.assertIn(b"real output", clean)
        self.assertIn(b"web-automation > run command", clean)
        self.assertEqual(clean.count(b"web-automation >"), 2)

    def test_pty_replay_collapses_bare_prompt_only_redraw_rows(self):
        raw = (
            b"real output\r\n"
            b"\xe2\x9d\xaf \r\n"
            b"\x1b[32m\xe2\x9d\xaf\x1b[0m\r\n"
            b"\xe2\x9d\xaf \r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("real output", visible)
        self.assertIn("after", visible)
        self.assertEqual(visible.count("❯"), 1)

    def test_pty_replay_collapses_inline_prompt_redraw_runs(self):
        raw = (
            b"before\r\n"
            b"web-automation >web-automation > web-automation >"
            b"\r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        self.assertIn(b"before", clean)
        self.assertIn(b"after", clean)
        self.assertEqual(clean.count(b"web-automation >"), 1)

    def test_pty_replay_collapses_prompt_frames_with_separators(self):
        sep = ("\u2500" * 80).encode()
        raw = (
            b"before\r\n"
            + sep + b"\r\n"
            + b"web-automation \xe2\x9d\xaf \r\n"
            + sep + b"\r\n"
            + b"web-automation > \r\n"
            + sep + b"\r\n"
            + b" web-automation \xe2\x9d\xaf \r\n"
            + b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("before", visible)
        self.assertIn("after", visible)
        self.assertEqual(visible.count("web-automation ❯"), 1)
        self.assertNotIn("web-automation >", visible)
        self.assertNotIn("\u2500" * 12, visible)

    def test_pty_replay_collapses_prompt_frames_with_blank_control_rows(self):
        raw = (
            b"before\r\n"
            b"web-automation \xe2\x9d\xaf \r\n"
            b"\x1b[?25h\r\n"
            b" \r\n"
            b"web-automation > \r\n"
            b"\x1b[0m\r\n"
            b" web-automation \xe2\x9d\xaf \r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("before", visible)
        self.assertIn("after", visible)
        self.assertEqual(visible.count("web-automation ❯") + visible.count("web-automation >"), 1)

    def test_pty_replay_strips_repeated_prompt_prefixes_before_output_lines(self):
        raw = (
            b"before\r\n"
            b"web-automation \xe2\x9d\xaf    Stage 1\r\n"
            b"web-automation \xe2\x9d\xaf    /tmp/file.json\r\n"
            b"web-automation \xe2\x9d\xaf    done\r\n"
            b"web-automation > real command\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("Stage 1\r\n", visible)
        self.assertIn("/tmp/file.json\r\n", visible)
        self.assertIn("done\r\n", visible)
        self.assertIn("web-automation > real command\r\n", visible)
        self.assertNotIn("web-automation ❯    ", visible)

    def test_pty_replay_collapses_prompt_frames_with_cursor_split_separator(self):
        raw = (
            b"before\r\n"
            b"\x1b[0;38;5;230mweb-automation \xe2\x9d\xaf \x1b[0m\r\r\n"
            b"\x1b[64C\xe2\x94\x80\x1b[0m\r\r\n"
            b"\x1b[0m web-automation > \x1b[17D\x1b[17C\x1b[?25h\r\n"
            b"after\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        visible = server._TERMINAL_CONTROL_BYTES_RE.sub(b"", clean).decode("utf-8", "replace")
        self.assertIn("before", visible)
        self.assertIn("after", visible)
        self.assertEqual(visible.count("web-automation ❯") + visible.count("web-automation >"), 1)

    def test_live_pty_output_is_not_prompt_or_progress_filtered(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        pump_body = source.split("async def _pump", 1)[1].split("def _trim_buffer", 1)[0]
        self.assertNotIn("_filter_output_chunk", source)
        self.assertNotIn("_last_output_ended_with_prompt_frame", source)
        self.assertNotIn("_compact_prompt_only_replay_lines(data, self.name)", pump_body)
        self.assertNotIn("_is_prompt_redraw_frame(data, self.name)", pump_body)
        self.assertIn("data = _strip_pty_device_responses(data)", pump_body)
        self.assertIn("self._broadcast(data)", pump_body)

    def test_screen_carrier_session_names_are_stable_and_safe(self):
        session_name = server._screen_session_name("web-automation:20260612_110625_6c8f1a")
        self.assertRegex(session_name, r"^hermes_dash_[A-Za-z0-9_.-]+_[0-9a-f]{12}$")
        self.assertNotIn(":", session_name)
        self.assertLessEqual(len(session_name), 80)

    def test_detached_screen_runtime_is_discoverable_from_replay_cache(self):
        runtime_id = "web-automation:20260612_110625_6c8f1a"
        with tempfile.TemporaryDirectory() as tmp:
            replay_dir = Path(tmp)
            with (
                patch.object(server, "PTY_REPLAY_DIR", replay_dir),
                patch.object(server, "_PTY_RUNTIMES", {}),
                patch.object(server, "_screen_session_exists", return_value=True),
            ):
                server._persist_pty_replay_append(runtime_id, b"cached output\n")
                self.assertEqual(server._known_pty_replay_runtime_ids(), [runtime_id])
                live = server.active_pty_sessions_for_profile("web-automation")

        self.assertIn("20260612_110625_6c8f1a", live)
        self.assertEqual(live["20260612_110625_6c8f1a"]["runtime_id"], runtime_id)
        self.assertEqual(live["20260612_110625_6c8f1a"]["runtime_carrier"], "screen-detached")

    def test_backend_shutdown_detaches_persistent_pty_instead_of_stopping_it(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        shutdown_body = source.split('async def _shutdown_pty_runtimes():', 1)[1].split("def _shutdown_pty_runtimes_sync", 1)[0]
        shutdown_sync_body = source.split('def _shutdown_pty_runtimes_sync(reason: str = "process-exit")', 1)[1].split('@app.get("/api/health")', 1)[0]
        self.assertIn("runtime.detach_for_server_shutdown()", shutdown_body)
        self.assertNotIn("runtime.stop()", shutdown_body)
        self.assertIn("runtime.detach_for_server_shutdown_sync()", shutdown_sync_body)
        self.assertNotIn("runtime.stop_sync()", shutdown_sync_body)

    def test_screen_carrier_tracks_adopted_session_id(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        adopt_body = source.split('def api_adopt_pty_session(runtime_id: str, req: PtyAdoptSessionReq):', 1)[1].split("class PtyModelReq", 1)[0]
        self.assertIn("target_screen_session = _screen_session_name(target_id)", adopt_body)
        self.assertIn("_rename_screen_session(runtime.screen_session, target_screen_session, runtime.screen_bin)", adopt_body)
        self.assertIn("runtime.screen_session = target_screen_session", adopt_body)

    def test_explicit_pty_delete_quits_detached_screen_carrier(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        close_body = source.split("async def api_close_pty_runtime(runtime_id: str):", 1)[1].split('@app.post("/api/pty/runtime/{runtime_id}/clear-buffer")', 1)[0]
        delete_body = source.split("async def api_delete_session(name: str, sid: str):", 1)[1].split('@app.websocket("/ws/chat/{name}")', 1)[0]
        self.assertIn("_quit_screen_session(_screen_session_name(runtime_id))", close_body)
        self.assertIn("_quit_screen_session(_screen_session_name(runtime_id))", delete_body)

    def test_whiteboard_launcher_uses_open_windows_not_all_history(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        self.assertIn("const launcherSessions = sessions.filter", source)
        self.assertIn("openedSessionIds.includes(session.id)", source)
        self.assertIn("Boolean(session.runtime_recovered || session.runtime_buffer_bytes !== undefined)", source)
        launcher_filter = source.split("const launcherSessions = sessions.filter", 1)[1].split("const dims", 1)[0]
        self.assertNotIn("wins.some", launcher_filter)
        launcher_block = source.split("{/* launcher / font toolbar */}", 1)[1].split("{/* desktop */}", 1)[0]
        self.assertIn("{launcherSessions.map((s)", launcher_block)
        self.assertNotIn("{sessions.map((s)", launcher_block)
        self.assertIn("窗口", launcher_block)
        initial_window = source.split("const [wins, setWins] = useState<Win[]>(() => {", 1)[1].split("const focusedWin", 1)[0]
        self.assertIn("(openedSessionIds ? undefined : sessions[0])", initial_window)
        self.assertIn("if (!s && !runtimeKey) return [];", initial_window)
        self.assertNotIn("??\n        sessions[0]", initial_window)
        allowed_filter = source.split("const allowed = new Set(openedSessionIds);", 1)[1].split("});\n  }, [active, openedSessionIds, preferredSid]);", 1)[0]
        self.assertIn("item.sid ? allowed.has(item.sid) : Boolean(item.runtimeKey)", allowed_filter)

    def test_whiteboard_close_window_only_does_not_close_session_state(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        close_only = source.split("const closeWindowOnly = (win: Win) => {", 1)[1].split("const stopWindowRuntime", 1)[0]
        self.assertIn("dismissedPreferredRef.current.add(win.sid)", close_only)
        self.assertIn("onWindowClosed?.(win.sid)", close_only)
        self.assertNotIn("apiDelete", close_only)

    def test_whiteboard_new_session_adoption_updates_parent_state(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        adopt_block = source.split("const adoptBoardSession = async", 1)[1].split("const closeWindowOnly", 1)[0]
        self.assertIn("onSessionAdopted(sessionId)", adopt_block)
        self.assertNotIn("win.runtimeKey && win.runtimeKey === preferredDraftKey", adopt_block)

    def test_whiteboard_new_windows_use_unique_runtime_keys(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        self.assertIn("const newBoardRuntimeKey = (seq: number)", source)
        open_block = source.split("const open = (sid: string | null, title: string) => {", 1)[1].split("const runtimeId", 1)[0]
        self.assertIn("runtimeKey: sid ? undefined : newBoardRuntimeKey(n)", open_block)
        self.assertNotIn("runtimeKey: sid ? undefined : `board:${", open_block)

    def test_conversation_close_paths_do_not_reopen_history(self):
        source = (Path(server.HERE) / "web/src/components/Conversation.tsx").read_text(encoding="utf-8")
        self.assertIn("hermes-dashboard-conversation-prefs-terminal-faithful-v1", source)
        self.assertNotIn("const nextOpenedSessionId =", source)
        self.assertNotIn("const openedSavedSessionIds =", source)
        self.assertNotIn("const deleteBoardSessionInTabs", source)
        self.assertNotIn("opened?: string[]", source)
        self.assertNotIn("hidden?: string[]", source)
        self.assertIn("const chooseFallbackActive =", source)
        self.assertIn("sessions.find(isRuntimeSession)?.id", source)
        preferred_block = source.split("const preferredActive = (sessions: Session[], prefs: ConversationPrefs) => {", 1)[1].split("export function Conversation", 1)[0]
        self.assertIn("if (preferred && !isEndedSession(preferred)) return preferred.id;", preferred_block)
        self.assertIn("preferred?.id", preferred_block)
        preserve_block = source.split("const shouldPreserveActiveSession =", 1)[1].split("export function Conversation", 1)[0]
        self.assertIn("if (!isEndedSession(currentSession)) return true;", preserve_block)
        self.assertIn("return !sessions.some((item) => item.id !== current && (isRuntimeSession(item) || !isEndedSession(item)));", preserve_block)
        self.assertIn("shouldPreserveActiveSession(nextSessions, current, preserveActive)", source)
        close_new = source.split("const closeNewConversation = async () => {", 1)[1].split("const deleteConversation", 1)[0]
        self.assertIn("chooseFallbackActive(key)", close_new)
        self.assertIn("const closeSavedTerminal = async", source)
        self.assertIn("className=\"agent-session-delete-history\"", source)
        self.assertIn("删除历史", source)
        self.assertNotIn("sessionAction", source)
        self.assertNotIn("会话操作", source)
        close_saved = source.split("const closeSavedTerminal = async", 1)[1].split("const deleteConversation", 1)[0]
        self.assertIn("isRuntimeSession(session)", close_saved)
        self.assertIn("/api/pty/runtime/", close_saved)
        self.assertNotIn("/api/session/", close_saved)

    def test_frontend_terminal_first_replay_chunk_is_raw_and_conditional(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn('const CLEAR_TERMINAL_FOR_REPLAY = "\\x1b[H\\x1b[2J\\x1b[3J"', source)
        self.assertIn("const terminalChunk = chunk;", source)
        self.assertNotIn("function replayBottomPadding", source)
        self.assertIn("const shouldReplaySnapshot = !hasConnectedRuntime;", source)
        self.assertIn("const replayPrefix = isFirstChunk && shouldReplaySnapshot ? CLEAR_TERMINAL_FOR_REPLAY : \"\";", source)
        self.assertNotIn("function terminalReplayPrefix", source)
        self.assertNotIn("term.clear()", source)
        self.assertNotIn("const sanitizedChunk = isHistoricalReplay", source)

    def test_pty_reattach_replays_large_tail_without_full_runtime_buffer(self):
        self.assertLess(server._PTY_REPLAY_LIMIT, server._PTY_BUFFER_LIMIT)
        self.assertGreaterEqual(server._PTY_REPLAY_LIMIT, 4 * 1024 * 1024)
        self.assertLessEqual(server._PTY_REPLAY_LIMIT, 4 * 1024 * 1024)

    def test_frontend_terminal_recovers_stream_when_window_becomes_visible(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn("const recoverVisibleTerminal = () => {", source)
        self.assertIn('window.addEventListener("focus", recoverVisibleTerminal);', source)
        self.assertIn('document.addEventListener("visibilitychange", recoverOnVisibility);', source)
        recover_block = source.split("const recoverVisibleTerminal = () => {", 1)[1].split("const recoverOnVisibility", 1)[0]
        self.assertIn("if (!ws || ws.readyState !== WebSocket.OPEN)", recover_block)
        self.assertIn("reconnectRef.current();", recover_block)
        self.assertIn("scrollTerminalToBottom(connectionId);", recover_block)
        self.assertNotIn("probeRuntimeStatus", recover_block)
        self.assertNotIn("connect(hasConnectedRuntime, 0);", recover_block)

    def test_frontend_terminal_does_not_watchdog_reconnect_live_pty(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertNotIn("WS_RUNTIME_WATCHDOG_MS", source)
        self.assertNotIn("watchdogTimer", source)
        self.assertNotIn("WS_STALE_AFTER_INPUT_MS", source)

    def test_frontend_terminal_does_not_override_xterm_helper_textarea(self):
        css = (Path(server.HERE) / "web/src/theme-overrides.css").read_text(encoding="utf-8")
        self.assertNotIn(".chat-terminal-host .xterm-helper-textarea", css)

    def test_frontend_terminal_does_not_layout_contain_xterm(self):
        css = (Path(server.HERE) / "web/src/theme-overrides.css").read_text(encoding="utf-8")
        terminal_block = css.split(".agent-terminal-slot", 1)[1].split(".whiteboard-terminal-sleep", 1)[0]
        self.assertNotIn("contain: layout", terminal_block)

    def test_frontend_inactive_terminals_are_hidden_not_unmounted(self):
        css = (Path(server.HERE) / "web/src/theme-overrides.css").read_text(encoding="utf-8")
        self.assertIn(".agent-terminal-slot.is-hidden", css)
        self.assertIn("visibility: hidden;", css)
        self.assertIn("pointer-events: none;", css)
        self.assertIn(".whiteboard-terminal-pane.is-idle", css)

    def test_frontend_terminal_uses_large_native_scrollback(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn("const TERMINAL_SCROLLBACK_LINES = 80_000;", source)
        self.assertIn("scrollback: TERMINAL_SCROLLBACK_LINES", source)

    def test_frontend_terminal_flushes_writes_in_order_without_dropping_chunks(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertNotIn("TERMINAL_WRITE_QUEUE_LIMIT", source)
        self.assertNotIn("writeQueue = writeQueue.slice", source)
        self.assertNotIn("const scheduleTerminalFlush", source)
        self.assertIn("let terminalWriteInFlight = false;", source)
        self.assertIn("term.write(next, () =>", source)
        self.assertIn("if (writeQueue.length) flushTerminalWriteQueue(activeConnectionId);", source)

    def test_frontend_terminal_reconnect_skips_replay_when_screen_is_preserved(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        connect_body = source.split("const connect = (resume: boolean, attempt = 0) => {", 1)[1].split("ws.onopen = () => {", 1)[0]
        onmessage_body = source.split("ws.onmessage = (message) => {", 1)[1].split("ws.onclose = () => {", 1)[0]
        self.assertIn("const shouldReplaySnapshot = !hasConnectedRuntime;", connect_body)
        self.assertIn('if (!shouldReplaySnapshot) params.set("replay", "0");', connect_body)
        self.assertIn("isFirstChunk && shouldReplaySnapshot ? CLEAR_TERMINAL_FOR_REPLAY", onmessage_body)

    def test_frontend_hidden_terminal_keeps_websocket_stream_attached(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        inactive_body = source.split("if (!isActive) {", 1)[1].split("return;", 1)[0]
        self.assertIn("releaseResizeRef.current();", inactive_body)
        self.assertNotIn("pauseConnectionRef.current();", inactive_body)
        self.assertNotIn("pauseConnectionRef", source)
        self.assertNotIn("const pauseConnection", source)

    def test_frontend_terminal_follow_output_only_changes_on_user_scroll(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        disable_sequence = source.split("const DISABLE_MOUSE_TRACKING =", 1)[1].split(";", 1)[0]
        self.assertIn("?1007l", disable_sequence)
        self.assertIn("scrollSensitivity: 1", source)
        self.assertIn("fastScrollSensitivity: 5", source)
        self.assertIn("function isVisibleTerminalHost(host: HTMLElement)", source)
        self.assertIn("function isWheelGeneratedHistoryKey(data: string)", source)
        self.assertIn("const handleTerminalWheel = (event: WheelEvent) => {", source)
        self.assertIn("term.attachCustomWheelEventHandler(handleTerminalWheel);", source)
        self.assertLess(
            source.index("term.attachCustomWheelEventHandler(handleTerminalWheel);"),
            source.index("term.open(host);"),
        )
        self.assertNotIn('host.addEventListener("wheel", onWheel', source)
        self.assertNotIn('host.removeEventListener("wheel", onWheel', source)
        self.assertNotIn('document.addEventListener("wheel", onWheel', source)
        self.assertNotIn('document.removeEventListener("wheel", onWheel', source)
        self.assertIn('if (event.key === "PageUp")', source)
        self.assertNotIn("term.onScroll", source)
        self.assertNotIn("scrollDisposable", source)

    def test_frontend_pty_ws_bypasses_vite_proxy_in_dev(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn("function ptyWebSocketOrigin()", source)
        self.assertIn('location.port === "5174"', source)
        self.assertIn("127.0.0.1:8877", source)
        self.assertNotIn("`${proto}://${location.host}/ws/chat/", source)

    def test_backend_accepts_local_vite_origin_for_direct_pty_ws(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        origin_block = source.split("def _ws_origin_ok", 1)[1].split("_PTY_BUFFER_LIMIT", 1)[0]
        self.assertIn('"http://localhost:5174"', origin_block)
        self.assertIn('"http://127.0.0.1:5174"', origin_block)

    def test_vite_does_not_proxy_pty_websocket(self):
        source = Path(server.HERE, "web/vite.config.ts").read_text(encoding="utf-8")
        self.assertIn('host: "127.0.0.1"', source)
        self.assertIn('"/api": { target: BACKEND, changeOrigin: true }', source)
        self.assertNotIn('"/ws"', source)
        self.assertNotIn("ws: true", source)

    def test_frontend_pty_cols_keep_right_edge_gutter(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn("const TERMINAL_PTY_COL_GUTTER = 2;", source)
        self.assertIn("return Math.max(TERMINAL_MIN_COLS, cols - TERMINAL_PTY_COL_GUTTER);", source)

    def test_frontend_hidden_terminals_do_not_capture_wheel(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        wheel_body = source.split("const handleTerminalWheel = (event: WheelEvent) => {", 1)[1].split("};\n    term.loadAddon", 1)[0]
        self.assertIn("if (!isActiveRef.current && !isVisibleTerminalHost(host)) return true;", wheel_body)
        self.assertNotIn("isPointerInsideElement", source)
        self.assertIn("suppressWheelHistoryInputUntil = performance.now() + 220;", wheel_body)
        self.assertIn("if (event.deltaY < 0) followOutputRef.current = false;", wheel_body)
        self.assertIn("requestAnimationFrame(() =>", wheel_body)
        self.assertIn("return true;", wheel_body)
        self.assertNotIn("term.scrollLines", wheel_body)
        self.assertNotIn("event.preventDefault();", wheel_body)
        self.assertNotIn("event.stopPropagation();", wheel_body)
        self.assertNotIn("event.stopImmediatePropagation();", wheel_body)
        self.assertNotIn("return false;", wheel_body)

    def test_frontend_terminal_never_turns_wheel_into_history_input(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        input_body = source.split("const sendTerminalInput = (data: string) => {", 1)[1].split("const isDeviceResponse", 1)[0]
        self.assertIn("performance.now() < suppressWheelHistoryInputUntil", input_body)
        self.assertIn("isWheelGeneratedHistoryKey(data)", input_body)
        self.assertIn("return;", input_body)

    def test_frontend_terminal_focus_does_not_override_parent_active_state(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        force_body = source.split("const forceTerminalFocus = () => {", 1)[1].split("host.addEventListener", 1)[0]
        focus_body = source.split("const focusTerminal = () => {", 1)[1].split("const eventLayer", 1)[0]
        self.assertIn("if (!isActiveRef.current) return;", force_body)
        self.assertIn("if (!isActiveRef.current) return;", focus_body)
        self.assertNotIn("isActiveRef.current = true;", force_body)
        self.assertNotIn("isActiveRef.current = true;", focus_body)

    def test_whiteboard_focus_syncs_window_before_terminal_focus(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        pointer_capture = source.split("onPointerDownCapture={(event) => {", 1)[1].split("onPointerDown={(event) => {", 1)[0]
        self.assertIn("focus(w.key, true);", pointer_capture)
        self.assertIn("forceTerminalFocus(event.target);", pointer_capture)

    def test_whiteboard_preserves_inactive_terminal_instances(self):
        source = (Path(server.HERE) / "web/src/components/ChatWindows.tsx").read_text(encoding="utf-8")
        render_block = source.split("{wins.map((w) => {", 1)[1].split("{windowAction &&", 1)[0]
        self.assertNotIn("{windowActive ? (", render_block)
        self.assertIn("<TerminalCore", render_block)
        self.assertIn('className={`whiteboard-terminal-pane ${windowActive ? "is-active" : "is-idle"}`}', render_block)
        self.assertIn("active={active && windowActive}", render_block)
        self.assertIn("voteResize={active && windowActive}", render_block)
        self.assertIn('fitKey={active && windowActive ? `${w.w}x${w.h}:${w.z}` : "off"}', render_block)
        self.assertNotIn("whiteboard-terminal-sleep", render_block)

    def test_terminal_focus_reconnects_wrong_transport_runtime(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        focus_body = source.split("const focusTerminal = () => {", 1)[1].split("const eventLayer", 1)[0]
        self.assertIn('connection === "disconnected" || transportRuntimeRef.current !== runtimeKey', focus_body)
        self.assertIn("reconnectRef.current();", focus_body)

    def test_frontend_terminal_does_not_track_prompt_redraw_state(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        onmessage_body = source.split("ws.onmessage = (message) => {", 1)[1].split("ws.onclose = () => {", 1)[0]
        self.assertNotIn("terminalChunkEndingKind", source)
        self.assertNotIn("lastRenderedEndedWithPromptFrame", source)
        self.assertNotIn("promptRedrawLineKind", source)
        self.assertNotIn("compactPromptRuns", source)
        self.assertNotIn("promptOnlyRedraw", onmessage_body)
        self.assertNotIn("return;", onmessage_body.split("const terminalChunk = chunk;", 1)[1].split("queueTerminalWrite(", 1)[0].replace("if (!terminalChunk) return;", ""))
        self.assertIn("queueTerminalWrite(", onmessage_body)

    def test_frontend_terminal_runtime_key_is_backend_runtime_id(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertIn("const runtimeKey = terminalId;", source)
        self.assertNotIn("const runtimeKey = `${name}:${terminalId}:${sid ?? \"\"}`;", source)
        self.assertIn("connectedRuntimeRef.current === runtimeKey", source)
        self.assertIn("transportRuntimeRef.current === runtimeKey", source)

    def test_frontend_does_not_filter_historical_or_live_pty_output(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        onmessage_body = source.split("ws.onmessage = (message) => {", 1)[1].split("ws.onclose = () => {", 1)[0]
        self.assertIn("const terminalChunk = chunk;", onmessage_body)
        self.assertNotIn("sanitizeHistoricalReplay(", onmessage_body)
        self.assertNotIn("compactPromptRuns(", onmessage_body)
        self.assertNotIn("stripLowContrastAnsiForeground(", onmessage_body)
        self.assertNotIn("dropHermesProgressReplayFrames(", onmessage_body)

    def test_frontend_does_not_watchdog_or_busy_text_reconnect_live_pty(self):
        source = (Path(server.HERE) / "web/src/components/ChatTerminal.tsx").read_text(encoding="utf-8")
        self.assertNotIn("const HERMES_BUSY_OUTPUT_RE", source)
        self.assertNotIn("WS_BUSY_GRACE_MS", source)
        self.assertNotIn("busyUntil", source)
        self.assertNotIn("watchdogTimer", source)

    def test_frontend_keeps_live_pty_sessions_discoverable_without_forcing_opened_prefs(self):
        api_source = (Path(server.HERE) / "web/src/lib/api.ts").read_text(encoding="utf-8")
        server_source = (Path(server.HERE) / "server.py").read_text(encoding="utf-8")
        conversation_source = (Path(server.HERE) / "web/src/components/Conversation.tsx").read_text(encoding="utf-8")
        api_sessions_body = server_source.split('def api_sessions(name: str):', 1)[1].split('@app.get("/api/skills/{name}")', 1)[0]
        self.assertIn('merged[sid]["ended_at"] = None', api_sessions_body)
        self.assertIn('"runtime_recovered": True', server_source)
        self.assertIn("return getJSON<SessionsResp>(`/api/sessions/${encodeURIComponent(name)}`);", api_source)
        self.assertNotIn("/api/pty/runtimes", api_source)
        self.assertNotIn("未索引后台 PTY", api_source)
        self.assertNotIn("opened?: string[]", conversation_source)
        self.assertNotIn("hidden?: string[]", conversation_source)
        self.assertNotIn("opened: openedSavedSessionIds(opened)", conversation_source)
        self.assertNotIn("visibleLiveRuntimeSessionIds", conversation_source)
        self.assertNotIn("mergeOpenSessionIds", conversation_source)
        self.assertNotIn("validOpenedSessions", conversation_source)
        self.assertIn("session.id === active || isRuntimeSession(session)", conversation_source)
        self.assertIn("sessions.find(isRuntimeSession)?.id", conversation_source)
        self.assertNotIn("if (prefSession && !hiddenItems.includes(prefSession.id)) return prefSession.id;", conversation_source)

    def test_conversation_preserves_inactive_tab_terminals(self):
        source = (Path(server.HERE) / "web/src/components/Conversation.tsx").read_text(encoding="utf-8")
        self.assertNotIn('mode === "board"', source)
        self.assertNotIn('mode === "tab"', source)
        self.assertNotIn("<ChatWindows", source)
        self.assertEqual(source.count("<TerminalCore"), 1)
        self.assertIn("const visibleTerminalKeys = useMemo", source)
        self.assertIn("visibleTerminalKeys.map((key) =>", source)
        self.assertIn('className={`agent-terminal-slot absolute ${terminalActive ? "is-active" : "is-hidden"}`}', source)
        self.assertIn("active={terminalActive}", source)
        self.assertIn('fitKey={terminalActive ? `on:${key}` : "off"}', source)
        self.assertNotIn('style={{ display: mode === "board" ? "block" : "none" }}', source)
        self.assertNotIn('style={{ display: mode === "tab" ? "contents" : "none" }}', source)

    def test_conversation_ended_history_requires_explicit_resume(self):
        source = (Path(server.HERE) / "web/src/components/Conversation.tsx").read_text(encoding="utf-8")
        visible_keys = source.split("const visibleTerminalKeys = useMemo", 1)[1].split("const persistActive", 1)[0]
        render_block = source.split('<div className="agent-terminal-stage">', 1)[1].split(": visibleTerminalKeys.length", 1)[0]
        self.assertIn("const [resumedHistoryIds", source)
        self.assertIn("const shouldMountTerminalForSession =", source)
        self.assertIn("const activeHistoryNeedsResume =", source)
        self.assertIn("resumedHistoryIds.has(session.id)", source)
        self.assertIn("setResumedHistoryIds(new Set());", source)
        self.assertIn("shouldMountTerminalForSession(session)", visible_keys)
        self.assertIn("shouldMountTerminalForSession(activeSession)", visible_keys)
        self.assertIn("agent-terminal-history-gate", render_block)
        self.assertIn("resumeHistorySession(activeSession)", render_block)
        self.assertIn("恢复终端继续", source)
        self.assertIn("只有点击「恢复终端继续」才会启动后台 PTY", source)
        self.assertNotIn("输入会通过 Hermes 原生 `--resume`", source)

    def test_backend_live_pty_overrides_ended_session_and_sorts_first(self):
        class Proc:
            pid = 12345

            @staticmethod
            def poll():
                return None

        runtime = object.__new__(server._PtyRuntime)
        runtime.id = "web-automation:20260611_165340_81d8ba"
        runtime.name = "web-automation"
        runtime.proc = Proc()
        runtime.closed = False
        runtime.buffer = bytearray(b"alive")
        runtime.created_at = time.time() - 120
        runtime.last_attach_at = time.time()
        runtime.last_detach_at = 0.0
        runtime.last_output_at = time.time() - 30
        runtime.subscribers = {object()}

        ended = {
            "id": "20260611_165340_81d8ba",
            "title": "Executor Role Initialization #5",
            "preview": "old",
            "last_active": "4h ago",
            "started_at": 1,
            "ended_at": 2,
            "message_count": 1,
            "source": "state_db",
        }
        newer = {
            "id": "20260611_205249_dc5cc8",
            "title": "Executor Role Initialization #6",
            "preview": "newer",
            "last_active": "1m ago",
            "started_at": 3,
            "ended_at": None,
            "message_count": 1,
            "source": "state_db",
        }

        client = TestClient(server.app)
        with (
            patch.object(server, "_profile_dir", return_value=Path(server.HERE)),
            patch.object(server, "run_hermes", return_value=(0, "", "")) as run_mock,
            patch.object(server, "sessions_from_state_db", return_value=[newer, ended]),
            patch.object(server, "_PTY_RUNTIMES", {runtime.id: runtime}),
            patch.object(server, "_known_pty_replay_runtime_ids", return_value=[]),
        ):
            response = client.get("/api/sessions/web-automation")

        self.assertEqual(response.status_code, 200)
        sessions = response.json()["sessions"]
        self.assertEqual(sessions[0]["id"], "20260611_165340_81d8ba")
        self.assertIsNone(sessions[0]["ended_at"])
        self.assertTrue(sessions[0]["runtime_recovered"])
        self.assertEqual(sessions[0]["source"], "state_db+runtime")
        run_mock.assert_not_called()

    def test_backend_ignores_temporary_pty_runtimes_in_session_index(self):
        class Proc:
            pid = 12345

            @staticmethod
            def poll():
                return None

        def runtime(runtime_id: str):
            item = object.__new__(server._PtyRuntime)
            item.id = runtime_id
            item.name = "web-automation"
            item.proc = Proc()
            item.closed = False
            item.buffer = bytearray(b"alive")
            item.created_at = time.time() - 60
            item.last_attach_at = time.time() - 30
            item.last_detach_at = 0.0
            item.last_output_at = time.time() - 10
            item.subscribers = set()
            return item

        runtimes = {
            "web-automation:new:7": runtime("web-automation:new:7"),
            "web-automation:board:abc:def:1": runtime("web-automation:board:abc:def:1"),
            "web-automation:20260611_165340_81d8ba": runtime("web-automation:20260611_165340_81d8ba"),
        }

        client = TestClient(server.app)
        with (
            patch.object(server, "_profile_dir", return_value=Path(server.HERE)),
            patch.object(server, "sessions_from_state_db", return_value=[]),
            patch.object(server, "_PTY_RUNTIMES", runtimes),
            patch.object(server, "_known_pty_replay_runtime_ids", return_value=[]),
        ):
            response = client.get("/api/sessions/web-automation")

        self.assertEqual(response.status_code, 200)
        sessions = response.json()["sessions"]
        self.assertEqual([session["id"] for session in sessions], ["20260611_165340_81d8ba"])
        self.assertNotIn("new:7", {session["id"] for session in sessions})
        self.assertNotIn("board:abc:def:1", {session["id"] for session in sessions})

    def test_agent_graph_action_controls_are_real_buttons(self):
        source = Path(server.HERE, "web/src/components/AgentGraph.tsx").read_text(encoding="utf-8")
        self.assertIn('<button type="button" className="ag-act p"', source)
        self.assertIn('<button type="button" className="ag-act g"', source)
        self.assertNotIn('<span className="ag-act p"', source)
        self.assertNotIn('<span className="ag-act g"', source)

    def test_delete_session_route_stops_matching_runtime_and_uses_threaded_cli(self):
        class Runtime:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True

        runtime = Runtime()
        client = TestClient(server.app)
        with (
            patch.object(server, "_PTY_RUNTIMES", {"web-automation:20260611_165340_81d8ba": runtime}),
            patch.object(server, "_clear_pty_replay") as clear_mock,
            patch.object(server, "run_hermes", return_value=(0, "", "")) as run_mock,
        ):
            response = client.delete("/api/session/web-automation/20260611_165340_81d8ba")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(runtime.stopped)
        run_mock.assert_called_once_with(["-p", "web-automation", "sessions", "delete", "20260611_165340_81d8ba", "--yes"])
        clear_mock.assert_called_once_with("web-automation:20260611_165340_81d8ba")

    def test_delete_session_route_is_async_and_closes_runtime_before_cli_delete(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        body = source.split('async def api_delete_session(name: str, sid: str):', 1)[1].split('@app.websocket("/ws/chat/{name}")', 1)[0]
        self.assertIn('runtime_id = f"{name}:{sid}"', body)
        self.assertIn("await runtime.stop()", body)
        self.assertIn("await asyncio.to_thread(run_hermes, args)", body)
        self.assertIn("_clear_pty_replay(runtime_id)", body)

    def test_session_reads_do_not_call_hermes_sessions_list(self):
        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        api_sessions_body = source.split('def api_sessions(name: str):', 1)[1].split('@app.get("/api/skills/{name}")', 1)[0]
        api_profile_body = source.split('def api_profile(name: str):', 1)[1].split("# --------------------------------------------------------------------------- #\n# Profile 面板扩展", 1)[0]
        has_resumable_body = source.split('def _has_resumable_session(name: str) -> bool:', 1)[1].split('def _chat_argv', 1)[0]
        self.assertNotIn('["sessions", "list"', api_sessions_body)
        self.assertNotIn('["sessions", "list"', api_profile_body)
        self.assertNotIn('["sessions", "list"', has_resumable_body)
        self.assertNotIn("run_hermes(", api_sessions_body)
        self.assertNotIn("run_hermes(", api_profile_body)
        self.assertNotIn("run_hermes(", has_resumable_body)
        self.assertIn("sessions_from_state_db(name, limit=50)", api_sessions_body)
        self.assertIn("sessions_from_state_db(name, limit=50)", api_profile_body)
        self.assertIn("_state_db_has_resumable_session(name)", has_resumable_body)

    def test_state_db_sessions_hide_empty_ghost_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "web-automation"
            profile_dir.mkdir()
            db = profile_dir / "state.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE sessions (id TEXT, title TEXT, started_at REAL, ended_at REAL, message_count INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL, role TEXT, content TEXT)"
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    ("20260611_221247_186d5d", "Executor Role Initialization #7", 1, 2, 0),
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    ("20260611_221356_630a58", "Executor Role Initialization #8", 3, None, 1),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                    ("20260611_221356_630a58", 4, "user", "真实对话"),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(server, "_profile_dir", return_value=profile_dir):
                sessions = server.sessions_from_state_db("web-automation", limit=50)

        self.assertEqual([session["id"] for session in sessions], ["20260611_221356_630a58"])

    def test_session_archive_manifest_hides_segments_and_overrides_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "web-automation"
            profile_dir.mkdir()
            db = profile_dir / "state.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE sessions (id TEXT, title TEXT, started_at REAL, ended_at REAL, message_count INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL, role TEXT, content TEXT)"
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    ("20260611_000000_parent", "Executor Role Initialization #1", 1, 2, 1),
                )
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    ("20260611_000001_head", "Executor Role Initialization #2", 3, None, 1),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                    ("20260611_000000_parent", 2, "assistant", "old"),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                    ("20260611_000001_head", 4, "assistant", "new"),
                )
                conn.commit()
            finally:
                conn.close()

            archive = {
                "canonical_titles": {"20260611_000001_head": "Executor Role Initialization"},
                "hidden_session_ids": ["20260611_000000_parent"],
            }
            with (
                patch.object(server, "_profile_dir", return_value=profile_dir),
                patch.object(server, "_session_archive_for_profile", return_value=archive),
            ):
                sessions = server.sessions_from_state_db("web-automation", limit=50)

        self.assertEqual([session["id"] for session in sessions], ["20260611_000001_head"])
        self.assertEqual(sessions[0]["title"], "Executor Role Initialization")

    def test_archived_live_runtime_is_not_reintroduced_to_session_index(self):
        class Proc:
            pid = 12345

            @staticmethod
            def poll():
                return None

        def runtime(runtime_id: str):
            item = object.__new__(server._PtyRuntime)
            item.id = runtime_id
            item.name = "web-automation"
            item.proc = Proc()
            item.closed = False
            item.buffer = bytearray(b"alive")
            item.created_at = time.time() - 60
            item.last_attach_at = time.time() - 30
            item.last_detach_at = 0.0
            item.last_output_at = time.time() - 10
            item.subscribers = set()
            return item

        runtimes = {
            "web-automation:20260611_000000_parent": runtime("web-automation:20260611_000000_parent"),
            "web-automation:20260611_000001_head": runtime("web-automation:20260611_000001_head"),
        }

        client = TestClient(server.app)
        with (
            patch.object(server, "_profile_dir", return_value=Path(server.HERE)),
            patch.object(server, "sessions_from_state_db", return_value=[]),
            patch.object(server, "_session_archive_hidden_ids", return_value={"20260611_000000_parent"}),
            patch.object(server, "_PTY_RUNTIMES", runtimes),
            patch.object(server, "_known_pty_replay_runtime_ids", return_value=[]),
        ):
            response = client.get("/api/sessions/web-automation")

        self.assertEqual(response.status_code, 200)
        sessions = response.json()["sessions"]
        self.assertEqual([session["id"] for session in sessions], ["20260611_000001_head"])

    def test_pty_replay_tail_limits_large_browser_reattach(self):
        raw = (b"old line\n" * 200_000) + b"recent prompt\n"
        clean = server._tail_pty_replay(raw, limit=4096)
        self.assertGreater(len(raw), 1_000_000)
        self.assertLess(len(clean), 5000)
        self.assertIn(b"replay truncated", clean)
        self.assertIn(b"recent prompt", clean)

    def test_pty_replay_cache_persists_and_clears_by_runtime_id(self):
        runtime_id = "web-automation:test-persist-cache"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "PTY_REPLAY_DIR", Path(tmp)):
                server._persist_pty_replay_append(runtime_id, b"first line\n")
                server._persist_pty_replay_append(runtime_id, "第二行\n".encode())
                loaded = server._load_pty_replay(runtime_id)
                self.assertIn(b"first line", loaded)
                self.assertIn("第二行".encode(), loaded)
                self.assertIn("self.buffer = bytearray(_load_pty_replay(runtime_id))", Path(server.HERE, "server.py").read_text(encoding="utf-8"))
                server._clear_pty_replay(runtime_id)
                self.assertEqual(server._load_pty_replay(runtime_id), b"")

    def test_pty_runtime_attach_can_skip_replay_snapshot(self):
        runtime = object.__new__(server._PtyRuntime)
        runtime.subscribers = set()
        runtime.last_attach_at = 0.0
        runtime.buffer = bytearray(b"old output\r\n")
        queue, snapshot = runtime.attach(replay=False)
        try:
            self.assertEqual(snapshot, b"")
            self.assertIn(queue, runtime.subscribers)
        finally:
            runtime.subscribers.discard(queue)

    def test_pty_runtime_broadcast_does_not_drop_when_browser_is_slow(self):
        runtime = object.__new__(server._PtyRuntime)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        runtime.subscribers = {queue}
        for index in range(1200):
            server._PtyRuntime._broadcast(runtime, f"chunk-{index}\n".encode())
        self.assertEqual(queue.qsize(), 1200)
        self.assertEqual(queue.get_nowait(), b"chunk-0\n")

        source = Path(server.HERE, "server.py").read_text(encoding="utf-8")
        attach_body = source.split("def attach(self, *, replay: bool = True)", 1)[1].split("def detach", 1)[0]
        broadcast_body = source.split("def _broadcast(self, data: bytes | None)", 1)[1].split("def _finish", 1)[0]
        self.assertIn("asyncio.Queue()", attach_body)
        self.assertNotIn("asyncio.Queue(maxsize=", attach_body)
        self.assertNotIn("QueueFull", broadcast_body)
        self.assertNotIn("get_nowait()", broadcast_body)

    def test_pty_runtime_keeps_last_size_when_no_visible_votes(self):
        runtime = object.__new__(server._PtyRuntime)
        runtime.rows = 48
        runtime.cols = 180
        runtime._size_votes = {}

        def fail_resize(rows, cols):
            self.fail(f"hidden PTY should not resize to fallback, got {rows}x{cols}")

        runtime.resize = fail_resize
        server._PtyRuntime._apply_size_votes(runtime)
        self.assertEqual((runtime.rows, runtime.cols), (48, 180))

    def test_pty_trim_persists_newline_aligned_tail(self):
        runtime_id = "web-automation:test-trim-cache"
        rt = object.__new__(server._PtyRuntime)
        rt.id = runtime_id
        rt.buffer = bytearray(b"old line\n" + b"x" * (server._PTY_BUFFER_LIMIT + 128))
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "PTY_REPLAY_DIR", Path(tmp)):
                server._PtyRuntime._trim_buffer(rt)
                loaded = server._load_pty_replay(runtime_id)
                self.assertEqual(loaded, bytes(rt.buffer))
                self.assertNotIn(b"old line", loaded)
                self.assertLessEqual(len(loaded), server._PTY_BUFFER_LIMIT)

    def test_pty_replay_drops_stale_fullscreen_editor_after_prompt_returns(self):
        pending = "hermes子文件在哪里能帮我调出来吗".encode()
        raw = (
            b"normal history\r\n"
            b"\x1b[?1049h"
            b"UW PICO 5.09        File: /var/folders/tv/tmpfdu6rxku.md\r\n"
            b"^G Get Help          ^O WriteOut          ^R Read File\r\n"
            b"\x1b[?1049l\r\n"
            b"web-automation \xe2\x9d\xaf " + pending
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        self.assertNotIn(b"UW PICO", clean)
        self.assertNotIn(b"WriteOut", clean)
        self.assertTrue(clean.startswith(b"web-automation \xe2\x9d\xaf"))
        self.assertIn(pending, clean)

    def test_pty_replay_preserves_fullscreen_editor_without_returned_prompt(self):
        raw = (
            b"\x1b[?1049h"
            b"UW PICO 5.09        File: /var/folders/tv/tmpfdu6rxku.md\r\n"
            b"^G Get Help          ^O WriteOut          ^R Read File\r\n"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "web-automation")
        self.assertIn(b"UW PICO", clean)
        self.assertIn(b"WriteOut", clean)

    def test_pty_replay_drops_stale_editor_with_bare_prompt(self):
        raw = (
            b"\x1b[?1049h"
            b"UW PICO 5.09        File: /var/folders/tv/tmpfdu6rxku.md\r\n"
            b"^G Get Help          ^O WriteOut          ^R Read File\r\n"
            b"\x1b[?1049l\r\n"
            b"\xe2\x9d\xaf pending default input"
        )
        clean = server._sanitize_pty_replay_snapshot(raw, "default")
        self.assertNotIn(b"UW PICO", clean)
        self.assertTrue(clean.startswith(b"\xe2\x9d\xaf pending default input"))

    def test_chat_env_matches_official_dashboard_mouse_scroll_policy(self):
        env = server._chat_env()
        self.assertEqual(env.get("HERMES_TUI_DISABLE_MOUSE"), "1")
        self.assertEqual(env.get("HERMES_TUI_INLINE"), "1")

    def test_terminal_core_wheel_scroll_never_becomes_history_input(self):
        source = (Path(server.HERE) / "web/src/components/TerminalCore.tsx").read_text(encoding="utf-8")
        self.assertIn("DISABLE_MOUSE_TRACKING", source)
        self.assertIn("term.attachCustomWheelEventHandler", source)
        self.assertIn("term.scrollLines(rows)", source)
        self.assertIn("isWheelGeneratedHistoryKey(data)", source)
        self.assertIn("return;", source.split("const inputDisposable = term.onData((data) => {", 1)[1].split("const ws = wsRef.current;", 1)[0])

    def test_terminal_core_does_not_inject_control_bytes_into_pty_output_chunks(self):
        source = (Path(server.HERE) / "web/src/components/TerminalCore.tsx").read_text(encoding="utf-8")
        self.assertIn("queueWrite(chunk);", source)
        self.assertNotIn("queueWrite(chunk + DISABLE_MOUSE_TRACKING)", source)
        self.assertIn("SGR_MOUSE_RE.test(data)", source)

    def test_terminal_core_restores_drop_upload_without_attachment_button(self):
        source = (Path(server.HERE) / "web/src/components/TerminalCore.tsx").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/pty/upload"', source)
        self.assertIn('body.set("runtime_id", terminalId)', source)
        self.assertIn("onDrop={(event) =>", source)
        self.assertIn("sendInput(quoteTerminalPath(data.path))", source)
        self.assertIn("pty-upload-drop-hint", source)
        self.assertNotIn("<button", source)


if __name__ == "__main__":
    unittest.main()
