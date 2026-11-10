"""Agent 自查（2026-11-11）：临时库、合成 Agent 与假 Session Host，不调用模型。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.api_errors import ApiError
from app.capabilities.self_check import SelfCheckService, run_key
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project


class FakeHost:
    def __init__(self, fail=False):
        self.sent, self.fail, self.state = [], fail, "running"

    async def ensure_runtime(self, conversation):
        if self.fail:
            raise RuntimeError("engine offline")

    async def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))

    async def run_state_of(self, conversation):
        return self.state


class FakeImports:
    def __init__(self, items):
        self.items = items

    def candidates(self, agent_key):
        return {"agentKey": agent_key, "items": self.items}


ITEMS = [
    {"id": "alpha:memory", "kind": "memory", "name": "Agent 自带记忆", "state": "available", "available": True},
    {"id": "s-pdf", "kind": "skill", "name": "pdf", "description": "PDF tools", "sourcePath": "/tmp/pdf/SKILL.md",
     "state": "available", "available": True},
    {"id": "s-self", "kind": "skill", "name": "alpha-agent", "state": "bound", "available": True},
    {"id": "m-docs", "kind": "mcp", "name": "docs", "state": "imported", "available": True},
]


@pytest.fixture()
def world(tmp_path):
    unit = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repos = unit.repositories

    async def seed():
        root = await repos.projects.save(Project.create(slug="default", display_name="Root"))
        backend = await repos.backends.save(Backend.create(key="alpha", display_name="Alpha", driver_kind="acp"))
        binding = await repos.bindings.save(AgentBinding.create(project=root, backend=backend, display_name="Alpha"))
        return root, backend, binding
    root, backend, binding = asyncio.run(seed())
    try:
        yield SimpleNamespace(repos=repos, root=root, backend=backend, binding=binding)
    finally:
        unit.close()


def service(world, host=None, items=ITEMS):
    return SelfCheckService(world.repos, host or FakeHost(), FakeImports(items),
                            lambda: {"alpha": {"agentLabel": "Alpha", "backendIds": {world.backend.id}}})


def test_self_check_reviews_only_undecided_items_in_a_read_only_conversation(world):
    host = FakeHost()
    checker = service(world, host)
    options = asyncio.run(checker.options("alpha"))
    assert options["bindingId"] == world.binding.id and options["count"] == 2
    run = asyncio.run(checker.start("alpha", "model-small"))
    assert run["status"] == "running" and run["count"] == 2
    conversation = asyncio.run(world.repos.conversations.get(run["conversationId"]))
    assert conversation.approval_mode == "read_only" and conversation.model_id == "model-small"
    assert conversation.title == "Agent 自查 · Alpha" and conversation.project_id == world.root.id
    [(sent_to, prompt)] = host.sent
    assert sent_to == conversation.id and "kaus_submit_review" in prompt
    assert "s-pdf · 技能 · pdf · /tmp/pdf/SKILL.md · PDF tools" in prompt and "alpha:memory · 自带记忆" in prompt
    assert "s-self" not in prompt and "m-docs" not in prompt  # 规则已判定的、已导入的不复查


def test_submitted_review_is_structured_and_limited_to_the_run(world):
    checker = service(world)
    run = asyncio.run(checker.start("alpha"))
    with pytest.raises(ApiError) as unknown:
        asyncio.run(checker.submit(run["conversationId"], [{"id": "s-self", "verdict": "bound", "reason": "讲它自己"}]))
    assert unknown.value.code == "self_check_unknown_item"
    with pytest.raises(ApiError):
        asyncio.run(checker.submit(run["conversationId"], [{"id": "s-pdf", "verdict": "maybe", "reason": "x"}]))
    with pytest.raises(ApiError):
        asyncio.run(checker.submit(run["conversationId"], [{"id": "s-pdf", "verdict": "likely", "reason": " "}]))
    accepted = asyncio.run(checker.submit(run["conversationId"], [
        {"id": "s-pdf", "verdict": "likely", "reason": "调用了 alpha_render 专有工具"},
        {"id": "s-pdf", "verdict": "bound", "reason": "重复的一条不收"}], "一条疑似绑定"))
    assert accepted == {"accepted": 1}
    result = asyncio.run(checker.get(run["conversationId"]))
    assert result["status"] == "answered" and result["summary"] == "一条疑似绑定"
    assert result["suggestions"] == [{"id": "s-pdf", "kind": "skill", "name": "pdf", "verdict": "likely",
                                      "reason": "调用了 alpha_render 专有工具"}]


def test_a_run_that_ends_without_a_review_or_cannot_start_says_so(world):
    host = FakeHost()
    checker = service(world, host)
    run = asyncio.run(checker.start("alpha"))
    host.state = "idle"
    record = asyncio.run(world.repos.settings.get(run_key(run["conversationId"])))
    asyncio.run(world.repos.settings.save(run_key(run["conversationId"]),
                                          {**record, "sentAt": "2026-01-01T00:00:00+00:00"}))
    assert asyncio.run(checker.get(run["conversationId"]))["status"] == "ended"
    failed = asyncio.run(service(world, FakeHost(fail=True)).start("alpha"))
    assert failed["status"] == "failed" and "engine offline" in failed["error"]


def test_nothing_to_review_or_unknown_agent_is_refused(world):
    with pytest.raises(ApiError) as nothing:
        asyncio.run(service(world, items=ITEMS[2:]).start("alpha"))
    assert nothing.value.code == "self_check_nothing"
    with pytest.raises(ApiError) as unknown:
        asyncio.run(service(world).options("beta"))
    assert unknown.value.code == "agent_not_connected"


def _builtin_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "mcp_servers" / "kaus_builtin_mcp.py"
    spec = importlib.util.spec_from_file_location("kaus_builtin_mcp_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_the_self_check_conversation_gets_the_review_tool(monkeypatch):
    module = _builtin_module()
    plain = module.parse_args(["--project", "project:default", "--agent", "backend:alpha"])
    names = {t["name"] for t in module.handle_request(plain, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert "kaus_submit_review" not in names
    review = module.parse_args(["--project", "project:default", "--agent", "backend:alpha", "--review", "conversation:c1"])
    calls = []
    monkeypatch.setattr(review, "http", lambda method, path, body=None: calls.append((method, path, body)) or {"accepted": 1})
    tools = module.handle_request(review, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert "kaus_submit_review" in {t["name"] for t in tools}
    module.handle_request(review, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "kaus_submit_review", "arguments": {"items": [{"id": "s-pdf", "verdict": "likely", "reason": "r"}], "summary": "s"}}})
    assert calls == [("POST", "/api/skill-tree/self-check/conversation%3Ac1/review",
                      {"items": [{"id": "s-pdf", "verdict": "likely", "reason": "r"}], "summary": "s"})]


def test_review_flag_reaches_the_builtin_server_args(world, tmp_path):
    from app.capabilities.models import EffectiveCapabilities
    from app.projects.builtin_mcp import BUILTIN_MCP_ID, with_builtin_mcp
    effective = EffectiveCapabilities(project_id=world.root.id)
    plain = asyncio.run(with_builtin_mcp(effective, world.binding, world.repos, token_file=tmp_path / "t", url="http://x"))
    reviewed = asyncio.run(with_builtin_mcp(effective, world.binding, world.repos, token_file=tmp_path / "t", url="http://x",
                                            review="conversation:c1"))
    args = lambda result: result.get("mcp", BUILTIN_MCP_ID).config["value"]["args"]  # noqa: E731
    assert "--review" not in args(plain)
    assert args(reviewed)[-2:] == ["--review", "conversation:c1"]


def test_session_host_hands_the_conversation_only_to_projectors_that_ask():
    from runtime.session_host import SessionHost
    host = object.__new__(SessionHost)
    seen = []

    async def plain(binding):
        seen.append(("plain", binding))

    async def wanting(binding, conversation):
        seen.append(("wanting", binding, conversation))
    wanting.wants_conversation = True
    for projector in (plain, wanting):
        host._capability_projector = projector
        asyncio.run(host._project_capabilities("b", "c"))
    assert seen == [("plain", "b"), ("wanting", "b", "c")]
