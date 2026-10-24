from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.conversations.models import Conversation
from drivers.acp.client import AcpAgentSpec, AcpTransportError
from drivers.acp.driver import AcpDriver
from drivers.acp.native_cli import native_path
from drivers.acp.presets import AcpPreset, SESSION_ID_PLACEHOLDER
from drivers.base import UnsupportedCapabilityError


def executable(tmp_path, name):
    path = tmp_path / name
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return str(path)


def driver_for(tmp_path, **overrides):
    preset = AcpPreset(id='synthetic', label='Synthetic', executable='fixture-acp',
        command=('fixture-acp',), native_executable='fixture-native',
        native_executable_env='FIXTURE_NATIVE_PATH', resume_argv_template=('fixture-native', 'resume', SESSION_ID_PLACEHOLDER),
        skill_projection_adapter=None, **overrides)
    spec = AcpAgentSpec(command=('fixture-acp',), env={'PATH': str(tmp_path)}, inherit_env=False)
    return AcpDriver(spec, backend_key='fixture', preset=preset)


@pytest.mark.parametrize('selection', ['env', 'assembly', 'flag'])
async def test_terminal_uses_selected_native_installation_with_custom_filename(tmp_path, selection):
    selected = executable(tmp_path, 'my-engine-version')
    executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path)
    if selection == 'env':
        driver.agent_spec = driver.agent_spec.with_env(FIXTURE_NATIVE_PATH=selected)
    elif selection == 'assembly':
        driver.assembly_spec = SimpleNamespace(options={'native_cli_path': selected})
    else:
        driver.preset = replace(driver.preset, native_cli_flag='--native-cli')
        driver.agent_spec = driver.agent_spec.model_copy(update={'command': ('fixture-acp', '--native-cli', selected)})
    conversation = Conversation.create(project_id='project:test', agent_binding_id='binding:test:fixture', title='Synthetic').evolve(native_session_id='native-id')
    assert native_path(driver) == selected
    launch = await driver.build_external_cli_launch(conversation)
    assert launch.command == (selected, 'resume', 'native-id')


async def test_missing_explicit_engine_never_falls_back_to_other_installation(tmp_path):
    executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path)
    driver.agent_spec = driver.agent_spec.with_env(FIXTURE_NATIVE_PATH=str(tmp_path / 'removed'))
    conversation = Conversation.create(project_id='project:test', agent_binding_id='binding:test:fixture', title='Synthetic').evolve(native_session_id='native-id')
    with pytest.raises(UnsupportedCapabilityError, match='未安装'):
        await driver.build_external_cli_launch(conversation)


def test_direct_native_command_path_is_reused_without_resolving_symlink(tmp_path):
    target = executable(tmp_path, 'actual-engine')
    link = tmp_path / 'my-engine'
    link.symlink_to(target)
    driver = driver_for(tmp_path)
    driver.preset = replace(driver.preset, native_executable='fixture-acp')
    driver.agent_spec = driver.agent_spec.model_copy(update={'command': (str(link), 'acp')})
    assert native_path(driver) == str(link)


def test_absolute_bridge_path_is_not_used_as_native_executable(tmp_path):
    native = executable(tmp_path, 'fixture-native')
    bridge = executable(tmp_path, 'fixture-acp')
    driver = driver_for(tmp_path)
    driver.agent_spec = driver.agent_spec.model_copy(update={'command': (bridge,)})
    assert native_path(driver) == native


async def test_failed_service_handshake_does_not_mean_native_engine_is_uninstalled(tmp_path, monkeypatch):
    native = executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path)
    monkeypatch.setattr(driver, '_connect', AsyncMock(side_effect=AcpTransportError('Synthetic service offline')))
    result = await driver.probe()
    assert result.installed and result.state == 'unavailable'
    assert native_path(driver) == native
    assert 'offline' in result.message


def test_connection_refused_does_not_recommend_reinstalling_engine():
    from drivers.acp.failure_hints import spawn_failed
    failure = spawn_failed(('fixture-acp',), cause='exit 1',
        stderr='ACP bridge failed: connect ECONNREFUSED 127.0.0.1:12345\nsecret-token')
    assert failure.code == 'agent_service_unavailable'
    assert '服务' in failure.describe()
    assert '重装' not in failure.describe() and 'secret-token' not in failure.describe()


@pytest.mark.parametrize('managed', [False, True])
async def test_terminal_adapter_receives_session_configuration_and_selected_installation(tmp_path, monkeypatch, managed):
    native = executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path)
    conversation = Conversation.create(project_id='project:test', agent_binding_id='binding:test:fixture',
        title='Synthetic').evolve(native_session_id='native-id', model_id='selected-model',
        reasoning_mode='high', execution_mode='plan', approval_mode='read_only')
    plan = object() if managed else None
    if managed:
        driver._skill_plans[conversation.id] = plan
    hook = Mock(return_value=(native, 'resume', 'native-id', '--model', 'selected-model'))
    legacy = Mock(return_value=())
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(external_cli_command=hook, cli_command=legacy))
    launch = await driver.build_external_cli_launch(conversation)
    hook.assert_called_once_with(plan, (native, 'resume', 'native-id'), conversation=conversation,
        binding=None, spec=driver.agent_spec)
    assert launch.command[-2:] == ('--model', 'selected-model')
    if managed:
        legacy.assert_called_once_with(plan, ())  # Existing preflight scope check.
    else:
        legacy.assert_not_called()


async def test_terminal_keeps_prepared_loader_and_native_home(tmp_path, monkeypatch):
    native = executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path, config_root_env='FIXTURE_HOME', config_root_default='~/.fixture')
    driver.agent_spec = driver.agent_spec.with_env(FIXTURE_HOME=str(tmp_path / 'native-history'))
    prepared = driver.agent_spec.with_env(KAUS_FIXTURE_SCOPE=str(tmp_path / 'selected-skills'),
                                          NODE_OPTIONS='--require=/selected-loader.cjs')
    conversation = Conversation.create(project_id='project:test', agent_binding_id='binding:test:fixture',
        title='Synthetic').evolve(native_session_id='native-id')
    driver._skill_plans[conversation.id] = SimpleNamespace(spec=prepared)
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(cli_command=lambda plan, command: command))
    launch = await driver.build_external_cli_launch(conversation)
    assert launch.command == (native, 'resume', 'native-id')
    assert launch.config_directory == ('FIXTURE_HOME', str(tmp_path / 'native-history'))
    assert launch.private_env['NODE_OPTIONS'] == '--require=/selected-loader.cjs'
    assert launch.private_env['KAUS_FIXTURE_SCOPE'] == str(tmp_path / 'selected-skills')
    assert driver.agent_spec.env.get('NODE_OPTIONS') is None


async def test_terminal_adapter_can_prepare_shared_store_and_private_config(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    native = executable(tmp_path, 'fixture-native')
    driver = driver_for(tmp_path)
    conversation = Conversation.create(project_id='project:test',agent_binding_id='binding:test:fixture',
        title='Synthetic').evolve(native_session_id='native-id')
    prepared = driver.agent_spec.with_env(KAUS_FIXTURE_TOKEN='synthetic-secret')
    hook = AsyncMock(return_value=((native,'resume','native-id','--project'),prepared))
    monkeypatch.setattr(driver,'_skill_adapter',lambda: SimpleNamespace(prepare_terminal_launch=hook))
    launch = await driver.build_external_cli_launch(conversation)
    hook.assert_awaited_once_with((native,'resume','native-id'),driver.agent_spec,{},conversation=conversation,binding=None)
    assert launch.command[-1] == '--project'
    assert launch.private_env['KAUS_FIXTURE_TOKEN'] == 'synthetic-secret'
    assert 'synthetic-secret' not in ' '.join(launch.command)
    assert 'KAUS_FIXTURE_TOKEN' not in driver.agent_spec.env
