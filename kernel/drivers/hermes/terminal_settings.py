"""Conversation-owned native terminal settings; launch records contain no secrets."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.hermes.group_execution import hermes_python
from drivers.projection_store import atomic_write_text


def _directory(home, conversation_id):
    root = Path(home) / '.kaus-terminal-settings'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / hashlib.sha256(conversation_id.encode()).hexdigest()
    directory.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or directory.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录无效')
    return directory


def remember_mcp(home, conversation, servers):
    directory = _directory(home, conversation.id)
    value = {'conversationId': conversation.id, 'bindingId': conversation.agent_binding_id,
             'mcpServers': list(servers)}
    target = directory / 'current.json'
    if target.is_symlink():
        raise UnsupportedCapabilityError('终端配置文件无效')
    atomic_write_text(target, json.dumps(value, ensure_ascii=False, sort_keys=True))
    target.chmod(0o600)


def edit_policy(mode):
    if mode is None:
        return 'ask'  # Hermes ACP session default.
    policies = {'ask': 'ask', 'auto': 'workspace_session',
                'native:default': 'ask', 'native:accept_edits': 'workspace_session',
                'native:dont_ask': 'session'}
    if mode not in policies:
        raise UnsupportedCapabilityError('当前权限模式无法在 Hermes 终端中保持')
    return policies[mode]


def command_for(home, conversation, binding, command, hermes_bin):
    policy = edit_policy(conversation.approval_mode)
    directory = _directory(home, conversation.id)
    current = directory / 'current.json'
    if current.is_symlink() or not current.is_file():
        raise UnsupportedCapabilityError('终端配置尚未应用，请重新打开会话')
    payload = json.loads(current.read_text())
    if payload.get('conversationId') != conversation.id or payload.get('bindingId') != binding.id:
        raise UnsupportedCapabilityError('终端配置归属不匹配')
    payload.update(model=conversation.model_id, provider=conversation.provider_id,
                   reasoning=conversation.reasoning_mode, editPolicy=policy,
                   workspace=binding.runtime_config.get('workspace_root'), home=str(Path(home).resolve()))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    target = directory / (digest + '.json')
    if target.exists():
        if target.is_symlink() or target.read_text() != raw:
            raise UnsupportedCapabilityError('终端配置校验失败')
    else:
        atomic_write_text(target, raw)
        target.chmod(0o600)
    python = hermes_python(AcpAgentSpec(command=(hermes_bin,)))
    if not python:
        raise UnsupportedCapabilityError('无法找到 Hermes 终端运行环境')
    if len(command) > 2 and command[1] == '-B' and Path(command[2]).name == 'skill_launcher.py':
        target_command, args = command[2], command[3:]
    elif command and command[0] == hermes_bin:
        target_command, args = 'native', command[1:]
    else:
        raise UnsupportedCapabilityError('此 Hermes 启动器尚未提供终端配置接续')
    return (python, '-B', str(Path(__file__).with_name('terminal_launcher.py')),
            str(target), digest, target_command, *args)
