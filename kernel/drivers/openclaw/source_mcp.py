"""Disabled Gateway MCP registrations enabled only in one owned source session.

The native Agent, its Skills, memory and existing configuration stay untouched.
OpenClaw's session overlay accepts server names, not server definitions, so only
Kaus-owned disabled definitions are added to the Gateway registry.
"""
from __future__ import annotations

import hashlib
import json

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from drivers.session_mcp import projection as session_mcp
from drivers.skill_execution.common import state_root
from .driver import OpenClawGateway
from .source_session import owns_session, _connection_id


def discard_automatic_fallback(metadata):
    """Keep an older native route usable without claiming injected tools exist."""
    from drivers.computer_use.integration import server_fingerprint
    info = metadata.get('_kausComputerUse', {})
    rows = info.get('autoInjectedServers', [])
    servers, removed = [], set()
    for server in metadata.get('mcpServers', []):
        matched = next((row for row in rows if row.get('name') == server.get('name')
            and row.get('fingerprint') == server_fingerprint(server)), None)
        if matched:
            removed.update(matched.get('capabilities', []))
        else:
            servers.append(server)
    metadata['mcpServers'] = servers
    retired_resources, retained_resources = set(), set()
    for cap in ('browser', 'desktop'):
        selection = info.get(cap, {})
        resource = selection.get('resource')
        if cap in removed and selection.get('source') == 'fallback':
            if resource:
                retired_resources.add(resource)
            info[cap] = {'status': 'unavailable', 'requirements': ['new-session'], 'exposed': False}
        elif resource and (selection.get('exposed') or selection.get('status') == 'ready'):
            retained_resources.add(resource)
    if 'browser:shared' in retired_resources and 'browser:shared' not in retained_resources:
        retired_resources.add('desktop:local')
    info['resources'] = sorted(set(info.get('resources', [])) - (retired_resources - retained_resources))
    info['autoInjectedServers'] = [row for row in rows if not set(row.get('capabilities', [])) & removed]


class SourceMcpGateway(OpenClawGateway):
    allowed_methods = frozenset({'config.get', 'config.patch', 'sessions.describe', 'sessions.patch'})

    def __init__(self, plan, namespace):
        super().__init__(plan.spec)
        self.plan, self.agent_id = plan, namespace

    def _owned(self):
        if not owns_session(self.spec, self.plan.session_key, self.plan.ownership):
            raise UnsupportedCapabilityError('原生会话归属不匹配')

    def validate_session_patch(self, params):
        self._owned()
        if (set(params) - {'key', 'expectedSessionId', 'expectedLifecycleRevision', 'toolOverrides', 'expectedToolOverrides'}
                or params.get('key') != self.plan.session_key or not params.get('expectedSessionId')
                or 'expectedToolOverrides' not in params):
            raise ValueError('Only the owned source session MCP overlay can be changed')
        before, after = params.get('expectedToolOverrides') or {}, params.get('toolOverrides') or {}
        if {k: v for k, v in before.items() if k != 'mcpServers'} != {k: v for k, v in after.items() if k != 'mcpServers'}:
            raise ValueError('Unrelated session tools must remain unchanged')
        previous, updated = before.get('mcpServers', {}), after.get('mcpServers', {})
        prefix = self.agent_id + '-'
        if (not all(isinstance(v, bool) for v in updated.values())
                or {k: v for k, v in previous.items() if not k.startswith(prefix)} !=
                   {k: v for k, v in updated.items() if not k.startswith(prefix)}):
            raise ValueError('Only owned MCP overrides can be changed')

    async def call(self, method, params):
        self._owned()
        if method == 'config.patch':
            patch = json.loads(params.get('raw', '{}'))
            prefix = self.agent_id + '-'
            servers = patch.get('mcp', {}).get('servers', {})
            if (set(patch) != {'mcp'} or set(patch['mcp']) != {'servers'} or not params.get('baseHash')
                    or not all(k.startswith(prefix) and (v is None or v.get('enabled') is False) for k, v in servers.items())
                    or any(not any(path == 'mcp.servers.' + key or path.startswith('mcp.servers.' + key + '.')
                        for key in servers) for path in params.get('replacePaths', []))):
                raise ValueError('Only disabled owned MCP definitions can be changed')
        return await super().call(method, params)


def _payload(servers, registry):
    # config.patch deep-merges objects; delete obsolete transport/credential
    # keys explicitly, exactly as Project MCP replacement does.
    def replacement(value, old):
        if not isinstance(value, dict) or not isinstance(old, dict):
            return value
        return {key: replacement(value.get(key), old.get(key)) for key in value.keys() | old.keys()}
    paths = set()
    def arrays(value, path):
        if isinstance(value, list):
            paths.add(path)
        elif isinstance(value, dict):
            for key, item in value.items():
                arrays(item, path + '.' + key)
    for name, value in servers.items():
        paths.add('mcp.servers.' + name)
        arrays(value, 'mcp.servers.' + name)
        arrays(registry.get(name), 'mcp.servers.' + name)
    return {'raw': json.dumps({'mcp': {'servers': {name: replacement(value, registry.get(name))
        for name, value in servers.items()}}}), 'replacePaths': sorted(paths)}


async def prepare(plan, conversation, servers):
    from .config_lock import configuration_lock
    async with configuration_lock(plan.spec):
        return await _prepare(plan, conversation, servers)


async def _prepare(plan, conversation, servers):
    from .skill_adapter import _config
    identity = hashlib.sha256((_connection_id(plan.spec) + ':' + plan.session_key).encode()).hexdigest()[:24]
    namespace = 'kaus-source-' + identity
    marker = state_root(plan.spec) / 'openclaw-source-mcp' / (identity + '.json')
    provenance = {'schema': 1, 'sessionKey': plan.session_key, 'namespace': namespace,
                  'connectionId': _connection_id(plan.spec)}
    ownership = {}
    if marker.exists() or marker.is_symlink():
        try:
            if marker.is_symlink() or marker.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            ownership = json.loads(marker.read_text())
            if any(ownership.get(k) != v for k, v in provenance.items()):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise UnsupportedCapabilityError('会话 MCP 归属不匹配') from None
    if not servers and not ownership:
        return
    definitions, names = {}, {}
    for server in servers:
        name = namespace + '-' + hashlib.sha256(server['name'].encode()).hexdigest()[:12]
        value = {k: v for k, v in server.items() if k != 'name'}
        for field in ('env', 'headers'):
            if field in value:
                value[field] = {item['name']: item['value'] for item in value[field]}
        if value.get('type') == 'http':
            value['type'] = 'streamable-http'
        definitions[name] = {**value, 'enabled': False}
        names[name] = server['name']
    if definitions:
        checked = session_mcp._config(plan.spec, conversation, {'mcpServers': list(servers)})
        await session_mcp.probe(plan.spec.with_env(**{session_mcp.CONFIG_ENV: str(checked)}))
    gateway = SourceMcpGateway(plan, namespace)
    previous_gateway = plan.gateway
    try:
        snapshot = await gateway.call('config.get', {})
        config = _config(snapshot, gateway)
        if config.get('gateway', {}).get('reload', {}).get('mode') == 'off':
            raise UnsupportedCapabilityError('Gateway 已关闭配置热更新，无法添加会话工具')
        registry = config.get('mcp', {}).get('servers', {})
        old, pending = ownership.get('appliedMcp', {}), ownership.get('pendingMcp', {})
        keys = set(old) | set(pending) | set(definitions)
        previous = {name: registry.get(name) for name in keys}
        for name, value in previous.items():
            if value is not None and value not in (old.get(name), pending.get(name)):
                raise UnsupportedCapabilityError('会话 MCP 标识已被占用或由外部修改')
        target = {name: definitions.get(name) for name in keys}
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if marker.parent.is_symlink():
            raise UnsupportedCapabilityError('会话 MCP 归属目录不可用')
        if previous != target:
            atomic_write_text(marker, json.dumps({**provenance, 'appliedMcp': previous, 'pendingMcp': definitions}, sort_keys=True))
            await gateway.call('config.patch', {'baseHash': snapshot['hash'], **_payload(target, registry)})
        latest = _config(await gateway.call('config.get', {}), gateway).get('mcp', {}).get('servers', {})
        if any(latest.get(name) != value for name, value in target.items()):
            raise UnsupportedCapabilityError('Gateway 未应用会话 MCP')
        atomic_write_text(marker, json.dumps({**provenance, 'appliedMcp': definitions}, sort_keys=True))
        plan.gateway = gateway
        plan.mcp_namespace = namespace
        plan.runtime_mcp_servers = tuple(dict(server) for server in servers)
        plan.mcp_config, plan.mcp_display_names = definitions, names
        plan.mcp_overrides = {name: name in definitions for name in keys}
        await previous_gateway.close()
    except BaseException:
        # A failed CAS or disconnect is never blindly replayed. The pending
        # receipt allows the next preparation to reconcile only our own keys.
        await gateway.close()
        await previous_gateway.close()
        raise


async def validate(plan, snapshot):
    if not getattr(plan, 'mcp_overrides', None):
        return
    from .skill_adapter import _config
    registry = _config(snapshot, plan.gateway).get('mcp', {}).get('servers', {})
    if any(registry.get(name) != plan.mcp_config.get(name) for name in plan.mcp_overrides):
        raise UnsupportedCapabilityError('会话 MCP 配置已变化，请重新应用')
    if hasattr(plan, 'expected_tool_overrides'):
        row = (await plan.gateway.call('sessions.describe', {'key': plan.session_key})).get('session') or {}
        if row.get('toolOverrides') != plan.expected_tool_overrides:
            raise UnsupportedCapabilityError('会话 MCP 配置已变化，请重新应用')
