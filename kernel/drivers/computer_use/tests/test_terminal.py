"""External terminal handoff boundaries for computer-tool sessions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def test_idle_computer_capability_preserves_native_terminal_and_cmux(tmp_path, monkeypatch):
    from app.conversations.models import Conversation
    from drivers.acp.tests.test_native_cli_selection import driver_for, executable

    executable(tmp_path, "fixture-native")
    driver = driver_for(tmp_path)
    conversation = Conversation.create(
        project_id="project:test", agent_binding_id="binding:test:fixture", title="Synthetic"
    ).evolve(native_session_id="native-id")
    original = await driver.build_external_cli_launch(conversation)
    driver._computer_use_plans[conversation.id] = {"resources": ["desktop:local"]}
    driver.validate_external_session(conversation)
    assert (await driver.build_external_cli_launch(conversation)).model_dump() == original.model_dump()
    assert driver.external_computer_use(conversation) == {"resources": ("desktop:local",), "remote": False}
    stop = AsyncMock(return_value=False)
    monkeypatch.setattr(driver, "_runtime_adapter", lambda: SimpleNamespace(stop_computer_use=stop))
    driver._computer_use_plans[conversation.id]["desktop"] = {"source": "native", "status": "ready"}
    assert (await driver.build_external_cli_launch(conversation)).model_dump() == original.model_dump()
    assert driver.external_computer_use(conversation)["remote"] is True
    assert await driver.stop_external_computer_use(conversation) is False
    stop.return_value = True
    assert await driver.stop_external_computer_use(conversation) is True
    driver._computer_use_plans[conversation.id] = {}
    assert (await driver.build_external_cli_launch(conversation)).model_dump() == original.model_dump()
    assert original.terminal_integration == "synthetic"


async def test_running_turn_cannot_bypass_existing_surface_interlock(tmp_path):
    import asyncio
    from app.conversations.models import Conversation
    from drivers.acp.tests.test_native_cli_selection import driver_for, executable
    from drivers.base import TurnAlreadyRunningError

    executable(tmp_path, "fixture-native")
    driver = driver_for(tmp_path)
    conversation = Conversation.create(project_id="project:test", agent_binding_id="binding:test:fixture",
                                       title="Synthetic").evolve(native_session_id="native-id")
    task = asyncio.create_task(asyncio.sleep(60))
    driver._runtimes["running"] = SimpleNamespace(
        handle=SimpleNamespace(conversation_id=conversation.id), prompt_task=task)
    try:
        with pytest.raises(TurnAlreadyRunningError):
            await driver.build_external_cli_launch(conversation)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_card_only_fallback_is_not_claimed_as_native_terminal_capability(tmp_path, monkeypatch):
    from app.conversations.models import Conversation
    from drivers.acp.tests.test_native_cli_selection import driver_for
    driver = driver_for(tmp_path)
    conversation = Conversation.create(project_id="project:test", agent_binding_id="binding:test:fixture", title="Synthetic")
    monkeypatch.setattr(driver, "_runtime_adapter", lambda: SimpleNamespace(COMPUTER_USE_TERMINAL_MCP=False))
    plan = {"resources": ["browser:shared", "desktop:local"],
            "browser": {"source": "fallback", "status": "ready", "resource": "browser:shared"}}
    driver._computer_use_plans[conversation.id] = plan
    assert driver.external_computer_use(conversation)["resources"] == ()
    plan["desktop"] = {"source": "native", "status": "unknown", "resource": "desktop:local", "exposed": True}
    assert driver.external_computer_use(conversation)["resources"] == ("desktop:local",)
    plan["browser"]["source"] = "native"
    assert driver.external_computer_use(conversation)["resources"] == ("browser:shared", "desktop:local")
