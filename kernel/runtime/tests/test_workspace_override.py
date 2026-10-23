"""Module adaptation must not fall back to the bound project's workspace."""
from unittest.mock import AsyncMock
import pytest
from drivers.base import SessionProjection, UnsupportedCapabilityError
from runtime.tests.test_session_host import make_world
from runtime.tests.test_batch43_mcp_wire import _effective

async def test_staging_keeps_project_capabilities_and_binding_unchanged(tmp_path):
    world = make_world()
    driver, conversation = await world.add_backend()
    binding = await world.bindings.get(conversation.agent_binding_id)
    world.host.workspace_override_resolver = lambda cid: str(tmp_path) if cid == conversation.id else None
    world.host._capability_projector = AsyncMock(return_value=_effective(binding.project_id, ('files', {'command':'test-server'})))
    await world.host.start_runtime(conversation)
    try:
        projection = world.host.session_projection(conversation.id)
        assert projection.options.workspace_root == str(tmp_path)
        assert driver.projected_capabilities[conversation.id] == ('files',)
        assert await world.bindings.get(binding.id) == binding
        with pytest.raises(UnsupportedCapabilityError, match='工作台'):
            await world.host.prepare_external_session(conversation, binding, driver)
    finally:
        await world.host.stop_runtime(conversation.id)

@pytest.mark.parametrize('failure', ['unsupported', 'missing_path', 'wrong_projection', 'capability_error'])
async def test_staging_failure_never_starts_in_original_workspace(tmp_path, failure):
    world = make_world()
    driver, conversation = await world.add_backend()
    world.host.workspace_override_resolver = lambda _: str(tmp_path / 'missing') if failure == 'missing_path' else str(tmp_path)
    if failure == 'unsupported': driver.supports_workspace_override = lambda: False
    if failure == 'wrong_projection': driver.workspace_session_projection = AsyncMock(return_value=SessionProjection())
    if failure == 'capability_error': world.host._capability_projector = AsyncMock(side_effect=RuntimeError('projection failed'))
    with pytest.raises((UnsupportedCapabilityError, RuntimeError)):
        await world.host.start_runtime(conversation)
    assert conversation.id not in driver.received_session_metadata
