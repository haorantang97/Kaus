import hashlib
import json
from pathlib import Path

import pytest

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.base import UnsupportedCapabilityError
from drivers.hermes import terminal_settings as settings
from drivers.hermes.terminal_runtime_guard import mcp_config, verify_mcp
from types import SimpleNamespace
import threading


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'hermes_python', lambda _: '/native/python')
    binding = AgentBinding.create(project='fixture', backend='hermes')
    conversation = Conversation.create(title='Synthetic', project_id=binding.project_id,
        agent_binding_id=binding.id).evolve(native_session_id='same-id', model_id='selected',
        provider_id='custom:fixture', reasoning_mode='high', approval_mode='auto')
    return binding, conversation


def test_launch_has_immutable_conversation_settings_and_no_secret(tmp_path, monkeypatch):
    binding, conversation = setup(tmp_path, monkeypatch)
    servers = [{'name': 'test', 'command': 'server', 'args': [],
                'env': [{'name': 'TOKEN', 'value': 'synthetic-secret'}]}]
    settings.remember_mcp(tmp_path, conversation, servers)
    args = settings.command_for(tmp_path, conversation, binding,
                                ('hermes', 'chat', '--resume', 'same-id'), 'hermes')
    assert 'synthetic-secret' not in repr(args)
    payload = json.loads(Path(args[3]).read_text())
    assert hashlib.sha256(Path(args[3]).read_bytes()).hexdigest() == args[4]
    assert payload['editPolicy'] == 'workspace_session'
    assert payload['reasoning'] == 'high' and payload['model'] == 'selected'
    assert Path(args[3]).stat().st_mode & 0o777 == 0o600
    settings.remember_mcp(tmp_path, conversation, [])
    next_args = settings.command_for(tmp_path, conversation.evolve(reasoning_mode='low'), binding,
                                    ('hermes', 'chat', '--resume', 'same-id'), 'hermes')
    assert next_args[3] != args[3] and json.loads(Path(args[3]).read_text()) == payload
    assert json.loads(Path(next_args[3]).read_text())['mcpServers'] == []


def test_unknown_modes_and_missing_projection_never_fall_back(tmp_path, monkeypatch):
    binding, conversation = setup(tmp_path, monkeypatch)
    with pytest.raises(UnsupportedCapabilityError, match='尚未应用'):
        settings.command_for(tmp_path, conversation, binding, ('hermes', 'chat'), 'hermes')
    settings.remember_mcp(tmp_path, conversation, [])
    for mode in ('host:auto', 'host:ask', 'bypass', 'read_only', 'plan', 'native:unknown'):
        with pytest.raises(UnsupportedCapabilityError, match='权限模式'):
            settings.command_for(tmp_path, conversation.evolve(approval_mode=mode), binding,
                                 ('hermes', 'chat'), 'hermes')
    assert settings.edit_policy('native:dont_ask') == 'session'
    assert settings.edit_policy('native:accept_edits') == 'workspace_session'


def test_native_registration_failure_cannot_be_hidden_by_stale_same_name():
    config = mcp_config([{'name': 'test', 'command': '/current', 'args': [], 'env': []}])
    server = SimpleNamespace(session=object(), _config={'command': '/old', 'args': [], 'env': {}})
    module = SimpleNamespace(_lock=threading.RLock(), _servers={'test': server})
    with pytest.raises(RuntimeError, match='MCP 连接失败'):
        verify_mcp(module, config)
    server._config = config['test']
    verify_mcp(module, config)
    with pytest.raises(RuntimeError, match='范围'):
        verify_mcp(module, {})
