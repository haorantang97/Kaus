"""Host opt-in preserves native authority without losing turn ownership."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapability
from drivers.acp.presets import get_preset
from drivers.base import UnsupportedCapabilityError
from drivers.computer_use import integration, probe, settings
from drivers.computer_use.lease import ResourceLease, ResourceBusyError
from drivers.openclaw import skill_adapter as adapter
from drivers.openclaw.driver import OpenClawAcpDriver
from .test_project_mcp import Gateway, setup


@pytest.fixture
def source(setup, monkeypatch):
    spec, binding, conv, effective = setup
    settings.save_enabled(False)
    Gateway.config['agents']['entries']['user'].update({
        'skills': ['native-private'], 'agentDir': '/native/private',
        'tools': {'alsoAllow': ['browser', 'computer']},
    })
    native = AsyncMock(return_value={cap: {
        'status': 'ready', 'provider': 'openclaw-native', 'exposed': True,
    } for cap in ('browser', 'desktop')})
    fallbacks = AsyncMock(side_effect=AssertionError('disabled host must not probe fallbacks'))
    monkeypatch.setattr(integration, 'native_status', native)
    monkeypatch.setattr(probe, 'probe_fallbacks', fallbacks)
    driver = OpenClawAcpDriver(spec, backend_key='openclaw', preset=get_preset('openclaw'))
    driver.register_binding(binding)
    return spec, binding, conv, effective, driver, native, fallbacks


async def test_off_source_conversation_retains_original_identity_and_permissions(source):
    spec, binding, conv, effective, driver, native, fallbacks = source
    original = deepcopy(Gateway.config)
    result = await driver.session_options_for(conv, effective(None))
    plan = driver._skill_plans[conv.id]
    assert plan.agent_id == 'user' and plan.source_session and not plan.manage_skills
    assert result.options.metadata['_meta']['sessionKey'] == plan.session_key
    decision = result.options.metadata['_kausComputerUse']
    assert decision['nativePermissionControl'] == 'agent'
    assert decision['enabled'] is False
    assert decision['controls'] == {'browser': True, 'desktop': True}
    assert Gateway.config == original and not Gateway.patches
    assert not result.options.metadata.get('mcpServers')
    fallbacks.assert_not_called()
    native.assert_awaited_once()


async def test_off_keeps_native_resource_exclusion_and_drops_automatic_fallback(source, tmp_path):
    spec, binding, conv, effective, driver, native, fallbacks = source
    result = await driver.session_options_for(conv, effective(None))
    metadata = dict(result.options.metadata)
    automatic = {'name': 'kaus-computer', 'command': 'fixture-tool'}
    metadata['mcpServers'] = [automatic]
    metadata['_kausComputerUse']['autoInjectedServers'] = [{
        'name': automatic['name'], 'fingerprint': integration.server_fingerprint(automatic),
        'capabilities': ['desktop'],
    }]
    result = await integration.prepare_projection(spec, adapter, binding, conv, effective(None), metadata)
    assert result['mcpServers'] == []
    assert result['_kausComputerUse']['autoInjectedServers'] == []
    assert result['_kausComputerUse']['resources'] == ['browser:shared', 'desktop:local']
    leases = []
    try:
        for resource in result['_kausComputerUse']['resources']:
            leases.append(await ResourceLease(resource, root=tmp_path/'locks').acquire())
        with pytest.raises(ResourceBusyError):
            await ResourceLease('desktop:local', root=tmp_path/'locks', timeout=0).acquire()
    finally:
        for lease in leases: lease.release()
    fallbacks.assert_not_called()


async def test_off_never_relaxes_explicit_project_denial(source):
    spec, binding, conv, effective, driver, native, fallbacks = source
    selected = effective(None).model_copy(update={'entries': (EffectiveCapability(
        capability_type='policies', capability_id='computer_use',
        source_project_id=binding.project_id, inherited=False,
        config={'value': {'desktop': 'off'}},
    ),)})
    original = deepcopy(Gateway.config)
    with pytest.raises(UnsupportedCapabilityError, match='无法按会话关闭'):
        await driver.session_options_for(conv, selected)
    assert Gateway.config == original and not Gateway.patches
    assert not driver._skill_plans
    fallbacks.assert_not_called()


@pytest.mark.parametrize('private_kind', ['workspace', 'project-mcp'])
async def test_off_still_disables_computer_tools_in_private_project_scope(source, private_kind):
    spec, binding, conv, effective, driver, native, fallbacks = source
    selected = effective('project-data') if private_kind == 'project-mcp' else effective(None)
    original = deepcopy(Gateway.config['agents']['entries']['user'])
    result = await driver.session_options_for(conv, selected,
        **({'workspace_root': spec.cwd} if private_kind == 'workspace' else {}))
    plan = driver._skill_plans[conv.id]
    assert plan.agent_id.startswith('kaus-') and not getattr(plan, 'source_session', False)
    decision = result.options.metadata['_kausComputerUse']
    assert decision['controls'] == {'browser': False, 'desktop': False}
    assert 'nativePermissionControl' not in decision
    assert {'browser', 'computer'} <= set(plan.agent_config['tools']['deny'])
    assert Gateway.config['agents']['entries']['user'] == original
    native.assert_not_called()
    fallbacks.assert_not_called()


async def test_cold_source_runtime_default_cwd_does_not_become_a_private_scope(source, monkeypatch):
    from drivers.acp.driver import AcpDriver
    spec, binding, conv, effective, driver, native, fallbacks = source
    monkeypatch.setattr(driver, '_workspace_root_for', lambda _: spec.cwd)
    async def start(_driver, conversation, surface, *, session_options=None):
        decision = session_options.metadata['_kausComputerUse']
        assert decision['nativePermissionControl'] == 'agent'
        assert decision['controls'] == {'browser': True, 'desktop': True}
        assert decision['resources'] == ['browser:shared', 'desktop:local']
        assert driver._skill_plans[conv.id].source_session
        return 'synthetic-runtime'
    monkeypatch.setattr(AcpDriver, 'start_runtime', start)
    original = deepcopy(Gateway.config)
    assert await driver.start_runtime(conv, 'card') == 'synthetic-runtime'
    assert Gateway.config == original and not Gateway.patches
