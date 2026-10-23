"""Gateway-owned, conversation-specific Agent skill scopes.

Only a Kaus-owned Agent entry is written via the Gateway's CAS API. Native
Agents, provider settings and global skill directories are never rewritten.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import stat
from urllib.parse import urlparse
from pathlib import Path
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from drivers.skill_execution.common import materialize, state_root, selection_of
from drivers.acp.mcp_projection import McpProjection, project_mcp_servers
from drivers.session_mcp import projection as session_mcp
from drivers.computer_use.policy import project_controls
from .driver import OpenClawGateway
from .computer_use import (inherited_agent_policy, probe_computer_use, stop_computer_use,
                          confirm_computer_use_complete, computer_use_reference, recover_computer_use)


COMPUTER_USE_POLICY_SCOPES = frozenset({'browser', 'desktop'})
_runtime_contexts = {}

def project_scope_selected(effective):
    return selection_of(effective) is not None or any(
        entry.capability_type == 'mcp' for entry in effective.entries)


def native_computer_use_permission_control(effective, metadata=None, *, conversation=None):
    """Native source permissions remain with OpenClaw; owned scopes are controllable."""
    if project_scope_selected(effective):
        return "kaus"
    context = _runtime_contexts.get(getattr(conversation, 'id', None))
    if context is not None:
        _binding, _effective, plans, _workspace_root, workspace_scoped = context
        existing = plans.get(conversation.id)
        if workspace_scoped or (existing is not None and not getattr(existing, 'source_session', False)):
            return "kaus"
    session_key = (metadata or {}).get('_meta', {}).get('sessionKey', '')
    if session_key.startswith('agent:kaus-'):
        return "kaus"
    return "agent"


def computer_use_fallback_allowed(effective, metadata=None):
    # Source sessions use an owned MCP overlay without replacing their Agent.
    return True


def remember_runtime_context(conversation, binding, effective, plans, workspace_root=None, *, workspace_scoped=None):
    if workspace_scoped is None:
        workspace_scoped = bool(workspace_root)
    _runtime_contexts[conversation.id] = (binding, effective, plans, workspace_root, workspace_scoped)


def forget_runtime_context(conversation_id):
    _runtime_contexts.pop(conversation_id, None)


async def prepare_runtime_spec(spec, conversation, metadata):
    """Apply the final tool selection even when no project Skill was selected."""
    context = _runtime_contexts.get(conversation.id)
    computer_use = metadata.get('_kausComputerUse', {})
    controls = computer_use.get('controls', {})
    off = any(controls.get(kind) is False for kind in COMPUTER_USE_POLICY_SCOPES)
    needs_owned = off or bool(computer_use.get('resources'))
    if context is None:
        if needs_owned or metadata.get('mcpServers'):
            raise UnsupportedCapabilityError('无法确定 OpenClaw 项目范围')
        return spec
    binding, effective, plans, workspace_root, _workspace_scoped = context
    existing = plans.get(conversation.id)
    already_scoped = (existing is not None and
        metadata.get('_meta', {}).get('sessionKey') == getattr(existing, 'session_key', None))
    servers = metadata.get('mcpServers')
    if already_scoped and servers is None:
        servers = getattr(existing, 'runtime_mcp_servers', None)
    expected_controls = {**project_controls(effective), **controls}
    if (already_scoped and tuple(servers or ()) == getattr(existing, 'runtime_mcp_servers', ())
            and expected_controls == getattr(existing, 'computer_controls', None)):
        await validate_turn(existing, metadata.get('_kausWorkspaceRoot') or workspace_root or spec.cwd)
        metadata.pop('mcpServers', None)
        if getattr(existing, 'source_session', False):
            metadata['_kausOpenClawOwnership'] = existing.ownership
        return spec
    if not needs_owned and not servers and existing is None:
        return spec
    workspace = metadata.get('_kausWorkspaceRoot') or workspace_root
    private_scope = (existing is not None and not getattr(existing, 'source_session', False)
        and getattr(existing, 'agent_id', None)) or project_scope_selected(effective)
    if not private_scope:
        from .source_session import prepare
        plan = await prepare(spec, binding, effective, conversation, expected_controls)
        if plan is None:
            from .source_mcp import discard_automatic_fallback
            discard_automatic_fallback(metadata)
            if metadata.get('mcpServers'):
                raise UnsupportedCapabilityError('当前原生会话无法添加会话工具，请新建对话')
            return spec
        from .source_mcp import prepare as prepare_source_mcp
        await prepare_source_mcp(plan, conversation, servers or ())
    else:
        plan = await project_session(spec, binding, effective,
            workspace or spec.cwd, conversation=conversation, force_workspace=True,
            runtime_mcp=servers, computer_controls=controls)
    plans[conversation.id] = plan
    if existing is not None and existing is not plan and getattr(existing, 'gateway', None):
        await existing.gateway.close()
    metadata['_meta'] = {**metadata.get('_meta', {}), **plan.metadata}
    if getattr(plan, 'source_session', False):
        metadata['_kausOpenClawOwnership'] = plan.ownership
    metadata.pop('mcpServers', None)
    return plan.spec


class ScopeGateway(OpenClawGateway):
    allowed_methods = OpenClawGateway.allowed_methods | {'config.get', 'config.patch', 'skills.status'}

    def __init__(self, spec, agent_id, **kwargs):
        super().__init__(spec, **kwargs)
        self.agent_id = agent_id

    def validate_session_patch(self, params):
        if 'toolOverrides' not in params:
            return super().validate_session_patch(params)
        if (set(params) - {'key', 'expectedSessionId', 'expectedLifecycleRevision', 'toolOverrides', 'expectedToolOverrides'}
                or params.get('key') != f'agent:{self.agent_id}:kaus'
                or 'expectedToolOverrides' not in params
                or not params.get('expectedSessionId')):
            raise ValueError('Only the owned session MCP overlay can be changed')
        before = params.get('expectedToolOverrides') or {}
        after = params['toolOverrides']
        if {key: value for key, value in before.items() if key != 'mcpServers'} != {key: value for key, value in after.items() if key != 'mcpServers'}:
            raise ValueError('Unrelated session tools must remain unchanged')
        old_mcp, new_mcp = before.get('mcpServers', {}), after.get('mcpServers', {})
        if (not all(isinstance(value, bool) for value in new_mcp.values())
                or {name: value for name, value in old_mcp.items() if not name.startswith(self.agent_id + '-')} !=
                   {name: value for name, value in new_mcp.items() if not name.startswith(self.agent_id + '-')}):
            raise ValueError('Only owned MCP overrides can be changed')

    async def call(self, method, params):
        if method == 'config.patch':
            patch = json.loads(params.get('raw', '{}'))
            if (set(patch) - {'agents', 'mcp'} or set(patch.get('agents', {})) != {'entries'}
                    or set(patch['agents']['entries']) != {self.agent_id}
                    or not params.get('baseHash')):
                raise ValueError('Only the owned Agent entry can be changed')
            if 'mcp' in patch:
                servers = patch['mcp'].get('servers', {})
                if set(patch['mcp']) != {'servers'} or not all(name.startswith(self.agent_id + '-') and (value is None or value.get('enabled') is False) for name, value in servers.items()):
                    raise ValueError('Only disabled owned MCP entries can be changed')
        try:
            return await super().call(method, params)
        except BaseException:
            await self.close()
            raise


def _config(snapshot, gateway=None):
    value = snapshot.get('config')
    if not isinstance(value, dict) or not snapshot.get('hash'):
        raise UnsupportedCapabilityError('Gateway 未提供带版本的配置快照')
    if gateway is not None:
        registry = value.get('mcp', {}).get('servers', {})
        selected = {key: item for key, item in registry.items()
            if key.startswith(gateway.agent_id + '-') and '__OPENCLAW_REDACTED__' in json.dumps(item)}
        if selected:
            value = _read_owned_sensitive_entries(snapshot, value, gateway, selected)
    return value


def _read_owned_sensitive_entries(snapshot, config, gateway, selected):
    # The Gateway deliberately masks credentials. Compare only our own entries
    # against the same local native file; no credential is emitted or copied.
    # Remote Gateways need a future digest API instead of accepting a wildcard.
    try:
        env = gateway.spec.build_env()
        home = Path(env.get('OPENCLAW_HOME') or env.get('HOME') or Path.home()).expanduser()
        state = Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw')
        configured = Path(env.get('OPENCLAW_CONFIG_PATH') or state / 'openclaw.json').expanduser().absolute()
        if configured.is_symlink():
            raise ValueError()
        path = configured.resolve(strict=True)
        provided = Path(snapshot.get('path', ''))
        if not provided.is_absolute() or provided != path:
            raise ValueError()
        target = gateway.options.get('--url') or env.get('OPENCLAW_GATEWAY_URL')
        if target and urlparse(target).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
                raise ValueError()
            native = json.load(stream)
            after = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
        def identity(info):
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size
        if (not isinstance(native, dict) or not stat.S_ISREG(current.st_mode)
                or identity(before) != identity(after) or identity(after) != identity(current)):
            raise ValueError()
        if native.get('gateway', {}).get('mode') != 'local':
            raise ValueError()
        if target and (urlparse(target).port or 18789) != native.get('gateway', {}).get('port', 18789):
            raise ValueError()
        def matches(masked, raw):
            if masked == '__OPENCLAW_REDACTED__':
                return isinstance(raw, str) and raw != '__OPENCLAW_REDACTED__'
            if isinstance(masked, dict) and isinstance(raw, dict):
                return masked.keys() == raw.keys() and all(matches(value, raw[key]) for key, value in masked.items())
            if isinstance(masked, list) and isinstance(raw, list):
                return len(masked) == len(raw) and all(matches(a, b) for a, b in zip(masked, raw))
            return masked == raw
        raw_entries = native.get('mcp', {}).get('servers', {})
        restored = json.loads(json.dumps(config))
        for key, masked in selected.items():
            raw = raw_entries.get(key)
            if not matches(masked, raw):
                raise ValueError()
            restored['mcp']['servers'][key] = raw
        return restored
    except (OSError, ValueError, TypeError, KeyError):
        raise UnsupportedCapabilityError('当前 Gateway 无法核验 MCP 凭据配置') from None


def _entry(config, agent_id):
    agents = config.get('agents', {})
    entries = agents.get('entries', {})
    return entries.get(agent_id) if isinstance(entries, dict) else None


def _safe_roster(config, agent_id):
    agents = config.get('agents', {})
    entries = agents.get('entries', {})
    if not isinstance(entries, dict) or agents.get('list'):
        raise UnsupportedCapabilityError('请先升级原生 Agent 配置格式')
    if (agents.get('ownership') != 'explicit'
            and not any(isinstance(value, dict) and value.get('default') is True for value in entries.values())
            and any(key != agent_id for key in entries)):
        raise UnsupportedCapabilityError('请先在 OpenClaw 配置多 Agent 所有权')
    mode = config.get('gateway', {}).get('reload', {}).get('mode')
    if mode == 'off':
        raise UnsupportedCapabilityError('Gateway 已关闭配置热更新，无法应用会话技能范围')


async def validate_turn(plan, cwd):
    if getattr(plan, 'source_session', False):
        from .source_session import validate
        await validate(plan)
        return
    snapshot = await plan.gateway.call('config.get', {})
    config = _config(snapshot, plan.gateway)
    current = _entry(config, plan.agent_id)
    if current != plan.agent_config:
        raise UnsupportedCapabilityError('专属 Agent 配置已变化，请重新应用')
    registry = config.get('mcp', {}).get('servers', {})
    if any(registry.get(name) != value for name, value in getattr(plan, 'mcp_config', {}).items()):
        raise UnsupportedCapabilityError('项目 MCP 配置已变化，请重新应用')
    if hasattr(plan, 'expected_tool_overrides'):
        session = await plan.gateway.call('sessions.describe', {'key': plan.session_key})
        if (session.get('session') or {}).get('toolOverrides') != plan.expected_tool_overrides:
            raise UnsupportedCapabilityError('会话 MCP 配置已变化，请重新应用')
    if not getattr(plan, 'manage_skills', True):
        return
    description = await plan.gateway.call('sessions.describe', {'key': plan.session_key})
    row = description.get('session')
    params = {'agentId': plan.agent_id}
    if isinstance(row, dict):
        params['sessionKey'] = plan.session_key
    status = await plan.gateway.call('skills.status', params)
    if status.get('agentSkillFilter') != list(plan.names) or not isinstance(status.get('skills'), list):
        raise UnsupportedCapabilityError('Gateway 未执行专属 Agent 技能范围')
    expected = {name: str(path.resolve()) for name, path in zip(plan.names, plan.paths)}
    actual = {}
    for skill in status['skills']:
        if skill.get('blockedByAgentFilter') is not True:
            name, file = skill.get('name'), skill.get('filePath')
            if name not in expected or not isinstance(file, str) or str(Path(file).resolve()) != expected[name]:
                raise UnsupportedCapabilityError('Gateway 发现范围外技能，未启动对话')
            if not skill.get('eligible') or skill.get('disabled') or skill.get('blockedByAllowlist'):
                raise UnsupportedCapabilityError('所选技能的原生依赖或权限不可用')
            actual[name] = str(Path(file).resolve())
    if actual != expected:
        raise UnsupportedCapabilityError('Gateway 未完整加载所选技能')


async def apply_session_mcp(plan):
    if getattr(plan, 'source_session', False) and not plan.mcp_overrides:
        return
    description = await plan.gateway.call('sessions.describe', {'key': plan.session_key})
    row = description.get('session')
    if not isinstance(row, dict) or not row.get('sessionId'):
        raise UnsupportedCapabilityError('原生会话尚未就绪')
    before = row.get('toolOverrides')
    after = {**(before or {}), 'mcpServers': {**(before or {}).get('mcpServers', {}), **plan.mcp_overrides}}
    if before != after:
        params = {'key': plan.session_key, 'expectedSessionId': row['sessionId'],
            'expectedToolOverrides': before, 'toolOverrides': after}
        if row.get('lifecycleRevision'):
            params['expectedLifecycleRevision'] = row['lifecycleRevision']
        await plan.gateway.call('sessions.patch', params)
    verified = await plan.gateway.call('sessions.describe', {'key': plan.session_key})
    if (verified.get('session') or {}).get('toolOverrides') != after:
        raise UnsupportedCapabilityError('原生会话未应用项目 MCP')
    plan.expected_tool_overrides = after


async def watch(plan, cwd):
    while True:
        await asyncio.sleep(2)
        await validate_turn(plan, cwd)


async def project_session(spec, binding, effective, workspace_root, *, conversation, dry_run=False, runtime_info=None, force_workspace=False, runtime_mcp=None, computer_controls=None):
    from .config_lock import configuration_lock
    if dry_run:
        return await _project_session(spec, binding, effective, workspace_root, conversation=conversation,
            dry_run=True, runtime_info=runtime_info, force_workspace=force_workspace,
            runtime_mcp=runtime_mcp, computer_controls=computer_controls)
    async with configuration_lock(spec):
        return await _project_session(spec, binding, effective, workspace_root, conversation=conversation,
            dry_run=dry_run, runtime_info=runtime_info, force_workspace=force_workspace,
            runtime_mcp=runtime_mcp, computer_controls=computer_controls)


async def _project_session(spec, binding, effective, workspace_root, *, conversation, dry_run=False, runtime_info=None, force_workspace=False, runtime_mcp=None, computer_controls=None):
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    mcp = (McpProjection(servers=tuple(dict(server) for server in runtime_mcp))
           if runtime_mcp is not None else project_mcp_servers(effective))
    controls = {**project_controls(effective), **(computer_controls or {})}
    plan.computer_controls = controls
    disabled_tools = [tool for kind, tool in (('browser', 'browser'), ('desktop', 'computer'))
                      if controls.get(kind) is False]
    plan.runtime_mcp_servers = mcp.servers
    if mcp.warnings:
        raise UnsupportedCapabilityError('项目 MCP 配置无效')
    if dry_run:
        return plan
    identity = hashlib.sha256((binding.id + ':' + conversation.id).encode()).hexdigest()[:24]
    agent_id = 'kaus-' + identity
    owner = state_root(spec) / 'openclaw-agents' / identity
    marker = owner / 'owner.json'
    provenance = {'schema': 1, 'bindingId': binding.id, 'conversationId': conversation.id, 'agentId': agent_id}
    if plan.root is None and not mcp.servers and not marker.exists() and not force_workspace and not disabled_tools:
        return plan
    plan.manage_skills = selection_of(effective) is not None
    if plan.root is None:
        plan.root = owner / 'native'
    ownership = {}
    if marker.exists():
        try:
            ownership = json.loads(marker.read_text())
        except (OSError,ValueError):
            raise UnsupportedCapabilityError('专属 Agent 归属不匹配')
        if marker.is_symlink() or any(ownership.get(key) != value for key,value in provenance.items()):
            raise UnsupportedCapabilityError('专属 Agent 归属不匹配')
    elif conversation.native_session_id:
        raise UnsupportedCapabilityError('已有原生会话尚未迁移到专属 Agent，请保留原生技能模式')
    owner.mkdir(parents=True, exist_ok=True, mode=0o700)
    if owner.is_symlink():
        raise UnsupportedCapabilityError('专属 Agent 目录不可用')
    agent_dir = plan.root / 'openclaw' / identity
    workshop = agent_dir / 'workshop-skills'
    workshop.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = []
    for name, source in zip(plan.names, plan.paths):
        target = workshop / name
        if not target.exists():
            shutil.copytree(source.parent, target)
        if target.is_symlink() or not (target / 'SKILL.md').is_file():
            raise UnsupportedCapabilityError('专属 Agent 技能副本不可用')
        paths.append(target / 'SKILL.md')
    plan.paths = tuple(paths)
    plan.agent_id = agent_id
    plan.session_key = f'agent:{agent_id}:kaus'
    plan.agent_config = {'agentDir': str(agent_dir), 'workspace': str(Path(workspace_root or spec.cwd).resolve()),
                         'cwd': str(Path(workspace_root or spec.cwd).resolve())}
    if plan.manage_skills:
        plan.agent_config['skills'] = list(plan.names)
    plan.mcp_config = {}
    plan.mcp_display_names = {}
    for server in mcp.servers:
        name = agent_id + '-' + hashlib.sha256(server['name'].encode()).hexdigest()[:12]
        value = {key: item for key, item in server.items() if key != 'name'}
        for field in ('env', 'headers'):
            if field in value:
                value[field] = {item['name']: item['value'] for item in value[field]}
        if value.get('type') == 'http':
            value['type'] = 'streamable-http'
        value['enabled'] = False
        plan.mcp_config[name] = value
        plan.mcp_display_names[name] = server['name']
    if plan.mcp_config:
        # A required service must complete its handshake before Gateway config
        # changes or any model call. The native Gateway owns actual tool calls.
        checked = session_mcp._config(spec, conversation, {'mcpServers': [dict(row) for row in mcp.servers]})
        await session_mcp.probe(spec.with_env(**{session_mcp.CONFIG_ENV: str(checked)}))
    plan.gateway = ScopeGateway(spec, agent_id)
    try:
        snapshot = await plan.gateway.call('config.get', {})
        config = _config(snapshot, plan.gateway)
        _safe_roster(config, agent_id)
        # Preserve the source Agent's explicit tool policy without copying its
        # credentials or executor settings. Project scope fields take priority.
        plan.agent_config = {**inherited_agent_policy(config, spec, binding), **plan.agent_config}
        if disabled_tools:
            tools = plan.agent_config.setdefault('tools', {})
            tools['deny'] = list(dict.fromkeys([*tools.get('deny', []), *disabled_tools]))
        previous = _entry(config, agent_id)
        if previous is not None and not marker.exists():
            raise UnsupportedCapabilityError('专属 Agent 标识已被占用')
        if marker.exists() and previous is not None and previous not in (ownership.get('appliedConfig'), ownership.get('pendingConfig')):
            raise UnsupportedCapabilityError('专属 Agent 配置已由外部修改，请重新确认归属')
        if marker.exists() and previous is None and ownership.get('appliedConfig') is not None:
            raise UnsupportedCapabilityError('专属 Agent 已被外部移除，请先恢复原生配置')
        registry = config.get('mcp', {}).get('servers', {})
        owned_before = ownership.get('appliedMcp', {})
        owned_pending = ownership.get('pendingMcp', {})
        keys = set(owned_before) | set(owned_pending) | set(plan.mcp_config)
        plan.mcp_overrides = {name: name in plan.mcp_config for name in keys}
        previous_mcp = {name: registry.get(name) for name in keys}
        for name in keys:
            value = registry.get(name)
            if value is not None and value not in (owned_before.get(name), owned_pending.get(name)):
                raise UnsupportedCapabilityError('项目 MCP 标识已被占用或由外部修改')
        def payload(entry, servers, base=config):
            # Native config.patch deep-merges objects; replacePaths handles
            # arrays only. Explicitly remove old transport/credential fields
            # when a project changes an existing service's transport.
            def replacement(value, old):
                if not isinstance(value, dict) or not isinstance(old, dict):
                    return value
                return {key: replacement(value.get(key), old.get(key))
                        for key in value.keys() | old.keys()}
            old_entry = _entry(base, agent_id)
            old_servers = base.get('mcp', {}).get('servers', {})
            patch = {'agents': {'entries': {agent_id: replacement(entry, old_entry)}}}
            if servers:
                patch['mcp'] = {'servers': {name: replacement(value, old_servers.get(name))
                                          for name, value in servers.items()}}
            paths = [f'agents.entries.{agent_id}', *[f'mcp.servers.{name}' for name in servers]]
            def array_paths(value, prefix):
                if isinstance(value, list):
                    paths.append(prefix)
                elif isinstance(value, dict):
                    for key, item in value.items():
                        array_paths(item, prefix + '.' + key)
            array_paths(entry, f'agents.entries.{agent_id}')
            array_paths(old_entry, f'agents.entries.{agent_id}')
            for name, value in servers.items():
                array_paths(value, f'mcp.servers.{name}')
                array_paths(old_servers.get(name), f'mcp.servers.{name}')
            return {'raw': json.dumps(patch), 'replacePaths': sorted(set(paths))}
        target_mcp = {name: plan.mcp_config.get(name) for name in keys}
        if previous != plan.agent_config or previous_mcp != target_mcp:
            atomic_write_text(marker, json.dumps({**provenance, 'appliedConfig': previous, 'pendingConfig': plan.agent_config,
                'appliedMcp': previous_mcp, 'pendingMcp': plan.mcp_config}, sort_keys=True))
            await plan.gateway.call('config.patch', {'baseHash': snapshot['hash'], **payload(plan.agent_config, target_mcp)})
        atomic_write_text(marker, json.dumps({**provenance, 'appliedConfig': plan.agent_config, 'appliedMcp': plan.mcp_config}, sort_keys=True))
        plan.metadata = {'sessionKey': plan.session_key}
        try:
            await validate_turn(plan, workspace_root or spec.cwd)
        except Exception:
            # Revert only if nobody changed the entry after our write. A failed or
            # conflicted rollback never launches a model or edits another Agent.
            try:
                latest = await plan.gateway.call('config.get', {})
                latest_config = _config(latest, plan.gateway)
                if _entry(latest_config, agent_id) == plan.agent_config and all(latest_config.get('mcp', {}).get('servers', {}).get(name) == value for name, value in target_mcp.items()):
                    await plan.gateway.call('config.patch', {'baseHash': latest['hash'],
                        **payload(previous, previous_mcp, latest_config)})
                    atomic_write_text(marker, json.dumps({**provenance, 'appliedConfig': previous, 'appliedMcp': {key: value for key, value in previous_mcp.items() if value is not None}}, sort_keys=True))
            except Exception:
                pass
            await plan.gateway.close()
            raise
        return plan
    except BaseException:
        await plan.gateway.close()
        raise


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    # Global apply can preview the resolved set, but Agent creation belongs to a
    # concrete conversation so unrelated conversations never share native state.
    return materialize(spec, binding, effective, layout='roots', dry_run=True)


def cli_command(plan, command):
    # The specialized driver resolves the native Gateway key and checks this
    # same owned Agent scope before returning the actual TUI command.
    return tuple(command)

from .skill_inventory import inventory_roots, refresh_inventory
