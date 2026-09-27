import asyncio
import base64
import json
from pathlib import Path
import uuid

import pytest

from drivers.crush_bridge.bridge import (
    Bridge, Session, mcp_config, prompt_parts, question_responses, question_schema,
)
from drivers.crush_bridge.protocol import BridgeError, Peer


def event(kind, payload):
    return {"type": kind, "payload": {"type": "updated", "payload": payload}}


@pytest.fixture
def session(tmp_path):
    frames = []
    bridge = Bridge("crush", tmp_path, Peer(frames.append), turn_timeout=1)
    instance = Session(bridge, str(uuid.uuid4()), tmp_path / "session", str(tmp_path))
    instance.native_id, instance.workspace_id = "native-1", "ws-1"
    bridge.sessions[instance.ident] = instance
    return instance, frames


async def test_rpc_permission_reply_is_sent_back_to_native(session):
    instance, frames = session
    requests = []

    async def request(method, path, body=None):
        requests.append((method, path, body))
        return {"resolved": True}

    instance.request = request
    payload = {"id": "p1", "session_id": "native-1", "tool_call_id": "tool-1",
               "tool_name": "write", "description": "Write a file", "params": {"file_path": "a.txt"}}
    task = asyncio.create_task(instance.interact("permission_request", payload))
    await asyncio.sleep(0)
    rpc = frames[-1]
    assert rpc["method"] == "session/request_permission"
    assert rpc["params"]["toolCall"]["rawInput"] == {"file_path": "a.txt"}
    assert requests == []
    instance.bridge.peer.response({"id": rpc["id"], "result": {"outcome": {"outcome": "selected", "optionId": "allow"}}})
    await task
    assert requests[-1][2] == {"permission": payload, "action": "allow"}


async def test_unknown_permission_option_fails_closed(session):
    instance, frames = session
    bodies = []

    async def request(method, path, body=None):
        bodies.append(body)

    instance.request = request
    task = asyncio.create_task(instance.interact("permission_request", {"id": "p"}))
    await asyncio.sleep(0)
    instance.bridge.peer.response({"id": frames[-1]["id"], "result": {"outcome": {"outcome": "selected", "optionId": "invented"}}})
    await task
    assert bodies[-1]["action"] == "deny"


async def test_native_mcp_permission_json_arguments_roundtrip(session):
    instance, frames = session
    bodies = []

    async def request(method, path, body=None):
        bodies.append(body)

    instance.request = request
    payload = {"id": "p", "session_id": "native-1", "tool_name": "mcp_probe_marker", "params": '{"label":"keep"}'}
    task = asyncio.create_task(instance.interact("permission_request", payload))
    await asyncio.sleep(0)
    assert frames[-1]["params"]["toolCall"]["rawInput"] == {"label": "keep"}
    instance.bridge.peer.response({"id": frames[-1]["id"], "result": {"outcome": {"outcome": "selected", "optionId": "allow"}}})
    await task
    assert bodies[-1] == {"permission": {**payload, "params": {"label": "keep"}}, "action": "allow"}


async def test_question_batch_roundtrip_preserves_all_fields(session):
    instance, frames = session
    requests = []
    questions = [{"id": "language", "type": "single_choice", "question": "Language?", "choices": [{"id": "py", "label": "Python"}]},
                 {"id": "packages", "type": "multi_choice", "question": "Packages?", "choices": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
                 {"id": "ready", "type": "yes_no", "question": "Ready?"},
                 {"id": "note", "type": "free_text", "question": "Note?"}]

    async def request(method, path, body=None):
        requests.append((path, body))
        return {"resolved": True}

    instance.request = request
    task = asyncio.create_task(instance.interact("question_batch_request", {"id": "batch", "questions": questions}))
    await asyncio.sleep(0)
    rpc = frames[-1]
    assert rpc["method"] == "elicitation/create"
    schema = rpc["params"]["requestedSchema"]
    assert schema["properties"]["packages"]["items"]["enum"] == ["a", "b"]
    instance.bridge.peer.response({"id": rpc["id"], "result": {"action": "accept", "content": {
        "language": "py", "packages": ["b", "a"], "ready": False, "note": "keep this",
    }}})
    await task
    assert requests[-1] == (instance.base + "/questions/answer", {
        "batch_request_id": "batch", "responses": [
            {"request_id": "language", "selected_ids": ["py"]},
            {"request_id": "packages", "selected_ids": ["b", "a"]},
            {"request_id": "ready", "yes": False},
            {"request_id": "note", "fill_in_text": "keep this"},
        ],
    })


async def test_unsupported_question_client_cancels_native_request(session):
    instance, frames = session
    paths = []

    async def request(method, path, body=None):
        paths.append(path)

    instance.request = request
    instance.turn = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(instance.interact("question_batch_request", {"id": "batch", "questions": []}))
    await asyncio.sleep(0)
    instance.bridge.peer.response({"id": frames[-1]["id"], "error": {"code": -32601}})
    await task
    assert paths[-1].endswith("/questions/cancel")
    with pytest.raises(BridgeError, match="required Crush interaction"):
        await instance.turn


async def test_stream_completion_only_accepts_own_run_and_deduplicates_text(session):
    instance, frames = session
    instance.turn = asyncio.get_running_loop().create_future()
    instance.run_id = "mine"
    message = {"id": "m", "session_id": "native-1", "role": "assistant", "parts": [
        {"type": "reasoning", "data": {"thinking": "Plan"}},
        {"type": "text", "data": {"text": "Hel"}},
    ]}
    await instance.event(event("message", message))
    message["parts"][1]["data"]["text"] = "Hello"
    await instance.event(event("message", message))
    await instance.event(event("run_complete", {"session_id": "native-1", "run_id": "other", "text": "Wrong"}))
    assert not instance.turn.done()
    await instance.event(event("run_complete", {"session_id": "native-1", "run_id": "mine", "message_id": "m", "text": "Hello"}))
    assert await instance.turn == {"stopReason": "end_turn"}
    updates = [f["params"]["update"] for f in frames]
    assert [u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"] == ["Hel", "lo"]
    assert updates[0]["sessionUpdate"] == "agent_thought_chunk"


async def test_native_error_and_cancel_are_not_reported_as_success(session):
    instance, _ = session
    instance.turn = asyncio.get_running_loop().create_future()
    instance.run_id = "r1"
    await instance.event(event("run_complete", {"session_id": "native-1", "run_id": "r1", "error": "quota"}))
    with pytest.raises(BridgeError):
        await instance.turn
    instance.turn = asyncio.get_running_loop().create_future()
    await instance.event(event("run_complete", {"session_id": "native-1", "run_id": "r1", "cancelled": True, "error": "context cancelled"}))
    assert await instance.turn == {"stopReason": "cancelled"}


async def test_tool_updates_are_structured_and_results_not_replayed(session):
    instance, frames = session
    message = {"id": "m", "role": "assistant", "parts": [{"type": "tool_call", "data": {"id": "t", "name": "read", "input": '{"path":"a"}'}}]}
    instance.message(message)
    instance.message(message)
    result = {"id": "r", "role": "tool", "parts": [{"type": "tool_result", "data": {"tool_call_id": "t", "content": "abc", "is_error": False}}]}
    instance.message(result)
    instance.message(result)
    assert len(frames) == 2
    assert frames[0]["params"]["update"]["rawInput"] == {"path": "a"}
    assert frames[1]["params"]["update"]["status"] == "completed"


async def test_foreign_session_and_duplicate_interactions_ignored(session):
    instance, frames = session
    instance.turn = asyncio.get_running_loop().create_future()
    instance.run_id = "r"
    await instance.event(event("permission_request", {"id": "p", "session_id": "foreign"}))
    assert not instance.interactions
    payload = {"id": "p", "session_id": "native-1"}
    await instance.event(event("permission_request", payload))
    await instance.event(event("permission_request", payload))
    await asyncio.sleep(0)
    assert len(frames) == 1
    for task in instance.interactions.values():
        task.cancel()
    await asyncio.gather(*instance.interactions.values(), return_exceptions=True)


async def test_model_effort_updates_are_private_and_follow_success(session):
    instance, _ = session
    calls = []
    model_key = json.dumps(["custom", "m2"], separators=(",", ":"))
    instance.catalog = {model_key: {"value": model_key, "name": "M2", "provider": "custom", "model": "m2", "levels": ["low", "high"], "default": "low"}}

    async def request(method, path, body=None):
        calls.append((path, body))
        if path.endswith("/config"):
            return {"models": {"large": {"provider": "custom", "model": "m2", "reasoning_effort": "low"}},
                    "providers": {"custom": {"api_key": "DO-NOT-EXPOSE", "models": [{"id": "m2", "reasoning_levels": ["low", "high"], "default_reasoning_effort": "low"}]}}}
        return {}

    instance.request = request
    result = await instance.set_option("model", model_key)
    assert calls[0][1]["scope"] == 1
    assert calls[0][1]["model"] == {"provider": "custom", "model": "m2", "reasoning_effort": "low"}
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert next(o for o in result["configOptions"] if o["id"] == "reasoning")["currentValue"] == "low"


async def test_rejected_setting_does_not_mutate_bridge_state(session):
    instance, _ = session

    async def request(*_args, **_kwargs):
        raise BridgeError("Rejected")

    instance.request = request
    with pytest.raises(BridgeError):
        await instance.set_option("approval", "bypass")
    assert instance.permission == "ask"


async def test_resume_missing_and_different_workspace_never_make_new_session(tmp_path, monkeypatch):
    bridge = Bridge("crush", tmp_path / "state", Peer(lambda _: None))
    called = False

    async def start(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(Session, "start", start)
    with pytest.raises(BridgeError, match="original session"):
        await bridge.dispatch("session/load", {"sessionId": str(uuid.uuid4()), "cwd": str(tmp_path), "mcpServers": []})
    assert not called
    ident = str(uuid.uuid4())
    directory = bridge.state_dir / ident
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({"cwd": "/different", "native_id": "original"}))
    with pytest.raises(BridgeError, match="original session"):
        await bridge.dispatch("session/load", {"sessionId": ident, "cwd": str(tmp_path), "mcpServers": []})
    assert not called


class DisconnectedEvents:
    async def aclose(self):
        pass

    async def events(self, _path, ready):
        ready()
        yield event("message", {"id": "m", "role": "assistant", "session_id": "native-1", "parts": [{"type": "text", "data": {"text": "ok"}}]})
        # End without run_complete: a disconnected transport must fail the turn.


async def test_sse_disconnect_fails_pending_turn(session):
    instance, frames = session
    instance.turn = asyncio.get_running_loop().create_future()
    instance.client = DisconnectedEvents()
    await instance.read_events()
    assert frames[0]["params"]["update"]["content"]["text"] == "ok"
    with pytest.raises(BridgeError, match="before turn completion"):
        await instance.turn


async def test_prompt_sends_exact_session_run_id_and_attachments(session):
    instance, _ = session
    sent = []

    async def request(method, path, body=None):
        sent.append((path, body))
        await instance.event(event("run_complete", {"session_id": body["session_id"], "run_id": body["run_id"], "message_id": "m", "text": "ok"}))
        return {}

    instance.request = request
    image = base64.b64encode(b"image").decode()
    result = await instance.prompt([{"type": "text", "text": "look"}, {"type": "image", "mimeType": "image/png", "data": image}])
    assert result == {"stopReason": "end_turn"}
    assert sent[0][1]["session_id"] == "native-1"
    assert sent[0][1]["run_id"]
    assert sent[0][1]["attachments"][0]["content"] == image


def test_mcp_preserves_literal_values_and_transports():
    output = mcp_config([
        {"name": "local", "command": "/bin/tool", "args": ["$(touch bad)"], "env": [{"name": "VALUE", "value": "$HOME `id`"}]},
        {"name": "remote", "type": "http", "url": "https://example.test/mcp", "headers": [{"name": "Authorization", "value": "Bearer $literal"}]},
    ])
    assert output["local"]["args"] == [r"\$(touch bad)"]
    assert output["local"]["env"]["VALUE"] == r"\$HOME \`id\`"
    assert output["remote"]["headers"]["Authorization"] == r"Bearer \$literal"
    with pytest.raises(BridgeError):
        mcp_config([{"name": "same", "command": "a"}, {"name": "same", "command": "b"}])


def test_invalid_prompt_and_question_rejected_without_native_execution():
    with pytest.raises(BridgeError):
        prompt_parts([{"type": "image", "data": "invalid"}])
    with pytest.raises(BridgeError):
        question_responses([{"id": "q", "type": "yes_no"}], {"q": "true"})
    with pytest.raises(BridgeError):
        question_responses([{"id": "q", "type": "single_choice", "choices": [{"id": "a"}]}], {"q": "b"})


async def test_cancel_requires_native_terminal_acknowledgement(session):
    instance, _ = session
    instance.turn = asyncio.get_running_loop().create_future()
    instance.run_id = "r"
    calls = []

    async def request(method, path, body=None):
        calls.append(path)
        await instance.event(event("run_complete", {"session_id": "native-1", "run_id": "r", "cancelled": True}))

    instance.request = request
    await instance.cancel()
    assert calls == [instance.base + "/agent/sessions/native-1/cancel"]
    assert await instance.turn == {"stopReason": "cancelled"}


async def test_model_update_failure_rolls_back_private_selection(session):
    instance, _ = session
    instance.selected = {"provider": "p", "model": "old"}
    value = '["p","new"]'
    instance.catalog = {value: {"value": value, "name": "New", "provider": "p", "model": "new", "default": ""}}
    calls, updates = [], 0

    async def request(method, path, body=None):
        nonlocal updates
        calls.append((path, body))
        if path.endswith("/agent/update"):
            updates += 1
            if updates == 1:
                raise BridgeError("provider unavailable")
        if path.endswith("/config"):
            return {"models": {"large": {"provider": "p", "model": "old"}}}

    instance.request = request
    with pytest.raises(BridgeError, match="could not apply"):
        await instance.set_option("model", value)
    writes = [body for path, body in calls if path.endswith("/config/model")]
    assert [body["model"]["model"] for body in writes] == ["new", "old"]
    assert all(body["scope"] == 1 for body in writes)
    assert instance.selected == {"provider": "p", "model": "old"}


async def test_permissions_and_execution_mode_are_independent(session):
    instance, _ = session
    calls = []

    async def request(method, path, body=None):
        calls.append((path, body))

    instance.request = request
    await instance.set_option("mode", "plan")
    assert instance.permission == "ask"
    await instance.set_option("_approval", "bypass")
    assert instance.mode == "plan"
    assert calls == [(instance.base + "/agent/main", {"agent_id": "plan"}),
                     (instance.base + "/permissions/skip", {"skip": True})]
    assert next(option for option in instance.options() if option["id"] == "_approval")["category"] == "other"


def test_resource_blob_is_validated():
    with pytest.raises(BridgeError, match="Invalid resource"):
        prompt_parts([{"type": "resource", "resource": {"uri": "file:x", "blob": "not base64!"}}])
    with pytest.raises(BridgeError, match="Unsupported native question"):
        question_schema([{"id": "q", "type": "future_unknown_type"}])


async def test_unrelated_native_state_event_may_contain_an_array(session):
    instance, _ = session
    instance.turn = asyncio.get_running_loop().create_future()
    await instance.event({"type": "permissions", "payload": []})
    assert not instance.turn.done()
    instance.turn.cancel()


async def test_projected_mcp_waits_for_connection_and_fails_visibly(session):
    instance, _ = session
    calls = 0

    async def request(method, path, body=None):
        nonlocal calls
        calls += 1
        return {"probe": {"state": "starting" if calls == 1 else "connected"}}

    instance.request = request
    await instance.wait_mcp([{"name": "probe"}])
    assert calls == 2

    async def failed(method, path, body=None):
        return {"probe": {"state": "needs auth"}}

    instance.request = failed
    with pytest.raises(BridgeError, match="projected MCP server probe"):
        await instance.wait_mcp([{"name": "probe"}])
