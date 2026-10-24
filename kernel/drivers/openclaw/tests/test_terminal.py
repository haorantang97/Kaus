from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from app.projects.models import AgentBinding
from app.conversations.models import Conversation
from drivers.acp.client import AcpAgentSpec, AcpTransportError
from drivers.acp.presets import get_preset
from drivers.openclaw.driver import OpenClawAcpDriver, OpenClawGateway
from drivers.base import CreateSessionOptions, UnsupportedCapabilityError


def prepared(tmp_path, monkeypatch):
    cli = tmp_path / 'native'; cli.write_text('#!/bin/sh\nexit 0\n'); cli.chmod(0o700)
    credential = tmp_path / 'credential'; credential.write_text('synthetic-private-credential')
    spec = AcpAgentSpec(command=(str(cli), 'acp', '--url', 'ws://127.0.0.1:12345', '--token-file', str(credential)),
        env={'HOME': str(tmp_path), 'OPENCLAW_STATE_DIR': str(tmp_path/'state'), 'PATH': '/usr/bin'}, inherit_env=False, cwd=str(tmp_path))
    driver = OpenClawAcpDriver(spec, backend_key='openclaw', preset=get_preset('openclaw'))
    binding = AgentBinding.create(project='terminal', backend='openclaw').model_copy(update={'runtime_config':{'workspace_root':str(tmp_path)}})
    driver.register_binding(binding)
    conv = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture').model_copy(
        update={'native_session_id':'acp-fixture', 'model_id':'provider/new', 'reasoning_mode':'low', 'approval_mode':'native:guarded'})
    handle = SimpleNamespace(native_session_id='acp-fixture')
    state = SimpleNamespace(connection=object(), session_id='acp-fixture', skill_plan=None,
        session_result={'_kausOpenClawModels':{'key':'agent:owned:native-key', 'sessionId':'native-transcript'}})
    monkeypatch.setattr(driver, 'start_runtime', AsyncMock(return_value=handle))
    monkeypatch.setattr(driver, 'stop_runtime', AsyncMock())
    monkeypatch.setattr(driver, '_state', lambda h: state)
    monkeypatch.setattr(driver, '_ensure_gateway_identity', AsyncMock())
    return driver, conv, state, credential


async def test_terminal_uses_gateway_key_and_private_target_after_current_settings(tmp_path, monkeypatch):
    driver, conv, state, credential = prepared(tmp_path, monkeypatch)
    launch = await driver.build_external_cli_launch(conv)
    assert launch.command[1:] == ('tui','--session','agent:owned:native-key','--thinking','low')
    assert 'acp-fixture' not in launch.command
    assert driver.start_runtime.call_args.args[0] == conv
    assert launch.private_env['OPENCLAW_GATEWAY_TOKEN'] == credential.read_text()
    assert launch.private_env['OPENCLAW_GATEWAY_URL'] == 'ws://127.0.0.1:12345'
    assert launch.config_directory == ('OPENCLAW_STATE_DIR', str(tmp_path / 'state'))
    assert credential.read_text() not in launch.model_dump_json() and credential.read_text() not in repr(launch)
    driver.stop_runtime.assert_awaited_once()


async def test_owned_scope_must_match_actual_gateway_route_and_failure_closes_runtime(tmp_path, monkeypatch):
    driver, conv, state, _ = prepared(tmp_path, monkeypatch)
    state.skill_plan = SimpleNamespace(session_key='agent:other:kaus')
    validate = AsyncMock()
    monkeypatch.setattr('drivers.openclaw.skill_adapter.validate_turn', validate)
    with pytest.raises(AcpTransportError, match='技能范围'):
        await driver.build_external_cli_launch(conv)
    validate.assert_awaited_once()
    driver.stop_runtime.assert_awaited_once()


async def test_unconfirmed_model_or_unavailable_gateway_never_returns_launch(tmp_path, monkeypatch):
    driver, conv, state, _ = prepared(tmp_path, monkeypatch)
    state.session_result['_kausOpenClawModels']['selectionPending'] = True
    with pytest.raises(AcpTransportError, match='确认'):
        await driver.build_external_cli_launch(conv)
    driver.stop_runtime.assert_awaited_once()
    driver.start_runtime.reset_mock(); driver.gateway = None
    with pytest.raises(UnsupportedCapabilityError):
        await driver.build_external_cli_launch(conv)
    driver.start_runtime.assert_not_awaited()


async def test_precreate_establishes_gateway_identity_without_prompt(tmp_path, monkeypatch):
    driver, conv, state, _ = prepared(tmp_path, monkeypatch)
    result = await driver.create_external_native_session(conv.model_copy(update={'native_session_id':None}), CreateSessionOptions(title='Synthetic'))
    assert result.native_session_id == 'acp-fixture'
    assert driver._ensure_gateway_identity.call_args.kwargs == {'resumed':False}
    driver.stop_runtime.assert_awaited_once()


def test_gateway_credentials_file_failure_never_falls_back_to_other_auth(tmp_path):
    gateway = OpenClawGateway(AcpAgentSpec(command=('openclaw','acp','--token-file',str(tmp_path/'missing'))))
    with pytest.raises(AcpTransportError,match='凭据文件'):
        gateway.connection_environment()
