"""Restricted module requests cannot inherit the project's tool access."""
from types import SimpleNamespace
import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.hermes.workbench_launcher import install_guard
from drivers.workbench_execution.claude import prepare
from app.workbench.execution import install_execution_policy


async def test_native_sdk_has_no_tools_hooks_plugins_or_mcp(monkeypatch):
    async def supported(*args):
        return True
    monkeypatch.setattr("drivers.skill_execution.claude.supported", supported)
    spec = AcpAgentSpec(command=("example",), env={"ROUTING": "preserved"})
    updated, metadata = await prepare(spec, {"mcpServers": [{"name": "project-tools"}]}, ("example", "1"))
    options = metadata["_meta"]["claudeCode"]["options"]
    assert options["tools"] == options["skills"] == options["plugins"] == options["settingSources"] == []
    assert options["strictMcpConfig"] and options["settings"]["disableAllHooks"]
    assert metadata["mcpServers"] == []
    assert updated.env["ROUTING"] == "preserved"
    assert spec.env == {"ROUTING": "preserved"}


def test_runtime_guard_rejects_tool_refresh_and_dispatch():
    class Native:
        def __init__(self, *, skip_memory=False, skip_background_review=False,
                     skip_context_files=False, load_soul_identity=True, enabled_toolsets=None, **kwargs):
            self.received = (skip_memory, skip_background_review, skip_context_files, load_soul_identity)
            self.tools = ["terminal"]
            self.valid_tool_names = {"terminal"}
        def _execute_tool_calls(self, *args):
            pytest.fail("Native tool executed")
    guarded = install_guard(Native)()
    assert guarded.received == (True, True, True, False)
    guarded.tools = ["mcp:escape"]
    guarded.valid_tool_names = {"mcp:escape"}
    assert not guarded.tools and not guarded.valid_tool_names
    with pytest.raises(RuntimeError, match="disabled"):
        guarded._execute_tool_calls([])


def test_guard_fails_if_upstream_contract_changes():
    with pytest.raises(RuntimeError, match="does not support"):
        install_guard(object)


async def test_restricted_protocol_rejects_all_client_tool_requests():
    calls = []
    driver = AcpDriver(AcpAgentSpec(command=("example",)))
    state = SimpleNamespace(closed=False, interrupt_requested=False, prompt_task=None,
        runtime_metadata={"_kausExecutionPolicy": "context-only"},
        connection=SimpleNamespace(respond=lambda *args, **kwargs: calls.append((args, kwargs))))
    for method in ("fs/read_text_file", "fs/write_text_file", "terminal/create", "session/request_permission"):
        await driver._on_request(state, 1, method, {})
    assert len(calls) == 4
    assert calls[-1][1]["result"]["outcome"]["outcome"] == "cancelled"
    assert all(call[1]["error"]["code"] == -32601 for call in calls[:3])


def test_policy_restored_for_direct_reopen_and_previous_attempts(tmp_path):
    job = {"executionPolicy": "context-only", "steps": [{"conversationId": "conversation:new", "previousConversationIds": ["conversation:old"]}]}
    service = SimpleNamespace(root=tmp_path, store=SimpleNamespace(all=lambda _: [job],
        is_module_conversation=lambda identity: identity in {"conversation:new", "conversation:old"}), host=SimpleNamespace())
    install_execution_policy(service)
    resolve = service.host.execution_policy_resolver
    assert resolve("conversation:ordinary") is None
    assert resolve("conversation:new")["policy"] == "context-only"
    assert resolve("conversation:old")["workspace_root"] != resolve("conversation:new")["workspace_root"]


def test_normal_conversation_uses_only_indexed_ownership_lookup(tmp_path):
    def never_load_jobs(bucket):
        pytest.fail("Ordinary conversations must not scan module job payloads")
    service = SimpleNamespace(root=tmp_path, store=SimpleNamespace(all=never_load_jobs,
        is_module_conversation=lambda identity: False), host=SimpleNamespace())
    install_execution_policy(service)
    assert service.host.execution_policy_resolver("conversation:ordinary") is None


def test_unimplemented_policy_is_not_advertised():
    driver = AcpDriver(AcpAgentSpec(command=("example",)), preset=get_preset("codex"))
    assert driver.supports_execution_policy("project")
    assert not driver.supports_execution_policy("context-only")
