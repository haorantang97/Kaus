"""Selected fallback -> ACP child -> MCP child -> approval -> tool response."""
import sys

import pytest

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from drivers.acp.driver import AcpDriver
from drivers.acp.testing import fake_mcp_server
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import InteractionResponse, MessageInput
from drivers.computer_use import Candidate, integration, probe
from drivers.contract_tests.harness import collect_until_run_terminal


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_fallback_reaches_native_mcp_and_respects_approval(tmp_path, monkeypatch, decision):
    async def native(*args, **kwargs):
        return {}

    async def fallback(**kwargs):
        return (Candidate(
            "fixture-browser", "browser", "fallback",
            resource_id=f"browser:context:{tmp_path.name}",
            mcp_server={"name": "fixture-browser", "command": sys.executable,
                        "args": [fake_mcp_server.__file__, "--nonce", "fallback-approval"],
                        "env": []},
        ),)

    monkeypatch.setattr(integration, "native_status", native)
    monkeypatch.setattr(integration, "ambient_mcp", lambda *_: ())
    monkeypatch.setattr(probe, "probe_fallbacks", fallback)
    driver = AcpDriver(fake_agent_spec("mcp-ping-permission", cwd=str(tmp_path)),
                       backend_key="acp", default_cwd=str(tmp_path))
    conversation = Conversation.create(project_id="project:cu-fixture",
        agent_binding_id="binding:cu-fixture:acp", title="Synthetic control")
    projection = await driver.session_options_for(
        conversation, EffectiveCapabilities(project_id=conversation.project_id))
    assert projection.options.metadata["_kausComputerUse"]["browser"]["source"] == "fallback"
    runtime = await driver.start_runtime(conversation, "card", session_options=projection.options)
    try:
        async def resolve(envelope):
            await driver.resolve_interaction(runtime, envelope.event.request.request_id,
                InteractionResponse(kind="permission", option_id=decision))

        await driver.send_message(runtime, MessageInput(text="Synthetic MCP test"))
        events = await collect_until_run_terminal(driver, runtime, on_interaction=resolve, timeout=30)
    finally:
        await driver.stop_runtime(runtime)
    assert len([item for item in events if item.event.type == "permission.requested"]) == 1
    text = "\n".join(getattr(item.event, "text", "") or "" for item in events)
    assert (fake_mcp_server.pong("fallback-approval") in text) == (decision == "allow")
    if decision == "deny":
        assert "was not called" in text
