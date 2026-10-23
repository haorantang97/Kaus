"""Native-shaped Gateway CAS/overlay tests; no local OpenClaw account required."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.acp.client import AcpRpcError
from drivers.openclaw import skill_adapter as adapter, source_session, source_mcp
from drivers.openclaw.driver import OpenClawGateway
from .test_project_mcp import Gateway, setup


@pytest.fixture
def overlay(setup, monkeypatch):
    spec, binding, conv, effective = setup
    native = Gateway.config['agents']['entries']['user']
    native.update({'skills': ['private'], 'agentDir': '/native/private',
                   'memorySearch': {'enabled': True}, 'identity': {'name': 'Original'}})
    class OverlayGateway(Gateway):
        def __init__(self, plan, namespace):
            super().__init__(plan.spec, namespace)
            self.plan, self.spec = plan, plan.spec
    monkeypatch.setattr(source_mcp, 'SourceMcpGateway', OverlayGateway)
    server = {'name': 'playwright', 'command': '/synthetic/browser', 'args': ['--isolated'], 'env': []}
    async def prepare(conversation=conv, servers=None):
        plan = await source_session.prepare(spec, binding, effective(None), conversation, {})
        await source_mcp.prepare(plan, conversation, [server] if servers is None else servers)
        return plan
    return spec, binding, conv, effective, server, prepare


async def test_source_agent_and_other_sessions_unchanged(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    before = copy.deepcopy(Gateway.config)
    Gateway.sessions['agent:user:native'] = {'sessionId': 'original', 'toolOverrides': {'skills': {'private': True}}}
    original_session = copy.deepcopy(Gateway.sessions['agent:user:native'])
    plan = await prepare()
    name = next(iter(plan.mcp_config))
    assert plan.agent_id == 'user' and plan.source_session and not plan.manage_skills
    assert Gateway.config['agents'] == before['agents']
    assert Gateway.config['mcp']['servers']['fixture'] == before['mcp']['servers']['fixture']
    assert Gateway.config['mcp']['servers'][name]['enabled'] is False
    assert all(set(json.loads(p['raw'])) == {'mcp'} for p in Gateway.patches)
    Gateway.sessions[plan.session_key] = {'sessionId': 'owned', 'toolOverrides': {'skills': {'private': True}, 'mcpServers': {'native': False}}}
    await adapter.apply_session_mcp(plan)
    await adapter.validate_turn(plan, spec.cwd)
    assert Gateway.sessions[plan.session_key]['toolOverrides'] == {'skills': {'private': True}, 'mcpServers': {'native': False, name: True}}
    assert Gateway.sessions['agent:user:native'] == original_session


async def test_two_concurrent_source_sessions_have_disjoint_tools_and_cleanup(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    first, second = await asyncio.gather(prepare(), prepare(conv.model_copy(update={'id': 'group-peer'})))
    first_name, second_name = next(iter(first.mcp_config)), next(iter(second.mcp_config))
    assert first_name != second_name and first.session_key != second.session_key
    for plan in (first, second):
        Gateway.sessions[plan.session_key] = {'sessionId': plan.session_key, 'toolOverrides': None}
        await adapter.apply_session_mcp(plan)
    assert Gateway.sessions[first.session_key]['toolOverrides']['mcpServers'] == {first_name: True}
    assert Gateway.sessions[second.session_key]['toolOverrides']['mcpServers'] == {second_name: True}
    cleaned = await prepare(servers=[])
    await adapter.apply_session_mcp(cleaned)
    assert first_name not in Gateway.config['mcp']['servers']
    assert second_name in Gateway.config['mcp']['servers']
    assert Gateway.sessions[first.session_key]['toolOverrides']['mcpServers'] == {first_name: False}
    await adapter.validate_turn(second, spec.cwd)


async def test_concurrent_registrations_share_config_lock_and_both_complete(overlay, monkeypatch):
    spec, binding, conv, effective, server, prepare = overlay
    original = source_mcp.SourceMcpGateway.call
    versions = []
    async def call(self, method, params):
        if method == 'config.get':
            value = await original(self, method, params)
            await asyncio.sleep(.01)
            return value
        if method == 'config.patch':
            versions.append(params['baseHash'])
        return await original(self, method, params)
    monkeypatch.setattr(source_mcp.SourceMcpGateway, 'call', call)
    first, second = await asyncio.gather(prepare(), prepare(conv.model_copy(update={'id': 'concurrent-peer'})))
    assert versions == ['0', '1']
    assert set(first.mcp_config).isdisjoint(second.mcp_config)
    assert first.mcp_config.items() <= Gateway.config['mcp']['servers'].items()
    assert second.mcp_config.items() <= Gateway.config['mcp']['servers'].items()


async def test_repeated_same_source_session_registration_is_serialized(overlay, monkeypatch):
    *_, prepare = overlay
    original = source_mcp.SourceMcpGateway.call
    async def call(self, method, params):
        value = await original(self, method, params)
        if method == 'config.get':
            await asyncio.sleep(.01)
        return value
    monkeypatch.setattr(source_mcp.SourceMcpGateway, 'call', call)
    first, second = await asyncio.gather(prepare(), prepare())
    assert first.session_key == second.session_key
    assert first.mcp_config == second.mcp_config
    assert len(Gateway.patches) == 1


async def test_unknown_patch_outcome_is_reconciled_without_duplicate_write(overlay, monkeypatch):
    *_, prepare = overlay
    original = source_mcp.SourceMcpGateway.call
    lost = False
    async def call(self, method, params):
        nonlocal lost
        result = await original(self, method, params)
        if method == 'config.patch' and not lost:
            lost = True
            raise AcpRpcError(-32000, 'synthetic lost acknowledgement')
        return result
    monkeypatch.setattr(source_mcp.SourceMcpGateway, 'call', call)
    with pytest.raises(AcpRpcError):
        await prepare()
    plan = await prepare()
    assert len(Gateway.patches) == 1
    assert plan.mcp_config.items() <= Gateway.config['mcp']['servers'].items()


async def test_existing_definition_changed_externally_is_never_overwritten(overlay):
    *_, prepare = overlay
    plan = await prepare()
    name = next(iter(plan.mcp_config))
    Gateway.config['mcp']['servers'][name] = {'enabled': True, 'command': '/external'}
    before = copy.deepcopy(Gateway.config)
    with pytest.raises(UnsupportedCapabilityError, match='外部修改'):
        await prepare()
    assert Gateway.config == before
    with pytest.raises(UnsupportedCapabilityError, match='已变化'):
        await adapter.validate_turn(plan, plan.spec.cwd)


async def test_failed_probe_never_registers_or_changes_source(overlay, monkeypatch):
    *_, prepare = overlay
    before = copy.deepcopy(Gateway.config)
    monkeypatch.setattr(source_mcp.session_mcp, 'probe', AsyncMock(side_effect=UnsupportedCapabilityError('probe failed')))
    with pytest.raises(UnsupportedCapabilityError, match='probe failed'):
        await prepare()
    assert Gateway.config == before and not Gateway.patches


async def test_reload_disabled_preserves_all_configuration(overlay):
    *_, prepare = overlay
    Gateway.config['gateway'] = {'reload': {'mode': 'off'}}
    before = copy.deepcopy(Gateway.config)
    with pytest.raises(UnsupportedCapabilityError, match='热更新'):
        await prepare()
    assert Gateway.config == before and not Gateway.patches


async def test_cold_resume_retains_source_route_and_does_not_reregister(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    first = await prepare()
    patches = len(Gateway.patches)
    second = await prepare(conv.model_copy(update={'native_session_id': 'native-session'}))
    assert second.session_key == first.session_key
    assert second.mcp_config == first.mcp_config
    assert len(Gateway.patches) == patches


async def test_transport_replacement_removes_old_credentials(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    remote = {'name': server['name'], 'type': 'http', 'url': 'http://127.0.0.1:9876/mcp',
              'headers': [{'name': 'Authorization', 'value': 'synthetic-token'}]}
    first = await prepare(servers=[remote])
    name = next(iter(first.mcp_config))
    assert Gateway.config['mcp']['servers'][name]['type'] == 'streamable-http'
    second = await prepare()
    assert Gateway.config['mcp']['servers'][name] == second.mcp_config[name]
    assert not {'headers', 'url', 'type'} & Gateway.config['mcp']['servers'][name].keys()


async def test_preparation_without_project_scope_does_not_create_private_agent(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    plans = {}
    adapter.remember_runtime_context(conv, binding, effective(None), plans)
    metadata = {'mcpServers': [server], '_kausComputerUse': {'controls': {}, 'resources': ['browser:isolated']}}
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    assert plans[conv.id].source_session and plans[conv.id].agent_id == 'user'
    assert 'mcpServers' not in metadata
    assert metadata['_meta']['sessionKey'] == plans[conv.id].session_key
    assert set(Gateway.config['agents']['entries']) == {'user'}
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    assert len(Gateway.patches) == 1


@pytest.mark.parametrize('turn_off', [False, True])
async def test_complete_preparation_explicit_empty_removes_old_tools(overlay, turn_off):
    spec, binding, conv, effective, server, prepare = overlay
    plans = {}
    adapter.remember_runtime_context(conv, binding, effective(None), plans)
    metadata = {'mcpServers': [server], '_kausComputerUse': {'controls': {}, 'resources': ['browser:isolated']}}
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    plan = plans[conv.id]
    name = next(iter(plan.mcp_config))
    Gateway.sessions[plan.session_key] = {'sessionId': 'owned', 'toolOverrides': None}
    await adapter.apply_session_mcp(plan)
    # A fresh projection explicitly supplies an empty set. Re-entry after
    # consumption omits the field instead, so it cannot resurrect this set.
    metadata['mcpServers'] = []
    if turn_off:
        Gateway.config['agents']['entries']['user']['tools'] = {'deny': ['browser', 'computer']}
        metadata['_kausComputerUse'] = {'controls': {'browser': False, 'desktop': False}, 'resources': []}
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    current = plans[conv.id]
    await adapter.apply_session_mcp(current)
    assert current.runtime_mcp_servers == ()
    assert name not in Gateway.config['mcp']['servers']
    assert Gateway.sessions[plan.session_key]['toolOverrides']['mcpServers'][name] is False
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    assert plans[conv.id].runtime_mcp_servers == ()


@pytest.mark.parametrize('route', ['existing', 'fixed', 'fixed-label'])
async def test_old_native_session_skips_only_automatic_fallback(overlay, route):
    from drivers.computer_use.integration import server_fingerprint
    spec, binding, conv, effective, server, prepare = overlay
    if route == 'existing':
        conv = conv.model_copy(update={'native_session_id': 'existing-native'})
    else:
        flag, value = ('--session', 'agent:user:original') if route == 'fixed' else ('--session-label', 'original')
        spec = spec.model_copy(update={'command': (*spec.command, flag, value)})
    adapter.remember_runtime_context(conv, binding, effective(None), {})
    metadata = {'mcpServers': [server], '_kausComputerUse': {
        'browser': {'source': 'fallback', 'status': 'ready', 'resource': 'browser:isolated'},
        'desktop': {'source': 'native', 'status': 'ready', 'exposed': True, 'resource': 'desktop:local'},
        'resources': ['browser:isolated', 'desktop:local'],
        'autoInjectedServers': [{'name': server['name'], 'capabilities': ['browser'], 'fingerprint': server_fingerprint(server)}]}}
    before = copy.deepcopy(Gateway.config)
    await adapter.prepare_runtime_spec(spec, conv, metadata)
    assert not metadata['mcpServers'] and '_meta' not in metadata
    assert metadata['_kausComputerUse']['resources'] == ['desktop:local']
    assert metadata['_kausComputerUse']['browser']['status'] == 'unavailable'
    assert metadata['_kausComputerUse']['desktop']['status'] == 'ready'
    assert Gateway.config == before and not Gateway.patches


def test_auto_fallback_receipt_does_not_remove_replaced_user_definition():
    from drivers.computer_use.integration import server_fingerprint
    original = {'name': 'same', 'command': '/original'}
    replacement = {'name': 'same', 'command': '/user'}
    metadata = {'mcpServers': [replacement], '_kausComputerUse': {
        'autoInjectedServers': [{'name': 'same', 'capabilities': ['desktop'], 'fingerprint': server_fingerprint(original)}]}}
    source_mcp.discard_automatic_fallback(metadata)
    assert metadata['mcpServers'] == [replacement]


async def test_unowned_old_session_is_not_redirected(overlay):
    spec, binding, conv, effective, server, prepare = overlay
    conv = conv.model_copy(update={'native_session_id': 'existing-native'})
    adapter.remember_runtime_context(conv, binding, effective(None), {})
    metadata = {'mcpServers': [server], '_kausComputerUse': {'resources': ['desktop:local']}}
    before = copy.deepcopy(Gateway.config)
    with pytest.raises(UnsupportedCapabilityError, match='新建对话'):
        await adapter.prepare_runtime_spec(spec, conv, metadata)
    assert Gateway.config == before and not Gateway.patches


async def test_gateway_write_guard_rejects_agent_and_foreign_session_changes(setup, monkeypatch):
    spec, binding, conv, effective = setup
    plan = await source_session.prepare(spec, binding, effective(None), conv, {})
    gateway = source_mcp.SourceMcpGateway(plan, 'kaus-source-fixture')
    upstream = AsyncMock(return_value={})
    monkeypatch.setattr(OpenClawGateway, 'call', upstream)
    with pytest.raises(ValueError, match='disabled owned'):
        await gateway.call('config.patch', {'baseHash': 'v1', 'raw': json.dumps({'agents': {'entries': {'user': {}}}})})
    with pytest.raises(ValueError, match='disabled owned'):
        await gateway.call('config.patch', {'baseHash': 'v1', 'raw': json.dumps({'mcp': {'servers': {'native': {'enabled': False}}}})})
    with pytest.raises(ValueError, match='owned source session'):
        gateway.validate_session_patch({'key': 'agent:user:other', 'expectedSessionId': 'id', 'expectedToolOverrides': None, 'toolOverrides': {}})
    with pytest.raises(ValueError, match='Unrelated session'):
        gateway.validate_session_patch({'key': plan.session_key, 'expectedSessionId': 'id', 'expectedToolOverrides': {'skills': {'private': True}}, 'toolOverrides': {'skills': {'private': False}}})
    with pytest.raises(ValueError, match='owned MCP'):
        gateway.validate_session_patch({'key': plan.session_key, 'expectedSessionId': 'id', 'expectedToolOverrides': {'mcpServers': {'native': True}}, 'toolOverrides': {'mcpServers': {'native': False}}})
    upstream.assert_not_called()
