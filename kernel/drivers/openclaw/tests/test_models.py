"""Model routing must stay session-local and preserve native ACP controls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec, AcpRpcError, AcpTransportError
from drivers.openclaw.driver import OpenClawAcpDriver, OpenClawGateway
from drivers.acp.presets import get_preset
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.contract_tests.harness import collect_until_run_terminal
from drivers.base import MessageInput, ModelRejectedError, UnsupportedCapabilityError


class GatewayFixture:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail_catalog = False
        self.confirm = True

    async def session_key(self, session_id, **kwargs):
        self.rows.setdefault(session_id, {"sessionId": f"native-{session_id}", "modelProvider": "first", "model": "shared"})
        return session_id

    async def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "models.list":
            if self.fail_catalog:
                raise AcpRpcError(-32000, "Gateway unavailable")
            return {"models": [
                {"provider": "first", "id": "shared", "name": "Same model", "contextWindow": 64000, "thinkingLevels": [{"id": "off"}, {"id": "low"}]},
                {"provider": "second", "id": "shared", "name": "Same model", "thinkingLevels": []},
                {"provider": "blocked", "id": "shared", "manualSelectionAllowed": False},
            ]}
        if method == "sessions.describe":
            return {"session": dict(self.rows[params["key"]])}
        if method == "sessions.patch":
            row = self.rows[params["key"]]
            if params["expectedSessionId"] != row["sessionId"]:
                raise AcpRpcError(-32000, "session changed")
            assert set(params) == {"key", "expectedSessionId", "model"}
            if self.confirm:
                row["modelProvider"], row["model"] = params["model"].split("/", 1)
            return {"ok": True, "entry": row}
        raise AssertionError(method)


def setup_driver():
    driver = OpenClawAcpDriver(fake_agent_spec("text-stream", cwd="/tmp"), backend_key="openclaw", preset=get_preset("openclaw"))
    driver.gateway = GatewayFixture()
    harness = FakeAcpHarness(backend_key="openclaw")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    return driver, harness, project, binding


async def test_provider_qualified_catalog_keeps_duplicate_names_and_auth_unknown():
    driver, harness, project, binding = setup_driver()
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["first/shared", "second/shared"]
    assert [m.provider_id for m in catalog.models] == ["first", "second"]
    assert catalog.models[0].context_window == 64000
    assert catalog.models[0].reasoning_levels == ("off", "low")
    assert catalog.models[1].reasoning_levels == ()
    assert catalog.default_model_id == "first/shared"
    assert catalog.engine_default_available and not catalog.degraded
    assert (await driver.read_auth_state(binding)).state == "unknown"
    assert (await driver.get_capabilities()).models.conversation_scoped.is_supported


async def test_switch_reaches_only_named_native_session_and_survives_thought_update():
    driver, harness, project, binding = setup_driver()
    handles = []
    try:
        first = await driver.start_runtime(harness.make_conversation(project, binding).evolve(model_id="first/shared"), "card")
        handles.append(first)
        second = await driver.start_runtime(harness.make_conversation(project, binding).evolve(model_id="first/shared"), "card")
        handles.append(second)
        await driver.set_conversation_model(first, "second/shared")
        assert driver.gateway.rows[first.native_session_id]["modelProvider"] == "second"
        assert driver.gateway.rows[second.native_session_id]["modelProvider"] == "first"
        state = driver._state(first)
        driver._accept_config_update(state, {"configOptions": [{"id": "thought_level", "category": "thought_level", "type": "select", "currentValue": "high", "options": [{"value": "high", "name": "High"}]}]})
        snapshot = driver._snapshot_from_session(state.session_result)
        assert snapshot.current_model_id == "second/shared"
        assert snapshot.current_thought_level == "high"
        assert snapshot.model_option_id == "kaus_gateway_model"
    finally:
        for handle in handles:
            await driver.stop_runtime(handle)


async def test_model_change_reuses_confirmed_identity_and_rejects_reset_session():
    driver, harness, project, binding = setup_driver()
    handle = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        start = len(driver.gateway.calls)
        await driver.set_conversation_model(handle, "second/shared")
        assert [method for method, _ in driver.gateway.calls[start:]] == ["sessions.patch"]
        driver.gateway.rows[handle.native_session_id]["sessionId"] = "reset-session"
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_model(handle, "first/shared")
        assert driver.gateway.rows[handle.native_session_id]["modelProvider"] == "second"
    finally:
        await driver.stop_runtime(handle)


async def test_unconfirmed_selection_not_recorded_and_unknown_model_never_patched():
    driver, harness, project, binding = setup_driver()
    handle = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        driver.gateway.confirm = False
        with pytest.raises(ModelRejectedError, match="拒绝"):
            await driver.set_conversation_model(handle, "second/shared")
        assert driver._snapshot_from_session(driver._state(handle).session_result).current_model_id == "first/shared"
        with pytest.raises(ModelRejectedError, match="尚未确认"):
            await driver.send_message(handle, MessageInput(text="Do not send via an uncertain model"))
        driver.gateway.confirm = True
        await driver.set_conversation_model(handle, "first/shared")
        assert driver._state(handle).session_result["_kausOpenClawModels"]["selectionPending"] is False
        await driver.send_message(handle, MessageInput(text="Confirmed route"))
        assert (await collect_until_run_terminal(driver, handle))[-1].event.type == "run.completed"
        await driver._state(handle).prompt_task
        before = len(driver.gateway.calls)
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_model(handle, "foreign/model")
        assert len(driver.gateway.calls) == before
    finally:
        await driver.stop_runtime(handle)


async def test_catalog_failure_does_not_turn_into_engine_default_availability():
    driver, harness, project, binding = setup_driver()
    driver.gateway.fail_catalog = True
    catalog = await driver.get_model_catalog(binding)
    assert catalog.degraded and not catalog.engine_default_available and not catalog.models
    assert catalog.diagnostics


async def test_resolve_uses_actual_canonical_key_and_missing_is_not_mutated(monkeypatch):
    gateway = OpenClawGateway(AcpAgentSpec(command=("openclaw", "acp")))
    seen = []
    async def resolve(method, params):
        seen.append((method, params))
        return {"ok": True, "key": "agent:dedicated:acp-bridge:12345678-1234-1234-1234-123456789012"}
    monkeypatch.setattr(gateway, "call", resolve)
    assert await gateway.session_key("12345678-1234-1234-1234-123456789012", require_existing=True) == "agent:dedicated:acp-bridge:12345678-1234-1234-1234-123456789012"
    assert seen[0][1] == {"key": "acp-bridge:12345678-1234-1234-1234-123456789012", "allowMissing": False}
    async def missing(method, params):
        return {"ok": False}
    monkeypatch.setattr(gateway, "call", missing)
    with pytest.raises(AcpTransportError, match="不存在"):
        await gateway.session_key("12345678-1234-1234-1234-123456789012", require_existing=True)


async def test_cli_uses_native_auth_and_forbids_broader_mutations(tmp_path, monkeypatch):
    credential = tmp_path / "token"
    credential.write_text("test-token-only")
    gateway = OpenClawGateway(AcpAgentSpec(command=("node", "openclaw.mjs", "acp", "--url", "ws://127.0.0.1:1234", "--token-file", str(credential)), env={"PATH": "/managed/node/bin"}, inherit_env=False))
    launched = []
    async def communicate():
        return b'{"models":[]}', b""
    async def spawn(*args, **kwargs):
        launched.append((args, kwargs))
        return SimpleNamespace(communicate=communicate, returncode=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await gateway.call("models.list", {}) == {"models": []}
    args, kwargs = launched[0]
    assert args[:5] == ("node", "openclaw.mjs", "gateway", "call", "models.list")
    assert "test-token-only" not in args and "--token" not in args
    assert kwargs["env"]["OPENCLAW_GATEWAY_TOKEN"] == "test-token-only"
    assert kwargs["env"]["PATH"] == "/managed/node/bin"
    with pytest.raises(ValueError):
        await gateway.call("config.patch", {"raw": "{}"})
    with pytest.raises(ValueError):
        await gateway.call("sessions.patch", {"key": "a", "model": "p/m", "sandboxMode": "off"})
    assert len(launched) == 1


async def test_cli_error_does_not_reveal_native_credentials(monkeypatch):
    gateway = OpenClawGateway(AcpAgentSpec(command=("openclaw", "acp")))
    async def communicate():
        return b'{"message":"token=hidden credential"}', b"private stderr"
    async def spawn(*args, **kwargs):
        return SimpleNamespace(communicate=communicate, returncode=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AcpRpcError) as error:
        await gateway.call("models.list", {})
    assert "hidden" not in str(error.value) and "private" not in str(error.value)


@pytest.mark.parametrize("routing", ["--session", "--session-label"])
async def test_fixed_native_session_cannot_be_used_for_independent_group_role(routing):
    driver = OpenClawAcpDriver(AcpAgentSpec(command=("openclaw", "acp", routing, "fixed")), backend_key="openclaw", preset=get_preset("openclaw"))
    harness = FakeAcpHarness(backend_key="openclaw")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    driver.register_binding(binding.model_copy(update={"runtime_config": {"group_execution": "planning"}}))
    assert not driver.group_context_isolation()
    with pytest.raises(UnsupportedCapabilityError, match="固定了原生会话"):
        await driver.start_runtime(harness.make_conversation(project, binding), "card")
