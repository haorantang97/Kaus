import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.openclaw import skill_adapter as adapter
from drivers.openclaw import source_session
from drivers.openclaw.driver import OpenClawAcpDriver
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.base import CreateSessionOptions, SessionProjection


class Gateway:
    config = {}
    sessions = {}
    patches = []
    def __init__(self, spec, agent_id=None, **_kwargs): self.agent_id = agent_id
    async def close(self): pass
    async def call(self, method, params):
        if method == 'config.get': return {'config': json.loads(json.dumps(self.config)), 'hash': str(len(self.patches))}
        if method == 'config.patch':
            assert params['baseHash'] == str(len(self.patches))
            self.patches.append(params)
            patch = json.loads(params['raw'])
            def merge(before, update):
                if not isinstance(before, dict) or not isinstance(update, dict):
                    return update
                result = dict(before)
                for key, value in update.items():
                    if value is None: result.pop(key, None)
                    else: result[key] = merge(result.get(key), value)
                return result
            for collection, entries in (('agents', patch.get('agents', {}).get('entries', {})), ('mcp', patch.get('mcp', {}).get('servers', {}))):
                target = self.config.setdefault(collection, {}).setdefault('entries' if collection == 'agents' else 'servers', {})
                for key, val in entries.items():
                    if val is None: target.pop(key, None)
                    else: target[key] = merge(target.get(key), val)
            return {'ok': True}
        if method == 'sessions.describe': return {'session': self.sessions.get(params['key'])}
        if method == 'sessions.patch':
            row = self.sessions[params['key']]
            assert row['sessionId'] == params['expectedSessionId']
            assert row.get('toolOverrides') == params['expectedToolOverrides']
            row['toolOverrides'] = params['toolOverrides']
            return {'session': row}
        raise AssertionError(method)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    Gateway.config = {'agents': {'ownership': 'explicit', 'entries': {'user': {'workspace': '/user'}}},
        'mcp': {'servers': {'fixture': {'enabled': True, 'command': '/user/service'}}}}
    Gateway.patches = []; Gateway.sessions = {}
    monkeypatch.setattr(adapter, 'ScopeGateway', Gateway)
    monkeypatch.setattr(source_session, 'SourceSessionGateway', Gateway)
    monkeypatch.setattr(adapter.session_mcp, 'probe', AsyncMock())
    spec = AcpAgentSpec(command=('unused', 'acp'), cwd=str(tmp_path), inherit_env=False, env={'KAUS_SKILL_STATE_ROOT': str(tmp_path/'state')})
    binding = AgentBinding.create(project='project', backend='openclaw')
    conv = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture')
    def effective(value):
        rows = () if value is None else (EffectiveCapability(capability_type='mcp', capability_id='fixture', source_project_id=binding.project_id, inherited=False, config={'command': '/synthetic', 'args': [value]}),)
        return EffectiveCapabilities(project_id=binding.project_id, entries=rows)
    return spec, binding, conv, effective


async def test_owned_disabled_registry_and_native_session_cold_update(setup):
    spec, binding, conv, effective = setup
    original = json.loads(json.dumps(Gateway.config))
    a = await adapter.project_session(spec, binding, effective('A'), spec.cwd, conversation=conv)
    assert 'skills' not in a.agent_config  # native Skill selection is preserved
    name, server = next(iter(a.mcp_config.items()))
    assert server['enabled'] is False and server['args'] == ['A']
    assert a.mcp_overrides == {name: True}
    Gateway.sessions[a.session_key] = {'sessionId':'native', 'toolOverrides': {'tools': {'unrelated': False}, 'mcpServers': {'native-explicit': True}}}
    await adapter.apply_session_mcp(a)
    assert Gateway.sessions[a.session_key]['toolOverrides']['tools'] == {'unrelated': False}
    assert Gateway.sessions[a.session_key]['toolOverrides']['mcpServers']['native-explicit'] is True
    assert 'fixture' not in Gateway.sessions[a.session_key]['toolOverrides']['mcpServers']
    await adapter.validate_turn(a, spec.cwd)
    b = await adapter.project_session(spec, binding, effective('B'), spec.cwd, conversation=conv.model_copy(update={'native_session_id':'native'}))
    assert set(b.mcp_config) == {name}
    assert Gateway.config['mcp']['servers'][name]['args'] == ['B']
    await adapter.apply_session_mcp(b)
    empty = await adapter.project_session(spec, binding, effective(None), spec.cwd, conversation=conv)
    await adapter.apply_session_mcp(empty)
    assert name not in Gateway.config['mcp']['servers']
    assert Gateway.sessions[a.session_key]['toolOverrides']['mcpServers'] == {'native-explicit': True, name: False}
    assert Gateway.config['mcp']['servers']['fixture'] == original['mcp']['servers']['fixture']
    assert Gateway.config['agents']['entries']['user'] == original['agents']['entries']['user']


async def test_parallel_conversations_own_disjoint_registry_and_overlay(setup):
    spec, binding, conv, effective = setup
    first = await adapter.project_session(spec, binding, effective('A'), spec.cwd, conversation=conv)
    second = await adapter.project_session(spec, binding, effective('B'), spec.cwd, conversation=conv.model_copy(update={'id':'group-other'}))
    assert set(first.mcp_config).isdisjoint(second.mcp_config)
    assert set(second.mcp_overrides).isdisjoint(first.mcp_config)
    await adapter.validate_turn(first, spec.cwd)
    await adapter.project_session(spec, binding, effective(None), spec.cwd, conversation=conv)
    await adapter.validate_turn(second, spec.cwd)


async def test_transport_change_removes_previous_credentials_and_transport_fields(setup, monkeypatch):
    spec, binding, conv, effective = setup
    monkeypatch.setenv('FIXTURE_HEADER', 'synthetic')
    http = EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type='mcp', capability_id='fixture', source_project_id=binding.project_id,
        inherited=False, config={'type':'http','url':'http://127.0.0.1:9000/mcp',
                                'headers':{'Authorization':'FIXTURE_HEADER'}}),))
    first = await adapter.project_session(spec, binding, http, spec.cwd, conversation=conv)
    second = await adapter.project_session(spec, binding, effective('A'), spec.cwd, conversation=conv)
    name = next(iter(first.mcp_config))
    assert Gateway.config['mcp']['servers'][name] == second.mcp_config[name]
    assert not {'headers','url','type'} & Gateway.config['mcp']['servers'][name].keys()


async def test_failed_transport_update_restores_exact_prior_config(setup, monkeypatch):
    spec, binding, conv, effective = setup
    monkeypatch.setenv('FIXTURE_HEADER', 'synthetic')
    http = EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type='mcp', capability_id='fixture', source_project_id=binding.project_id,
        inherited=False, config={'type':'http','url':'http://127.0.0.1:9000/mcp',
                                'headers':{'Authorization':'FIXTURE_HEADER'}}),))
    await adapter.project_session(spec, binding, http, spec.cwd, conversation=conv)
    original = json.loads(json.dumps(Gateway.config))
    monkeypatch.setattr(adapter, 'validate_turn', AsyncMock(side_effect=UnsupportedCapabilityError('Rejected')))
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.project_session(spec, binding, effective('A'), spec.cwd, conversation=conv)
    assert Gateway.config == original


async def test_bad_handshake_no_config_write_and_external_override_not_adopted(setup):
    spec, binding, conv, effective = setup
    adapter.session_mcp.probe.side_effect = UnsupportedCapabilityError('MCP unavailable')
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.project_session(spec, binding, effective('bad'), spec.cwd, conversation=conv)
    assert not Gateway.patches
    adapter.session_mcp.probe.side_effect = None
    plan = await adapter.project_session(spec, binding, effective('A'), spec.cwd, conversation=conv)
    name = next(iter(plan.mcp_config))
    Gateway.config['mcp']['servers'][name] = {'enabled':True, 'command':'/external'}
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.project_session(spec, binding, effective('B'), spec.cwd, conversation=conv)
    assert Gateway.config['mcp']['servers'][name]['command'] == '/external'


def test_redacted_credentials_require_exact_local_snapshot_and_detect_external_changes(tmp_path):
    from types import SimpleNamespace
    name = 'kaus-owned-server'
    file = tmp_path/'openclaw.json'
    config = {'gateway': {'mode':'local','port':18789}, 'mcp':{'servers':{name:{'enabled':False,'headers':{'Authorization':'synthetic-private-value'}}}}}
    file.write_text(json.dumps(config))
    masked = json.loads(json.dumps(config));masked['mcp']['servers'][name]['headers']['Authorization']='__OPENCLAW_REDACTED__'
    spec = AcpAgentSpec(command=('unused','acp'),env={'OPENCLAW_CONFIG_PATH':str(file)},inherit_env=False)
    gateway = SimpleNamespace(spec=spec,agent_id='kaus-owned',options={})
    snapshot = {'config':masked,'path':str(file),'hash':'revision'}
    assert adapter._config(snapshot,gateway)['mcp']['servers'][name] == config['mcp']['servers'][name]
    file.write_text(json.dumps({**config,'mcp':{'servers':{name:{'enabled':False,'headers':{'Authorization':'external-change'}}}}}))
    # The actual local value is compared, not a wildcard match on redaction.
    assert adapter._config(snapshot,gateway)['mcp']['servers'][name] != config['mcp']['servers'][name]
    gateway.options={'--url':'ws://remote.example:18789'}
    with pytest.raises(UnsupportedCapabilityError):adapter._config(snapshot,gateway)
    gateway.options={}
    with pytest.raises(UnsupportedCapabilityError):adapter._config({**snapshot,'path':str(tmp_path/'remote-copy.json')},gateway)
    config['mcp']['servers'][name]['headers']['Authorization'] = {'source':'env','id':'TOKEN'}
    file.write_text(json.dumps(config))
    with pytest.raises(UnsupportedCapabilityError):adapter._config(snapshot,gateway)
    file.write_text('[]')
    with pytest.raises(UnsupportedCapabilityError):adapter._config(snapshot,gateway)
    file.unlink();file.symlink_to(tmp_path/'elsewhere')
    with pytest.raises(UnsupportedCapabilityError):adapter._config(snapshot,gateway)


async def test_sensitive_external_edit_blocks_turn_without_showing_value(setup, tmp_path):
    from types import SimpleNamespace
    spec,binding,conv,effective=setup
    plan=await adapter.project_session(spec,binding,effective('A'),spec.cwd,conversation=conv)
    name=next(iter(plan.mcp_config))
    plan.mcp_config[name]['env']={'TOKEN':'expected-synthetic'}
    file=tmp_path/'openclaw.json'
    raw=json.loads(json.dumps(Gateway.config));raw['gateway']={'mode':'local'};raw['mcp']['servers'][name]['env']={'TOKEN':'external-synthetic'}
    file.write_text(json.dumps(raw))
    masked=json.loads(json.dumps(raw));masked['mcp']['servers'][name]['env']['TOKEN']='__OPENCLAW_REDACTED__'
    plan.gateway.spec=spec.with_env(OPENCLAW_CONFIG_PATH=str(file));plan.gateway.options={}
    plan.gateway.call=AsyncMock(return_value={'config':masked,'hash':'revision','path':str(file)})
    with pytest.raises(UnsupportedCapabilityError) as rejected:await adapter.validate_turn(plan,spec.cwd)
    assert 'external-synthetic' not in str(rejected.value)


@pytest.mark.parametrize("mcp", [None, []])
async def test_driver_off_without_project_scope_does_not_replace_native_identity(setup, monkeypatch, mcp):
    from drivers.computer_use.settings import save_enabled
    save_enabled(False)
    spec,binding,conv,effective=setup
    Gateway.config['agents']['entries']['user']['tools']={'alsoAllow':['computer','browser']}
    driver=OpenClawAcpDriver(spec,backend_key='openclaw',preset=get_preset('openclaw'))
    driver.register_binding(binding)
    metadata={'_kausComputerUse':{'controls':{'desktop':False,'browser':False},'resources':[]}}
    if mcp is not None:metadata['mcpServers']=mcp
    start=AsyncMock()
    monkeypatch.setattr(AcpDriver,'start_runtime',start)
    options=CreateSessionOptions(title=conv.title,metadata=metadata)
    await driver.start_runtime(conv,'card',session_options=options)
    start.assert_awaited_once()
    decision = start.call_args.kwargs['session_options'].metadata['_kausComputerUse']
    assert decision['nativePermissionControl'] == 'agent'
    assert decision['controls'] == {'browser': True, 'desktop': True}
    assert decision['resources'] == ['browser:shared', 'desktop:local']
    assert not Gateway.patches
    assert set(Gateway.config['agents']['entries']) == {'user'}
    assert Gateway.config['agents']['entries']['user']['tools']=={'alsoAllow':['computer','browser']}


@pytest.mark.parametrize("native_skills", [[], ["private"]])
async def test_driver_exposed_native_default_gets_owned_stop_route(setup, monkeypatch, native_skills):
    spec,binding,conv,effective=setup
    source=Gateway.config['agents']['entries']['user']
    source['tools']={'profile':'coding','alsoAllow':['computer']}
    source['skills']=native_skills
    source['agentDir']='/native/private'
    original=json.loads(json.dumps(Gateway.config))
    driver=OpenClawAcpDriver(spec,backend_key='openclaw',preset=get_preset('openclaw'))
    driver.register_binding(binding)
    async def start(_driver, conversation, surface, *, session_options=None):
        plan=_driver._skill_plans[conversation.id]
        assert session_options.metadata['_meta']['sessionKey']==plan.session_key
        assert plan.session_key.startswith('agent:user:kaus-')
        assert plan.agent_id=='user' and plan.source_session
        assert source_session.owns_session(spec,plan.session_key,session_options.metadata['_kausOpenClawOwnership'])
        _driver._bind_session_route('synthetic-acp-uuid', {'sessionKey':plan.session_key})
        assert _driver._gateway_for('synthetic-acp-uuid').options['--session']==plan.session_key
        await adapter.validate_turn(plan,spec.cwd)
        await adapter.apply_session_mcp(plan)  # Never mutates source session tool overrides.
        return plan
    monkeypatch.setattr(AcpDriver,'start_runtime',start)
    monkeypatch.setattr('drivers.computer_use.integration.prepare_projection',AsyncMock(return_value={
        '_kausComputerUse':{'controls':{'browser':True,'desktop':True},
            'desktop':{'source':'native','status':'unknown','exposed':True},'resources':['desktop:local']},
    }))
    await driver.start_runtime(conv,'card')
    assert Gateway.config==original
    assert not Gateway.patches


async def test_driver_projection_without_project_scope_keeps_native_agent(setup, monkeypatch):
    spec,binding,conv,effective=setup
    Gateway.config['agents']['entries']['user']['skills']=['private']
    original=json.loads(json.dumps(Gateway.config))
    driver=OpenClawAcpDriver(spec,backend_key='openclaw',preset=get_preset('openclaw'))
    driver.register_binding(binding)
    monkeypatch.setattr(AcpDriver,'session_options_for',AsyncMock(return_value=SessionProjection(
        options=CreateSessionOptions(title=conv.title,metadata={'mcpServers':[],
            '_kausComputerUse':{'controls':{'browser':True,'desktop':True},'resources':['desktop:local']}}))))
    projection=await driver.session_options_for(conv,effective(None))
    plan=driver._skill_plans[conv.id]
    assert plan.agent_id=='user' and plan.source_session
    assert projection.options.metadata['_meta']['sessionKey']==plan.session_key
    assert projection.options.metadata['_kausOpenClawOwnership']==plan.ownership
    assert Gateway.config==original and not Gateway.patches


def test_fallback_allowed_without_replacing_source_agent(setup):
    spec,binding,conv,effective=setup
    assert adapter.computer_use_fallback_allowed(effective(None),{'mcpServers':[{'name':'automatic'}]})
    assert not adapter.project_scope_selected(effective(None))
    assert adapter.computer_use_fallback_allowed(effective('project-mcp'),{})


async def test_driver_final_mcp_selection_reaches_gateway_without_redecoding_credentials(setup, monkeypatch):
    spec,binding,conv,effective=setup
    driver=OpenClawAcpDriver(spec,backend_key='openclaw',preset=get_preset('openclaw'))
    driver.register_binding(binding)
    selected={'name':'selected-fallback','command':'/synthetic/fallback','args':[],
              'env':[{'name':'FIXTURE_TOKEN','value':'synthetic-resolved-value'}]}
    monkeypatch.setattr(AcpDriver,'session_options_for',AsyncMock(return_value=SessionProjection(
        options=CreateSessionOptions(title=conv.title,metadata={
            'mcpServers':[selected], '_meta':{'unrelated':'retained'},
            '_kausComputerUse':{'controls':{'browser':True,'desktop':True},'resources':['browser:shared']},
        }))))
    projection=await driver.session_options_for(conv,effective('A'),workspace_root=spec.cwd)
    plan=driver._skill_plans[conv.id]
    assert list(plan.mcp_display_names.values())==['selected-fallback']
    server=next(iter(plan.mcp_config.values()))
    assert server['env']=={'FIXTURE_TOKEN':'synthetic-resolved-value'}
    assert server['enabled'] is False
    assert projection.options.metadata['_meta']['unrelated']=='retained'
    assert 'mcpServers' not in projection.options.metadata
    # The shared runtime preparation repeats safely and retains the selected MCP.
    metadata=dict(projection.options.metadata)
    await adapter.prepare_runtime_spec(spec,conv,metadata)
    prepared=driver._skill_plans[conv.id]
    assert prepared.mcp_config==plan.mcp_config
    assert metadata['_meta']['sessionKey']==plan.session_key
