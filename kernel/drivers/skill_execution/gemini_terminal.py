"""Carry the latest project configuration into Gemini's native resume command."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import state_root
from .node_package import locate
from .terminal_settings import approval, arguments, choice, environment, immutable, selected


def _current(spec, conversation):
    root = state_root(spec) / 'gemini-terminal' / hashlib.sha256(conversation.id.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录无效')
    path = root / 'current.json'
    if path.is_symlink():
        raise UnsupportedCapabilityError('终端配置文件无效')
    return path


def remember_session_options(spec, conversation, metadata):
    path = _current(spec, conversation)
    atomic_write_text(path, json.dumps({'conversationId': conversation.id,
        'bindingId': conversation.agent_binding_id, 'mcpServers': metadata.get('mcpServers', [])},
        ensure_ascii=False, sort_keys=True))
    path.chmod(0o600)


def cli_command(plan, command):
    # The external hook carries the process-local Skill guard in addition to
    # model, approval and current MCP configuration.
    return tuple(command)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .gemini import loader_module

    mode = choice(approval(conversation), {
        'ask': 'default', 'default': 'default', 'auto': 'auto_edit',
        'autoEdit': 'auto_edit', 'auto_edit': 'auto_edit', 'bypass': 'yolo',
        'yolo': 'yolo', 'plan': 'plan',
    }, '权限模式') or 'default'
    effort = selected(conversation, 'reasoning_mode', 'reasoning', 'thought_level')
    if effort not in (None, 'default'):
        raise UnsupportedCapabilityError('Gemini 终端未提供独立推理档位')
    model = selected(conversation, 'model_id', 'model')
    launch_spec = plan.spec if plan is not None and plan.spec is not None else spec
    current = _current(spec, conversation)
    try:
        payload = json.loads(current.read_text())
    except (OSError, ValueError):
        raise UnsupportedCapabilityError('终端配置尚未应用，请重新打开会话') from None
    if payload.get('conversationId') != conversation.id or payload.get('bindingId') != conversation.agent_binding_id:
        raise UnsupportedCapabilityError('终端配置归属不匹配')
    package = locate(launch_spec, '@google/gemini-cli')
    core = loader_module(package) if package else None
    try:
        source = core.read_text() if core else ''
        required = ('async initialize() {', 'setMcpServers(mcpServers) {',
                    'getMcpClientManager() {', 'async waitForMcpInit() {', 'getApprovalMode() {')
        if not source or any(anchor not in source for anchor in required):
            raise ValueError('unsupported native interface')
        payload.update(package=str(package.resolve()), core=str(core),
            coreHash=hashlib.sha256(source.encode()).hexdigest(), approvalMode='autoEdit' if mode == 'auto_edit' else mode,
            model=model, sessionId=conversation.native_session_id)
        from .gemini_session import empty_session_scope
        payload.update(empty_session_scope(package, conversation.native_session_id))
    except (OSError, ValueError):
        raise UnsupportedCapabilityError('当前 Gemini 版本的终端配置接口需要重新适配') from None
    manifest = immutable(spec, conversation, json.dumps(payload, ensure_ascii=False, sort_keys=True), suffix='.json')
    guard_source = Path(__file__).with_name('gemini_terminal_guard.mjs').read_text()
    guard = immutable(spec, conversation, guard_source, suffix='.mjs')
    options = launch_spec.build_env().get('NODE_OPTIONS', '')
    options = (options + ' --import=' + guard.as_uri()).strip()
    values = {'NODE_OPTIONS': options, 'KAUS_GEMINI_TERMINAL_SCOPE': str(manifest)}
    if plan is not None:
        for key in ('KAUS_GEMINI_SKILL_SCOPE',):
            if key in launch_spec.env:
                values[key] = launch_spec.env[key]
    command = arguments(command, ('--model', model), ('--approval-mode', mode))
    return environment(spec, conversation, command, values)
