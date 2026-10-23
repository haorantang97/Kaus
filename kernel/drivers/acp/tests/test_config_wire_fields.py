from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpRpcError
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec


@pytest.mark.parametrize("value", ["plan", True])
async def test_config_uses_standard_field_first(value):
    driver = AcpDriver(fake_agent_spec())
    call = AsyncMock(return_value={"configOptions": []})
    await driver._call_config_option(SimpleNamespace(call=call), "session", "mode", value)
    params = {"sessionId": "session", "configId": "mode", "value": value}
    if isinstance(value, bool):
        params["type"] = "boolean"
    call.assert_awaited_once_with("session/set_config_option", params, timeout=driver._call_timeout)


async def test_old_field_is_only_used_after_invalid_params():
    driver = AcpDriver(fake_agent_spec())
    call = AsyncMock(side_effect=[AcpRpcError(-32602, "missing optionId"), {}])
    await driver._call_config_option(SimpleNamespace(call=call), "session", "mode", "plan")
    assert [c.args[1] for c in call.await_args_list] == [
        {"sessionId": "session", "configId": "mode", "value": "plan"},
        {"sessionId": "session", "optionId": "mode", "value": "plan"},
    ]


@pytest.mark.parametrize("code", [-32603, -32000])
async def test_internal_or_policy_failure_does_not_repeat_mutation(code):
    driver = AcpDriver(fake_agent_spec())
    error = AcpRpcError(code, "configuration rejected")
    call = AsyncMock(side_effect=error)
    with pytest.raises(AcpRpcError) as caught:
        await driver._call_config_option(SimpleNamespace(call=call), "session", "mode", "plan")
    assert caught.value is error
    assert call.await_count == 1
