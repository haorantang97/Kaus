"""OpenClaw selects models through its Gateway, independent of the ACP bridge quirks."""
from __future__ import annotations

from drivers.acp.presets import get_preset
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.openclaw.driver import OpenClawAcpDriver
from drivers.openclaw.tests.test_models import GatewayFixture


def _binding(driver):
    harness = FakeAcpHarness()
    binding = harness.make_binding(harness.make_project(), driver).model_copy(update={"default_model_id": "first/shared"})
    driver.register_binding(binding)
    return binding


def _driver(gateway):
    driver = OpenClawAcpDriver(fake_agent_spec("text-stream", cwd="/tmp"), backend_key="openclaw", preset=get_preset("openclaw"))
    driver.gateway = gateway
    return driver


async def test_gateway_selection_applies_before_any_initialize():
    driver = _driver(GatewayFixture())
    assert driver.quirks.model_switch == "none"  # bridge preset, nothing absorbed yet
    catalog = await driver.get_model_catalog(_binding(driver))
    assert catalog.selection_applies is True
    assert catalog.default_model_id == "first/shared"


async def test_empty_configured_list_is_no_models_not_cannot_select():
    gateway = GatewayFixture()

    async def empty(method, params):
        assert method == "models.list"
        return {"models": [{"provider": "blocked", "id": "x", "manualSelectionAllowed": False}]}

    gateway.call = empty
    driver = _driver(gateway)
    catalog = await driver.get_model_catalog(_binding(driver))
    assert catalog.models == ()
    assert catalog.selection_applies is True
    assert catalog.diagnostics


async def test_unreachable_gateway_is_unknown():
    gateway = GatewayFixture()
    gateway.fail_catalog = True
    driver = _driver(gateway)
    catalog = await driver.get_model_catalog(_binding(driver))
    assert catalog.degraded and catalog.selection_applies is None


async def test_without_gateway_the_ordinary_session_probe_is_used():
    driver = _driver(None)
    catalog = await driver.get_model_catalog(_binding(driver))  # used to raise AttributeError
    assert catalog.models
    assert catalog.selection_applies is False  # bridge preset has no model switch
