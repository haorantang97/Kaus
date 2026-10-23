"""OpenClaw must preserve the base ACP resume contract and gateway state."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

from drivers.acp.testing.fake_acp_agent import dress_from_quirks, fake_agent_spec
from drivers.acp.presets import get_preset
from drivers.openclaw.driver import OpenClawAcpDriver
from .test_models import setup_driver


async def test_resume_accepts_metadata_and_passes_it_to_protocol():
    driver = OpenClawAcpDriver(fake_agent_spec("text-stream", cwd="/tmp"),
                              backend_key="openclaw", preset=get_preset("openclaw"))
    driver.gateway = None
    connection = SimpleNamespace(call=AsyncMock(return_value={"sessionId": "synthetic-resume"}))
    result = await driver._resume_session(connection, "synthetic-resume", "/tmp",
                                         metadata={"_meta": {"synthetic": "preserved"}})
    assert result["sessionId"] == "synthetic-resume"
    assert connection.call.call_args.args[1]["_meta"] == {"synthetic": "preserved"}


async def test_existing_openclaw_conversation_restarts_with_same_native_session():
    driver, harness, project, binding = setup_driver()
    driver.agent_spec = fake_agent_spec("text-stream", cwd="/tmp",
                                       dress=dress_from_quirks(driver.quirks))
    first = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    conversation = driver._state(first).conversation.evolve(native_session_id=first.native_session_id)
    await driver.stop_runtime(first)
    second = await driver.start_runtime(conversation, "card")
    try:
        assert second.native_session_id == first.native_session_id
        assert second.metadata["resumed"]
        assert driver._state(second).session_result["_kausOpenClawModels"]["resumed"]
    finally:
        await driver.stop_runtime(second)
