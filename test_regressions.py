#!/usr/bin/env python3.11
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class DashboardRegressionTests(unittest.TestCase):

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
        self.assertNotIn("warehouse", skills_event["scopes"])

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

    def test_agent_graph_action_controls_are_real_buttons(self):
        source = Path(server.HERE, "web/src/components/AgentGraph.tsx").read_text(encoding="utf-8")
        self.assertIn('<button type="button" className="ag-act p"', source)
        self.assertIn('<button type="button" className="ag-act g"', source)
        self.assertNotIn('<span className="ag-act p"', source)
        self.assertNotIn('<span className="ag-act g"', source)

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



if __name__ == "__main__":
    unittest.main()
