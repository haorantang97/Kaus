"""Opt-in native Crush verification against a local, deterministic model endpoint.

No real credentials, project or cloud model are used. The native binary
must be supplied explicitly; this script does not install or distribute it.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from drivers.crush_bridge.bridge import Bridge
from drivers.crush_bridge.protocol import Peer


class LocalModel(BaseHTTPRequestHandler):
    requests = []
    release_cancel = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        text = "Native Crush bridge smoke OK."
        if not body.get("stream"):
            data = json.dumps({"id": "test", "object": "chat.completion", "model": "kaus-test",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        messages = body.get("messages", [])
        user_index = max((index for index, message in enumerate(messages) if message.get("role") == "user"), default=-1)
        prompt = json.dumps(messages[user_index].get("content", "")) if user_index >= 0 else ""
        has_result = any(message.get("role") == "tool" for message in messages[user_index + 1:])
        tools = [tool.get("function", {}).get("name", "") for tool in body.get("tools", [])]
        if "WAIT_CANCEL" in prompt:
            chunk = {"id": "test", "object": "chat.completion.chunk", "model": "kaus-test",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Waiting"}, "finish_reason": None}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
            self.release_cancel.wait(timeout=5)
            return
        call = None
        if not has_result and tools:
            if "TRY_PERMISSION" in prompt:
                call = ("write", {"file_path": "must-not-exist.txt", "content": "denied"})
            elif "TRY_QUESTION" in prompt:
                call = ("question", {"questions": [{"type": "yes_no", "question": "Continue?", "description": "Native question smoke"}]})
            elif "TRY_MCP" in prompt:
                name = next((name for name in tools if "native_mcp_marker" in name), None)
                if name:
                    call = (name, {})
        if call:
            name, arguments = call
            chunks = [({"role": "assistant", "tool_calls": [{"index": 0, "id": "test-call", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}]}, None), ({}, "tool_calls")]
        else:
            chunks = [({"role": "assistant", "content": "Native Crush "}, None),
                      ({"content": "bridge smoke OK."}, None), ({}, "stop")]
        for delta, reason in chunks:
            chunk = {"id": "test", "object": "chat.completion.chunk", "model": "kaus-test",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


async def verify(binary: Path, root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    isolated, workspace = root / "isolated", root / "workspace"
    (isolated / "config").mkdir(parents=True, exist_ok=True)
    (isolated / "skills").mkdir(exist_ok=True)
    (isolated / "data").mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = {"id": "kaus-test", "name": "Kaus test", "context_window": 32768, "default_max_tokens": 2048,
             "can_reason": True, "reasoning_levels": ["low", "high"], "default_reasoning_effort": "low", "supports_images": True}
    config = {"providers": {"test": {"id": "test", "name": "Local test", "type": "openai-compat",
               "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "test-placeholder",
               "discover_models": False, "models": [model, {**model, "id": "kaus-test-2", "name": "Kaus test 2"}]}},
              "models": {"large": {"provider": "test", "model": "kaus-test"}, "small": {"provider": "test", "model": "kaus-test"}},
              "options": {"disable_default_providers": True, "disable_provider_auto_update": True}, "lsp": {}}
    (isolated / "config/crush.json").write_text(json.dumps(config))
    original = (isolated / "config/crush.json").read_bytes()
    # v0.96.1 ConfigStore.ImportCopilot returns before touching HOME if the
    # private global data contains a copilot API-key field. Keep the provider
    # disabled; this is only a defense-in-depth guard with a dummy value.
    (isolated / "data/crush.json").write_text(json.dumps({"providers": {
        "copilot": {"disable": True, "api_key": "disabled-local-test-placeholder"},
    }}))
    wrapper = root / "crush-isolated"
    # Preserve HOME verbatim. Crush's documented directory overrides isolate
    # configuration, provider data, cache and auto-discovered skills instead.
    child_env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    child_env.update({"CRUSH_DISABLE_METRICS": "true",
                 "CRUSH_DISABLE_PROVIDER_AUTO_UPDATE": "true", "CRUSH_DISABLE_DEFAULT_PROVIDERS": "true",
                 "CRUSH_GLOBAL_CONFIG": str(isolated / "config"), "CRUSH_GLOBAL_DATA": str(isolated / "data"),
                 "CRUSH_CACHE_DIR": str(isolated / "cache"), "CRUSH_SKILLS_DIR": str(isolated / "skills"),
                 "XDG_CONFIG_HOME": str(isolated / "xdg-config"), "XDG_DATA_HOME": str(isolated / "xdg-data"),
                 "XDG_CACHE_HOME": str(isolated / "xdg-cache"), "XDG_STATE_HOME": str(isolated / "xdg-state")})
    wrapper.write_text(f"#!{sys.executable}\nimport os,sys\nos.execve({str(binary)!r}, [{str(binary)!r}] + sys.argv[1:], {child_env!r})\n")
    wrapper.chmod(0o700)
    frames, interactions = [], []
    permission_action = "deny"
    started_cancel = asyncio.Event()

    def receive(frame):
        frames.append(frame)
        content = frame.get("params", {}).get("update", {}).get("content")
        if isinstance(content, dict) and content.get("text") == "Waiting":
            started_cancel.set()
        if frame.get("method") == "session/request_permission":
            assert not (workspace / "must-not-exist.txt").exists()
            interactions.append("permission:" + permission_action)
            peer.response({"id": frame["id"], "result": {"outcome": {"outcome": "selected", "optionId": permission_action}}})
        elif frame.get("method") == "elicitation/create":
            interactions.append("question")
            properties = frame["params"]["requestedSchema"]["properties"]
            peer.response({"id": frame["id"], "result": {"action": "accept", "content": {
                key: True if schema["type"] == "boolean" else "test" for key, schema in properties.items()
            }}})

    peer = Peer(receive)
    bridge = Bridge(str(wrapper), root / "sessions", peer, turn_timeout=30)
    report = {"native_binary": str(binary), "model": "local deterministic HTTP endpoint", "credentials": "isolated placeholder",
              "isolation": "CRUSH_GLOBAL_CONFIG/CRUSH_GLOBAL_DATA/CRUSH_CACHE_DIR/CRUSH_SKILLS_DIR and XDG paths; HOME unchanged"}
    mcp_audit = root / "mcp-audit.jsonl"
    mcp_audit.write_text("")
    test_literal = "$SENTINEL `echo must-stay-literal` $(echo must-stay-literal)"
    mcp_servers = [{"name": "probe", "command": sys.executable,
                    "args": [str(Path(__file__).with_name("probe_mcp.py")), str(mcp_audit)],
                    "env": [{"name": "KAUS_TEST_LITERAL", "value": test_literal}]}]
    try:
        created = await bridge.dispatch("session/new", {"cwd": str(workspace), "mcpServers": mcp_servers})
        ident = created["sessionId"]
        session = bridge.sessions[ident]
        report["version"] = await session.request("GET", "/v1/version")
        report["session_new"] = True
        report["config_option_ids"] = [option["id"] for option in created["configOptions"]]
        result = await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "Say hello"}]})
        report["first_turn"] = result
        streamed = "".join(frame["params"]["update"]["content"]["text"] for frame in frames
                           if frame.get("method") == "session/update" and frame["params"]["update"]["sessionUpdate"] == "agent_message_chunk")
        assert streamed == "Native Crush bridge smoke OK.", streamed
        report["text_stream"] = streamed
        old_native_id = session.native_id
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "reasoning", "value": "high"})
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "model", "value": '["test","kaus-test-2"]'})
        report["model_switch"] = session.selected["model"] == "kaus-test-2"
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "mode", "value": "plan"})
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "_approval", "value": "bypass"})
        report["independent_mode_permission"] = session.mode == "plan" and session.permission == "bypass"
        await bridge.dispatch("session/close", {"sessionId": ident})
        frames.clear()
        await bridge.dispatch("session/load", {"sessionId": ident, "cwd": str(workspace), "mcpServers": mcp_servers})
        loaded = bridge.sessions[ident]
        native_request = loaded.request

        async def audited_request(method, path, body=None):
            try:
                return await native_request(method, path, body)
            except Exception as exc:
                report["native_api_error"] = f"{method} {path}: {exc!r}"
                raise

        loaded.request = audited_request
        assert loaded.native_id == old_native_id
        report["resume_same_native_id"] = True
        report["history_replayed"] = any(f.get("params", {}).get("update", {}).get("sessionUpdate") == "user_message_chunk" for f in frames)
        result = await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "Continue"}]})
        report["second_turn"] = result
        report["model_after_resume"] = loaded.selected["model"]
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "mode", "value": "coder"})
        await bridge.dispatch("session/set_config_option", {"sessionId": ident, "configId": "_approval", "value": "ask"})
        await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "TRY_PERMISSION"}]})
        assert "permission:deny" in interactions and not (workspace / "must-not-exist.txt").exists()
        report["native_permission_deny"] = True
        await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "TRY_QUESTION"}]})
        assert "question" in interactions, "native question did not reach ACP"
        assert any(message.get("role") == "tool" and "User answered: yes" in str(message.get("content"))
                   for request in LocalModel.requests for message in request.get("messages", [])), "answer did not reach model"
        report["native_question_answer"] = True
        permission_action = "allow"
        await bridge.dispatch("session/prompt", {"sessionId": ident, "prompt": [{"type": "text", "text": "TRY_MCP"}]})
        audit = [json.loads(line) for line in mcp_audit.read_text().splitlines()]
        assert any(row["method"] == "tools/call" for row in audit), audit
        assert all(row["literal"] == test_literal for row in audit), audit
        report["native_mcp_tool_roundtrip"] = True
        report["mcp_literal_preserved"] = True
        second = await bridge.dispatch("session/new", {"cwd": str(workspace), "mcpServers": []})
        other = bridge.sessions[second["sessionId"]]
        assert other.native_id != loaded.native_id and other.selected["model"] == "kaus-test"
        report["group_member_isolation"] = True
        turn = asyncio.create_task(bridge.dispatch("session/prompt", {"sessionId": ident,
            "prompt": [{"type": "text", "text": "WAIT_CANCEL"}]}))
        await asyncio.wait_for(started_cancel.wait(), 10)
        await bridge.dispatch("session/cancel", {"sessionId": ident})
        assert await asyncio.wait_for(turn, 5) == {"stopReason": "cancelled"}
        report["native_cancel_acknowledged"] = True
        LocalModel.release_cancel.set()
        report["cloud_request_count"] = 0
        report["local_model_requests"] = len(LocalModel.requests)
        report["tool_names"] = sorted({tool.get("function", {}).get("name") for request in LocalModel.requests for tool in request.get("tools", [])})
        assert (isolated / "config/crush.json").read_bytes() == original
        report["original_config_unchanged"] = True
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stream_errors"] = [repr(session.stream_error) for session in bridge.sessions.values() if session.stream_error]
        report["interactions"] = interactions
    finally:
        LocalModel.release_cancel.set()
        await bridge.close()
        server.shutdown()
        server.server_close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crush", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    directory = args.directory or Path(tempfile.mkdtemp(prefix="kaus-crush-native-", dir="/tmp"))
    report = asyncio.run(verify(args.crush.resolve(), directory.resolve()))
    (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
