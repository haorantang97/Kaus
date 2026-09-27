"""A local session record or static model list is not authentication evidence."""
from dataclasses import replace
from datetime import timedelta

import pytest

from drivers.acp.client import AcpRpcError
from drivers.acp.presets import AgentQuirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_driver import _driver_with
from drivers.base import CreateSessionOptions, MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal


def make_driver():
    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model"))
    driver.preset = replace(driver.preset, session_creation_proves_auth=False, login_command="local-cli login")
    return driver


async def test_local_session_and_static_models_leave_auth_unknown():
    driver = make_driver()
    harness = FakeAcpHarness()
    binding = harness.make_binding(harness.make_project(), driver)
    assert len((await driver.get_model_catalog(binding)).models) == 2
    assert (await driver.read_auth_state(binding)).state == "unknown"
    await driver.create_native_session(binding, CreateSessionOptions())
    assert (await driver.read_auth_state(binding)).state == "unknown"


@pytest.mark.parametrize("error", [
    AcpRpcError(-32000, "Authentication required"),
    AcpRpcError(-32603, "Internal error: You need to sign in to use this model."),
    AcpRpcError(-32603, "Internal error", {"message": "Failed to authenticate: OAuth session expired and could not be refreshed"}),
])
async def test_static_catalog_cannot_clear_sign_out_but_successful_prompt_can(error, monkeypatch):
    driver = make_driver()
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    state = driver._state(runtime)
    original = state.connection.call

    async def reject_prompt(method, params, **kwargs):
        if method == "session/prompt":
            raise error
        return await original(method, params, **kwargs)

    monkeypatch.setattr(state.connection, "call", reject_prompt)
    try:
        await driver.send_message(runtime, MessageInput(text="hello"))
        assert (await collect_until_run_terminal(driver, runtime))[-1].event.type == "run.failed"
        await state.prompt_task
        assert (await driver.read_auth_state(binding)).state == "signed_out"
        driver._remember_models(binding.id, state.session_result)
        assert (await driver.read_auth_state(binding)).state == "signed_out"
        # A stale-catalog refresh can still return a static list without login.
        driver._catalog[binding.id] = replace(driver._catalog[binding.id],
            fetched_at=driver._catalog[binding.id].fetched_at - timedelta(hours=1))
        assert len((await driver.get_model_catalog(binding)).models) == 2
        assert (await driver.read_auth_state(binding)).state == "signed_out"
        other = harness.make_binding(project, driver, discriminator="independent")
        assert (await driver.read_auth_state(other)).state == "unknown"
    finally:
        await driver.stop_runtime(runtime)

    next_runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert (await driver.read_auth_state(binding)).state == "signed_out"
        await driver.send_message(next_runtime, MessageInput(text="logged in outside Kaus"))
        assert (await collect_until_run_terminal(driver, next_runtime))[-1].event.type == "run.completed"
        assert (await driver.read_auth_state(binding)).state == "signed_in"
        driver._remember_models(binding.id, driver._state(next_runtime).session_result)
        assert (await driver.read_auth_state(binding)).state == "signed_in"
        assert (await driver.read_auth_state(other)).state == "unknown"
    finally:
        await driver.stop_runtime(next_runtime)
