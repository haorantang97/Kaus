"""The native permission Gateway contract stays separate from ACP reasoning."""
from copy import deepcopy

import pytest

from drivers.acp.client import AcpConnection, AcpRpcError, AcpTransportError
from drivers.base import MessageInput, ModelRejectedError
from drivers.openclaw.tests.test_models import GatewayFixture, setup_driver


class PermissionGateway(GatewayFixture):
    failure = None

    async def call(self, method, params):
        if method != "sessions.patch" or "permissionMode" not in params:
            return await super().call(method, params)
        self.calls.append((method, deepcopy(params)))
        row = self.rows[params["key"]]
        assert set(params) == {"key", "expectedSessionId", "expectedPermissionMode", "permissionMode"}
        if params["expectedSessionId"] != row["sessionId"] or params["expectedPermissionMode"] != row.get("permissionMode"):
            raise AcpRpcError(-32000, "session changed")
        if self.failure == "reject":
            raise AcpRpcError(-32000, "native policy prohibits full access")
        if self.failure != "wrong_ack":
            if params["permissionMode"] is None:
                row.pop("permissionMode", None)
            else:
                row["permissionMode"] = params["permissionMode"]
        if self.failure == "lost_ack":
            raise AcpTransportError("connection lost after commit")
        return {"ok": True, "entry": deepcopy(row)}


async def test_all_permission_choices_are_session_scoped_and_preserved_on_config_update(monkeypatch):
    driver, harness, project, binding = setup_driver()
    driver.gateway = PermissionGateway()
    calls = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        calls.append((method, params))
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    conv = harness.make_conversation(project, binding).evolve(approval_mode="ask")
    first = await driver.start_runtime(conv, "card")
    second = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        controls = driver.runtime_conversation_controls(first, binding)
        assert controls["approvalModes"] == ["native:inherit", "read_only", "ask", "auto", "bypass"]
        assert driver.gateway.rows[first.native_session_id]["permissionMode"] == "guarded"
        assert "permissionMode" not in driver.gateway.rows[second.native_session_id]
        for policy, expected in [("read_only", "read-only"), ("auto", "workspace"), ("bypass", "full"), ("native:inherit", None)]:
            conv = conv.evolve(approval_mode=policy)
            await driver.set_conversation_selection(first, conv)
            assert driver.gateway.rows[first.native_session_id].get("permissionMode") == expected
            assert "permissionMode" not in driver.gateway.rows[second.native_session_id]
        assert not [p for m, p in calls if m == "session/set_config_option" and p.get("optionId") == "kaus_gateway_permission"]
        driver._accept_config_update(driver._state(first), {"configOptions": [{"id": "thought_level", "category": "thought_level", "currentValue": "high", "options": [{"value": "high"}]}]})
        updated = driver.runtime_conversation_controls(first, binding)
        assert updated["approvalModes"] == controls["approvalModes"]
        assert updated["reasoningLevels"] == ["high"]
    finally:
        await driver.stop_runtime(first)
        await driver.stop_runtime(second)


@pytest.mark.parametrize("failure", ["reject", "wrong_ack", "lost_ack"])
async def test_failed_change_never_commits_snapshot_and_retry_can_restore_previous_mode(failure):
    driver, harness, project, binding = setup_driver()
    driver.gateway = PermissionGateway()
    conv = harness.make_conversation(project, binding).evolve(approval_mode="ask")
    runtime = await driver.start_runtime(conv, "card")
    try:
        driver.gateway.failure = failure
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, conv.evolve(approval_mode="bypass"))
        assert driver._state(runtime).conversation.approval_mode == "ask"
        if failure != "reject":
            with pytest.raises(ModelRejectedError, match="尚未确认"):
                await driver.send_message(runtime, MessageInput(text="blocked"))
            driver.gateway.failure = None
            await driver.set_conversation_selection(runtime, conv)
            assert driver.gateway.rows[runtime.native_session_id]["permissionMode"] == "guarded"
            assert not driver._state(runtime).permission_pending
    finally:
        await driver.stop_runtime(runtime)


async def test_reset_or_missing_session_is_never_recreated_by_permission_change():
    driver, harness, project, binding = setup_driver()
    driver.gateway = PermissionGateway()
    conv = harness.make_conversation(project, binding).evolve(approval_mode="ask")
    runtime = await driver.start_runtime(conv, "card")
    try:
        driver.gateway.rows[runtime.native_session_id]["sessionId"] = "new-incarnation"
        before = len([c for c in driver.gateway.calls if c[0] == "sessions.patch"])
        with pytest.raises(ModelRejectedError):
            await driver.set_conversation_selection(runtime, conv.evolve(approval_mode="auto"))
        assert len([c for c in driver.gateway.calls if c[0] == "sessions.patch"]) == before
        assert driver.gateway.rows[runtime.native_session_id]["permissionMode"] == "guarded"
    finally:
        await driver.stop_runtime(runtime)
