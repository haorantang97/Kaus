from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import UnsupportedCapabilityError


async def test_scope_rejection_precedes_native_prompt_and_template_expansion(monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    native = AsyncMock()
    validate = AsyncMock(side_effect=UnsupportedCapabilityError('未选中技能'))
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(validate_prompt=validate))
    state = SimpleNamespace(skill_plan=object(), connection=SimpleNamespace(call=native),
        handle=SimpleNamespace(binding_id='fixture'))
    with pytest.raises(UnsupportedCapabilityError, match='未选中'):
        await driver._guarded_prompt(state, {'prompt': [{'type': 'text', 'text': '/unselected'}]})
    validate.assert_awaited_once()
    native.assert_not_awaited()


async def test_native_mode_does_not_restrict_original_skill_commands(monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    native = AsyncMock(return_value={'stopReason': 'end_turn'})
    validate = AsyncMock()
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(validate_prompt=validate))
    state = SimpleNamespace(skill_plan=None, connection=SimpleNamespace(call=native))
    assert await driver._guarded_prompt(state, {'prompt': []}) == {'stopReason': 'end_turn'}
    validate.assert_not_awaited()


def test_native_command_catalog_is_filtered_before_timeline_translation(monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    filtered = {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'chosen'}]}
    guard = Mock(return_value=filtered)
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(filter_session_update=guard))
    translator = SimpleNamespace(on_session_update=Mock(return_value=[]))
    state = SimpleNamespace(closed=False, client_interactions=None, skill_plan=object(),
        session_id='fixture', translator=translator)
    monkeypatch.setattr(driver, '_push', Mock())
    update = {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'unselected'}]}
    params = {'sessionId': 'fixture', 'update': update}
    driver._on_notification(state, 'session/update', params)
    translator.on_session_update.assert_called_once_with({'sessionId': 'fixture', 'update': filtered})
    assert params['update'] is update


async def test_projection_remember_hook_receives_latest_servers_and_empty_native_scope(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))
    from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
    from app.conversations.models import Conversation
    from app.projects.models import AgentBinding
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    binding = AgentBinding.create(project='fixture', backend='acp')
    driver.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture')
    remember = Mock()
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(remember_session_options=remember))
    effective = EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type='mcp', capability_id='selected', source_project_id=binding.project_id,
        inherited=False, config={'command':'synthetic-server'}),))
    projection = await driver.session_options_for(conversation, effective)
    remember.assert_called_once_with(driver.agent_spec, conversation, projection.options.metadata)
    assert remember.call_args.args[2]['mcpServers'][0]['command'] == 'synthetic-server'
    empty = EffectiveCapabilities(project_id=binding.project_id, entries=())
    assert await driver.session_options_for(conversation, empty) is None
    assert remember.call_args.args == (driver.agent_spec, conversation, {})


async def test_native_history_hook_receives_binding_and_exact_session(monkeypatch):
    from app.projects.models import AgentBinding
    from drivers.base import NativeHistory, NativeHistoryEntry
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    binding = AgentBinding.create(project='fixture', backend='acp')
    driver.register_binding(binding)
    history = NativeHistory(native_session_id='native-id', entries=(NativeHistoryEntry(
        entry_id='stable-id', role='user', text='synthetic terminal input'),))
    loader = AsyncMock(return_value=history)
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(load_native_history=loader))
    assert await driver.load_native_history(binding, 'native-id') == history
    loader.assert_awaited_once_with(driver.agent_spec, binding, 'native-id')


def test_native_history_capability_is_declared_only_for_adapter_reader(monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    result = {'protocolVersion':1, 'agentCapabilities':{'loadSession':True}}
    monkeypatch.setattr(driver, '_skill_adapter', lambda: None)
    driver._absorb_initialize(result)
    assert not driver._capabilities.sessions.history
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(load_native_history=AsyncMock()))
    driver._absorb_initialize(result)
    assert driver._capabilities.sessions.history
    assert driver._capabilities.sessions.history.verification == 'declared'


async def test_external_precreation_marks_runtime_options_without_mutating_project_options(monkeypatch):
    from drivers.base import CreateSessionOptions
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    options = CreateSessionOptions(metadata={'mcpServers': []})
    precreate = AsyncMock(return_value='created')
    monkeypatch.setattr('drivers.external_session.precreate_external_session', precreate)
    conversation = object()
    assert await driver.create_external_native_session(conversation, options) == 'created'
    called_driver, called_conversation, marked = precreate.call_args.args
    assert called_driver is driver and called_conversation is conversation
    assert marked.metadata == {'mcpServers': [], '_kausExternalPrecreate': True}
    assert options.metadata == {'mcpServers': []}
