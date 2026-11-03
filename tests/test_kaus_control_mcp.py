"""Kaus control MCP against the real session API (mock engine, no model calls)."""
import asyncio
import json
import time
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "kernel"), str(ROOT / "mcp_servers")]
from app.projects.models import AgentBinding, Project
from session_bootstrap import attach_session_api
import kaus_control_mcp as mcp


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_settings.installed_terminals", lambda: [])
    app = FastAPI()
    runtime = attach_session_api(app,
        config_loader=lambda: {"features": {"session_host_v1": True, "session_host_background": False}},
        db_path=tmp_path / "domain.sqlite3", token_path=tmp_path / "token", log=lambda _: None)
    # Production mounts the read-only domain API (project_domain_v1) beside the session API.
    from app.api.router import build_domain_router
    app.include_router(build_domain_router(runtime.repositories))
    client = TestClient(app)
    client.__enter__()  # one event loop for the whole test, so mock runs keep going between calls
    monkeypatch.setattr(mcp, "_test_client", client, raising=False)
    token = client.get("/api/session-auth/bootstrap").json()["token"]
    def http(method, path, body=None):
        response = client.request(method, path, headers={"Authorization": f"Bearer {token}"},
                                  json=body if body is not None else None)
        if response.status_code >= 400:
            error = response.json().get("error") or {}
            raise mcp.KausError(f"HTTP {response.status_code} {error.get('code', '')}")
        return response.json() if response.content else {}
    monkeypatch.setattr(mcp, "_http", http)
    project = asyncio.run(runtime.repositories.projects.save(Project.create(slug="ctl", display_name="Control")))
    binding = asyncio.run(runtime.repositories.bindings.save(
        AgentBinding.create(project=project, backend="mock", display_name="Mock agent")))
    return runtime, project, binding, http


def _proxy_notes(http, conversation_id):
    events = http("GET", f"/api/conversations/{conversation_id}/events/snapshot")["events"]
    return [e["event"]["data"] for e in events
            if e["event"]["type"] == "extension.event" and e["event"]["name"] == "proxy.action"]


def _wait_pending(kind, *, conversation_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _json(mcp.call_tool("kaus_pending", {})):
            if item["conversationId"] == conversation_id and item["kind"] == kind:
                return item
        time.sleep(0.02)
    raise AssertionError(f"no pending {kind}")


def _json(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_overview_start_read_and_list(tmp_path, monkeypatch):
    runtime, project, binding, _ = _setup(tmp_path, monkeypatch)
    try:
        overview = _json(mcp.call_tool("kaus_overview", {}))
        entry = next(item for item in overview["projects"] if item["projectId"] == project.id)
        assert {"bindingId": binding.id, "name": "Mock agent", "backendId": binding.backend_id} in entry["agents"]
        started = _json(mcp.call_tool("kaus_start_task", {"binding_id": binding.id, "prompt": "hello", "title": "T"}))
        rows = _json(mcp.call_tool("kaus_conversations", {}))
        assert any(row["id"] == started["conversationId"] and row["surface"] == "card" for row in rows)
        read = _json(mcp.call_tool("kaus_read", {"conversation_id": started["conversationId"]}))
        assert read["title"] == "T" and "pendingForUser" in read
        assert _json(mcp.call_tool("kaus_external_sessions", {})) == []
        missing = mcp.call_tool("kaus_start_task", {"binding_id": "binding:none", "prompt": "x"})
        assert missing["isError"]
    finally:
        _close(runtime)


def test_return_requires_explicit_user_confirmation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "_http", lambda *args, **kw: calls.append(args) or {})
    refused = mcp.call_tool("kaus_return_to_conversation", {"conversation_id": "c", "user_confirmed": False})
    assert refused["isError"] and calls == []
    mcp.call_tool("kaus_return_to_conversation", {"conversation_id": "c", "user_confirmed": True})
    assert calls == [("POST", "/api/conversations/c/surface/card?force=1")]


def _close(runtime):
    mcp._test_client.__exit__(None, None, None)
    asyncio.run(runtime.aclose())


def test_proxied_actions_require_explicit_user_consent(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "_http", lambda *args, **kw: calls.append(args) or {})
    for name, args in (("kaus_answer_pending", {"conversation_id": "c", "interaction_id": "i", "option_id": "allow"}),
                       ("kaus_change_settings", {"conversation_id": "c", "approval_mode": "bypass"})):
        for confirmed in (None, False, "yes"):
            refused = mcp.call_tool(name, {**args, **({} if confirmed is None else {"user_confirmed": confirmed})})
            assert refused["isError"] and "user_confirmed=true" in refused["content"][0]["text"]
    assert calls == []


def test_answer_approve_and_deny_reach_the_interaction_and_are_recorded(tmp_path, monkeypatch):
    runtime, project, binding, http = _setup(tmp_path, monkeypatch)
    try:
        approve = _json(mcp.call_tool("kaus_start_task", {"binding_id": binding.id, "prompt": "go", "title": "A"}))["conversationId"]
        deny = _json(mcp.call_tool("kaus_start_task", {"binding_id": binding.id, "prompt": "go", "title": "D"}))["conversationId"]
        pending = _wait_pending("permission", conversation_id=approve)
        assert pending["conversationTitle"] == "A" and pending["project"] == "Control" and pending["agent"] == "Mock agent"
        assert pending["title"] == "允许写入 a.txt？"
        assert [o["optionId"] for o in pending["options"]] == ["allow", "deny"]
        read = _json(mcp.call_tool("kaus_read", {"conversation_id": approve}))
        assert read["pending"][0]["interactionId"] == pending["interactionId"]

        offered_only = mcp.call_tool("kaus_answer_pending", {"conversation_id": approve, "interaction_id": pending["interactionId"],
                                                             "option_id": "always", "user_confirmed": True})
        assert offered_only["isError"]
        approved = _json(mcp.call_tool("kaus_answer_pending", {"conversation_id": approve, "interaction_id": pending["interactionId"],
                                                              "option_id": "allow", "user_confirmed": True}))
        assert approved["resolved"] is True and approved["action"] == "approved" and approved["recordedInTimeline"] is True
        # The mock script moves on only once the permission is resolved.
        question = _wait_pending("question", conversation_id=approve)
        assert question["canCancel"] is True

        denied_item = _wait_pending("permission", conversation_id=deny)
        denied = _json(mcp.call_tool("kaus_answer_pending", {"conversation_id": deny, "interaction_id": denied_item["interactionId"],
                                                            "option_id": "deny", "user_confirmed": True}))
        assert denied["action"] == "denied"
        events = http("GET", f"/api/conversations/{deny}/events/snapshot")["events"]
        assert any(e["event"]["type"] == "permission.resolved" and e["event"]["decision"] == "deny" for e in events)

        assert _proxy_notes(http, approve) == [{
            "proxy": "main_agent", "action": "approved", "subject": "允许写入 a.txt？", "choice": "允许",
            "interactionId": pending["interactionId"], "interactionKind": "permission",
            "text": "主 Agent 已代为批准：允许写入 a.txt？"}]
        assert _proxy_notes(http, deny)[0]["text"] == "主 Agent 已拒绝：允许写入 a.txt？"
        # A UI answer (no proxy marker) leaves no proxy note.
        answered = _wait_pending("question", conversation_id=deny)
        http("POST", f"/api/conversations/{deny}/interactions/{answered['interactionId']}",
             {"kind": "question", "cancelled": True})
        assert len(_proxy_notes(http, deny)) == 1
        missing = mcp.call_tool("kaus_answer_pending", {"conversation_id": deny, "interaction_id": "nope",
                                                       "option_id": "allow", "user_confirmed": True})
        assert missing["isError"]
    finally:
        _close(runtime)


def test_kernel_rejects_a_proxied_answer_the_card_does_not_offer(tmp_path, monkeypatch):
    runtime, project, binding, http = _setup(tmp_path, monkeypatch)
    try:
        conversation = _json(mcp.call_tool("kaus_start_task", {"binding_id": binding.id, "prompt": "go"}))["conversationId"]
        pending = _wait_pending("permission", conversation_id=conversation)
        path = f"/api/conversations/{conversation}/interactions/{pending['interactionId']}"
        for body in ({"kind": "permission", "optionId": "always"}, {"kind": "permission", "cancelled": True},
                     {"kind": "question", "optionId": "allow"}):
            try:
                http("POST", path, {**body, "proxy": "main_agent"})
            except mcp.KausError as exc:
                assert "invalid_answer" in str(exc)
            else:
                raise AssertionError(body)
        assert _proxy_notes(http, conversation) == []
        assert _wait_pending("permission", conversation_id=conversation)["interactionId"] == pending["interactionId"]
    finally:
        _close(runtime)


def test_change_settings_records_each_change_and_allows_bypass(tmp_path, monkeypatch):
    runtime, project, binding, http = _setup(tmp_path, monkeypatch)
    try:
        conversation = http("POST", f"/api/projects/{project.id}/conversations", {"bindingId": binding.id, "title": "S"})["id"]
        settings = _json(mcp.call_tool("kaus_conversation_settings", {"conversation_id": conversation}))
        assert {m["modelId"] for m in settings["models"]} >= {"mock-small", "mock-large"}
        assert settings["approvalModes"] == []

        unsupported = mcp.call_tool("kaus_change_settings", {"conversation_id": conversation, "approval_mode": "bypass",
                                                             "user_confirmed": True})
        assert unsupported["isError"] and "conversation_approval_unsupported" in unsupported["content"][0]["text"]
        assert _proxy_notes(http, conversation) == []

        driver = runtime.registry.get(binding.backend_id)
        monkeypatch.setattr(driver, "conversation_controls", lambda _binding: {
            "reasoning": True, "approvalModes": ["ask", "bypass"],
            "approvalOptions": [{"id": "ask", "name": "Ask"}, {"id": "bypass", "name": "Full access"}]})
        settings = _json(mcp.call_tool("kaus_conversation_settings", {"conversation_id": conversation}))
        assert {"id": "bypass", "name": "Full access"} in settings["approvalModes"]

        changed = _json(mcp.call_tool("kaus_change_settings", {
            "conversation_id": conversation, "model_id": "mock-large", "reasoning_effort": "xhigh",
            "approval_mode": "bypass", "user_confirmed": True}))
        assert changed["modelId"] == "mock-large" and changed["reasoningEffort"] == "xhigh"
        assert changed["approvalMode"] == "bypass" and changed["recordedInTimeline"] is True
        detail = http("GET", f"/api/conversations/{conversation}")["conversation"]
        assert detail["agentBindingId"] == binding.id and detail["approvalMode"] == "bypass"
        notes = _proxy_notes(http, conversation)
        assert [(n["setting"], n["value"]) for n in notes] == [("model", "mock-large"), ("reasoning", "xhigh"), ("approval", "bypass")]
        assert all(n["proxy"] == "main_agent" for n in notes)
        assert notes[-1]["text"] == "主 Agent 已将权限改为：Full access"

        bad_level = mcp.call_tool("kaus_change_settings", {"conversation_id": conversation, "model_id": "mock-small",
                                                           "user_confirmed": True})
        assert bad_level["isError"] and "reasoning_mode_not_supported" in bad_level["content"][0]["text"]
        try:
            http("PATCH", f"/api/conversations/{conversation}", {"executionMode": "x", "proxy": "main_agent"})
        except mcp.KausError as exc:
            assert "proxy_setting_unsupported" in str(exc)
        else:
            raise AssertionError("proxied execution mode accepted")
    finally:
        _close(runtime)


def test_protocol_offers_no_delete_or_project_config_tool(monkeypatch):
    listed = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"kaus_return_to_conversation", "kaus_pending", "kaus_answer_pending", "kaus_change_settings"} <= names
    assert not any(word in name for name in names for word in ("delete", "remove", "archive", "project", "config", "binding"))
    change = next(tool for tool in listed["result"]["tools"] if tool["name"] == "kaus_change_settings")
    assert set(change["inputSchema"]["properties"]) == {"conversation_id", "model_id", "reasoning_effort",
                                                         "approval_mode", "user_confirmed"}
    # Every Kaus write the tools make: no DELETE and no Project/Binding/group/material writes.
    calls = []
    def http(method, path, body=None):
        calls.append((method, path))
        return {"conversation": {"agentBindingId": "b"}, "timeline": {"pendingInteractions": [
            {"interactionId": "i", "interactionKind": "permission",
             "permission": {"requestId": "i", "title": "t", "options": [{"optionId": "allow", "label": "Allow"}]}}]}}
    monkeypatch.setattr(mcp, "_http", http)
    mcp.call_tool("kaus_answer_pending", {"conversation_id": "c", "interaction_id": "i", "option_id": "allow", "user_confirmed": True})
    mcp.call_tool("kaus_change_settings", {"conversation_id": "c", "model_id": "m", "approval_mode": "bypass", "user_confirmed": True})
    writes = [(m, p) for m, p in calls if m != "GET"]
    assert writes == [("POST", "/api/conversations/c/interactions/i"), ("PATCH", "/api/conversations/c"),
                      ("PATCH", "/api/conversations/c")]
    assert mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_initialize_carries_the_operating_rules_for_any_main_agent():
    result = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    rules = result["instructions"]
    assert "user_confirmed=true" in rules and "切回" in rules and "审批" in rules
    assert "明确同意" in rules and "不再询问" in rules and "完全访问" in rules
    assert "不删除" in rules and "Project 配置" in rules
