import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import CreateSessionOptions, SessionProjection, UnsupportedCapabilityError
from drivers.hermes import isolated_runtime, skill_adapter
from drivers.hermes.driver import HermesDriver
from drivers.hermes.mcp_runtime_guard import verify_registered
from runtime.capability_matrix import BackendCapabilities


@pytest.mark.parametrize('role', [None, 'planning'])
async def test_project_mcp_uses_private_protocol_for_ordinary_and_group(tmp_path, monkeypatch, role):
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'config.yaml').write_text('{}\n')
    owner = HermesDriver(hermes_root=native)
    owner._capabilities = BackendCapabilities()
    binding = AgentBinding.create(project='mcp', backend='hermes', runtime_config={
        'project_execution': 'isolated', **({'group_execution': role} if role else {})})
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='Synthetic')
    effective = EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type='mcp', capability_id='synthetic', config={'command': 'example'},
        source_project_id=binding.project_id, inherited=False),))
    projection = SessionProjection(options=CreateSessionOptions(metadata={'mcpServers': [{'name': 'synthetic'}]}),
                                   summary={'mcpServers': ['synthetic']})
    delegate = SimpleNamespace(session_options_for=AsyncMock(return_value=projection))
    monkeypatch.setattr(isolated_runtime, 'create_driver', lambda *_: delegate)
    result = await owner.session_options_for(conversation, effective)
    assert result.options.metadata['mcpServers'] == [{'name': 'synthetic'}]
    assert result.summary['mcpServers'] == ['synthetic']
    assert not result.warnings
    assert (native / 'config.yaml').read_text() == '{}\n'
    delegate.session_options_for.assert_awaited_once()


async def test_native_without_project_mcp_retains_original_spec():
    spec = AcpAgentSpec(command=('custom-hermes-launcher', 'acp'))
    assert await skill_adapter.prepare_runtime_spec(spec, None, {}) is spec


async def test_legacy_native_connection_does_not_silently_discard_explicit_project_mcp(tmp_path):
    owner = HermesDriver(hermes_root=tmp_path)
    binding = AgentBinding.create(project='legacy', backend='hermes', native_scope_ref='default')
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='Synthetic')
    empty = EffectiveCapabilities(project_id=binding.project_id, entries=())
    assert await owner.session_options_for(conversation, empty) is None
    mcp = empty.model_copy(update={'entries': (EffectiveCapability(
        capability_type='mcp', capability_id='synthetic', config={'command': 'example'},
        source_project_id=binding.project_id, inherited=False),)})
    with pytest.raises(UnsupportedCapabilityError, match='独立执行'):
        await owner.session_options_for(conversation, mcp)


@pytest.mark.parametrize('launcher', ['skill_launcher.py', 'group_launcher.py'])
async def test_mcp_guard_composes_with_skill_and_group_launchers(launcher):
    path = str(Path(skill_adapter.__file__).with_name(launcher))
    spec = AcpAgentSpec(command=('/native/python', '-B', path, 'planning'), env={'PRESERVE': 'yes'})
    prepared = await skill_adapter.prepare_runtime_spec(spec, None, {'mcpServers': [{'name': 'test'}]})
    assert prepared.command[3:] == (path, 'planning')
    assert Path(prepared.command[2]).name == 'mcp_launcher.py'
    assert prepared.env['PRESERVE'] == 'yes'
    assert json.loads(prepared.env['KAUS_HERMES_MCP_NAMES']) == ['test']


async def test_mcp_guard_does_not_assume_custom_shell_launcher_support():
    with pytest.raises(UnsupportedCapabilityError, match='启动器'):
        await skill_adapter.prepare_runtime_spec(AcpAgentSpec(command=('custom',)), None,
                                                {'mcpServers': [{'name': 'test'}]})


def fixture():
    request = SimpleNamespace(name='fixture', command='/native/python', args=['server'],
                              env=[SimpleNamespace(name='TOKEN', value='synthetic-secret')])
    server = SimpleNamespace(session=object(), _config={'command': '/native/python', 'args': ['server'],
                              'env': {'TOKEN': 'synthetic-secret'}}, _registered_tool_names=['fixture_tool'])
    module = SimpleNamespace(_lock=threading.RLock(), _servers={'fixture': server})
    state = SimpleNamespace(agent=SimpleNamespace(valid_tool_names={'fixture_tool'}))
    return module, state, request, server


def test_actual_connection_and_agent_tool_surface_required():
    module, state, request, server = fixture()
    verify_registered(module, state, [request])
    server._registered_tool_names = []
    state.agent.valid_tool_names = set()
    verify_registered(module, state, [request])  # A valid zero-tool server is allowed.


def test_native_deferred_tools_are_accepted_only_from_current_scope():
    module, state, request, server = fixture()
    state.agent.valid_tool_names = {'tool_search', 'tool_describe', 'tool_call'}
    verify_registered(module, state, [request], deferred_names={'fixture_tool'})
    with pytest.raises(RuntimeError, match='工具未加载'):
        verify_registered(module, state, [request], deferred_names={'other_tool'})


async def test_metadata_guard_is_idempotent_and_receives_later_session_names():
    path = str(Path(skill_adapter.__file__).with_name('skill_launcher.py'))
    spec = AcpAgentSpec(command=('/native/python', '-B', path, 'acp'),
                       env={'HERMES_ACP_SKIP_CONFIGURED_MCP': '1'})
    metadata_spec = skill_adapter.guarded_spec(spec, [])
    assert Path(metadata_spec.command[2]).name == 'mcp_launcher.py'
    runtime_spec = await skill_adapter.prepare_runtime_spec(metadata_spec, None,
                                                          {'mcpServers': [{'name': 'late'}]})
    assert runtime_spec.command == metadata_spec.command
    assert json.loads(runtime_spec.env['KAUS_HERMES_MCP_NAMES']) == ['late']


@pytest.mark.parametrize('failure', ['absent', 'disconnected', 'stale_config', 'missing_tools'])
def test_swallowed_native_errors_or_stale_same_name_cannot_pass(failure):
    module, state, request, server = fixture()
    if failure == 'absent':
        module._servers.clear()
    elif failure == 'disconnected':
        server.session = None
    elif failure == 'stale_config':
        server._config['args'] = ['previous-config']
    else:
        state.agent.valid_tool_names = set()
    with pytest.raises(RuntimeError) as error:
        verify_registered(module, state, [request])
    assert 'fixture' in str(error.value)
    assert 'synthetic-secret' not in str(error.value)
