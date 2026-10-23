import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import claude, codex
from drivers.skill_execution.common import materialize

CLAUDE = ('@agentclientprotocol/claude-agent-acp', '0.90.0')
CODEX = ('@agentclientprotocol/codex-acp', '1.10.0')


def setup(tmp_path, backend='claude-code'):
    spec = AcpAgentSpec(command=('unused',), cwd=str(tmp_path), inherit_env=False,
                        env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'state')})
    binding = AgentBinding.create(project='project:fixture', backend=backend,
                                  runtime_config={'workspace_root': str(tmp_path)})
    return spec, binding


def effective(tmp_path, binding, names=('common',), scope='skills'):
    assets = []
    for name in names:
        path = tmp_path / 'source' / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nname: {name}\ndescription: synthetic {name}\n---\nBODY_{name}\n')
        assets.append({'capabilityType': scope, 'capabilityId': name, 'name': name, 'sourcePath': str(path)})
    return EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type='skill-selection', capability_id='default', source_project_id=binding.project_id,
        inherited=False, config={'assets': assets}),))


def test_copy_preserves_source_and_disambiguates_scope(tmp_path):
    spec, binding = setup(tmp_path)
    value = effective(tmp_path, binding)
    original = value.entries[0].config['assets'][0]
    value.entries[0].config['assets'].append({**original, 'capabilityType': 'claude-code:skills'})
    before = Path(original['sourcePath']).read_bytes()
    plan = materialize(spec, binding, value, layout='plugin', dry_run=False)
    assert len(set(plan.names)) == 2
    assert all(path.is_file() for path in plan.paths)
    assert Path(original['sourcePath']).read_bytes() == before
    assert plan.root != Path(original['sourcePath']).parent
    assert materialize(spec, binding, value, layout='plugin', dry_run=False).root == plan.root


def test_foreign_and_linked_resources_not_in_allowed_set(tmp_path):
    spec, binding = setup(tmp_path)
    foreign = effective(tmp_path, binding, scope='codex:skills')
    plan = materialize(spec, binding, foreign, layout='plugin', dry_run=True)
    assert plan.names == () and len(plan.report.unsupported) == 1
    value = effective(tmp_path, binding)
    (tmp_path / 'source/common/linked').symlink_to(tmp_path / 'source/common/SKILL.md')
    plan = materialize(spec, binding, value, layout='plugin', dry_run=True)
    assert not plan.names and plan.report.unsupported
    with pytest.raises(UnsupportedCapabilityError):
        materialize(spec, binding, value, layout='plugin', dry_run=False)


def test_dry_run_never_creates_state(tmp_path):
    spec, binding = setup(tmp_path)
    materialize(spec, binding, effective(tmp_path, binding), layout='plugin', dry_run=True)
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('info', [None, ('custom', '999.0.0'), (CLAUDE[0], '0.89.9')])
async def test_unknown_adapter_is_preview_unsupported_and_runtime_blocked(tmp_path, info):
    spec, binding = setup(tmp_path)
    value = effective(tmp_path, binding)
    preview = await claude.project(spec, binding, value, tmp_path, dry_run=True, runtime_info=info)
    assert preview.root is None and preview.report.unsupported
    with pytest.raises(UnsupportedCapabilityError):
        await claude.project(spec, binding, value, tmp_path, runtime_info=info)


async def test_old_explicit_cli_cannot_silently_ignore_whitelist(tmp_path):
    spec, binding = setup(tmp_path)
    executable = tmp_path / 'old-cli'
    executable.write_text('#!/bin/sh\necho "2.1.228 (Claude Code)"\n')
    executable.chmod(0o700)
    with pytest.raises(UnsupportedCapabilityError):
        await claude.project(spec.with_env(CLAUDE_CODE_EXECUTABLE=str(executable)), binding,
            effective(tmp_path, binding), tmp_path, runtime_info=CLAUDE)


async def test_native_allowlist_includes_only_qualified_copies(tmp_path):
    spec, binding = setup(tmp_path)
    plan = await claude.project(spec, binding, effective(tmp_path, binding), tmp_path, runtime_info=CLAUDE)
    options = plan.metadata['claudeCode']['options']
    assert options['skills'] == ['kaus-project:' + plan.names[0]]
    assert options['plugins'] == [{'type': 'local', 'path': str(plan.root)}]


async def test_codex_missing_loader_blocks_run_but_not_preview(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'codex')
    monkeypatch.setattr(codex, '_executable', lambda spec: None)
    value = effective(tmp_path, binding)
    assert (await codex.project(spec, binding, value, tmp_path, dry_run=True, runtime_info=CODEX)).report.unsupported
    with pytest.raises(UnsupportedCapabilityError):
        await codex.project(spec, binding, value, tmp_path, runtime_info=CODEX)


async def test_codex_path_policy_and_parallel_launcher_are_immutable(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'codex')
    spec = spec.with_env(CODEX_CONFIG=json.dumps({'model': 'fixture', 'skills': {'other': True}}))
    value = effective(tmp_path, binding)
    server = tmp_path / 'server'
    server.write_text('verified fixture')
    monkeypatch.setattr(codex, '_executable', lambda spec: '/native/codex')
    monkeypatch.setattr(codex, 'managed_executable', AsyncMock(return_value=(server, {'sourceVersion': '0.160.0'})))
    async def inventory(executable, spec, cwd, root, *, expected=None):
        return {str(path.resolve()) for path in (root / '.agents/skills').glob('*/SKILL.md')}
    monkeypatch.setattr(codex, 'native_inventory', inventory)
    first = await codex.project(spec, binding, value, str(tmp_path / 'one'), runtime_info=CODEX)
    first_launcher = Path(first.spec.env['CODEX_PATH'])
    first_bytes = first_launcher.read_bytes()
    second = await codex.project(spec, binding, effective(tmp_path, binding, ('second',)),
                                 str(tmp_path / 'two'), runtime_info=CODEX)
    assert first.spec.env['CODEX_PATH'] != second.spec.env['CODEX_PATH']
    assert first.metadata == second.metadata == {}  # Skill roots are never writable workspace additions.
    assert first_launcher.read_bytes() == first_bytes
    config = json.loads(first.spec.env['CODEX_CONFIG'])
    assert config['model'] == 'fixture' and config['skills']['other'] is True
    assert config['skills']['config'] == [{'path': str(first.paths[0]), 'enabled': True}]
    assert str(first.paths[0]) in first_bytes.decode()
    assert str(second.paths[0]) not in first_bytes.decode()


async def test_driver_same_binding_conversations_keep_distinct_plans_and_resume_metadata(tmp_path, monkeypatch):
    _, binding = setup(tmp_path)
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)), backend_key='claude-code',
                       preset=get_preset('claude-code'), default_cwd=str(tmp_path))
    driver.agent_spec = driver.agent_spec.with_env(KAUS_SKILL_STATE_ROOT=str(tmp_path / 'state'))
    driver.register_binding(binding)
    async def capabilities():
        driver._backend_name, driver._backend_version = CLAUDE
    monkeypatch.setattr(driver, 'get_capabilities', capabilities)
    first = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    second = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    one = await driver.session_options_for(first, effective(tmp_path, binding, ('one',)))
    two = await driver.session_options_for(second, effective(tmp_path, binding, ('two',)))
    assert one.options.metadata != two.options.metadata
    assert driver._skill_plans[first.id].metadata == one.options.metadata['_meta']
    connection = SimpleNamespace(call=AsyncMock(return_value={'sessionId': 'native-preserved'}))
    await driver._resume_session(connection, 'native-preserved', str(tmp_path), metadata=one.options.metadata)
    params = connection.call.call_args.args[1]
    assert params['sessionId'] == 'native-preserved'
    assert params['_meta'] == one.options.metadata['_meta']
    with pytest.raises(UnsupportedCapabilityError):
        driver.validate_external_session(first)
    untouched = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    driver.validate_external_session(untouched)


async def test_external_preflight_rejects_before_native_creation(tmp_path, monkeypatch):
    from runtime.session_host import SessionHost
    _, binding = setup(tmp_path)
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)), backend_key='claude-code',
                       preset=get_preset('claude-code'))
    driver.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture')
    driver._skill_plans[conversation.id] = SimpleNamespace(root=tmp_path)
    create = AsyncMock()
    monkeypatch.setattr(driver, 'create_native_session', create)
    host = SimpleNamespace(_capability_projector=object(), _session_projection=AsyncMock())
    with pytest.raises(UnsupportedCapabilityError):
        await SessionHost.prepare_external_session(host, conversation, binding, driver)
        await driver.create_native_session(binding, None)
    create.assert_not_awaited()
    host._session_projection.assert_awaited_once()


async def test_loader_error_stops_required_projection(tmp_path, monkeypatch):
    _, binding = setup(tmp_path, 'codex')
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)), backend_key='codex', preset=get_preset('codex'))
    driver.register_binding(binding)
    async def capabilities():
        driver._backend_name, driver._backend_version = CODEX
    monkeypatch.setattr(driver, 'get_capabilities', capabilities)
    monkeypatch.setattr(codex, '_executable', lambda spec: None)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture')
    with pytest.raises(UnsupportedCapabilityError):
        await driver.session_options_for(conversation, effective(tmp_path, binding))
    assert conversation.id not in driver._skill_plans


@pytest.mark.parametrize('configured,explicit,expected', [
    ('gateway', None, 'gateway'), ('gateway', 'chosen', 'chosen'), (None, None, None), ('  ', None, None)])
async def test_resume_provider_matches_new_thread_without_overriding_explicit_env(tmp_path, monkeypatch, configured, explicit, expected):
    spec, binding = setup(tmp_path, 'codex')
    spec = spec.with_env(CODEX_CONFIG=json.dumps({'model_provider': configured} if configured is not None else {}))
    if explicit is not None:
        spec = spec.with_env(MODEL_PROVIDER=explicit)
    monkeypatch.setattr(codex, '_executable', lambda spec: '/native/codex')
    server = tmp_path / 'server'
    server.write_text('fixture')
    monkeypatch.setattr(codex, 'managed_executable', AsyncMock(return_value=(server, {'sourceVersion': '0.160.0'})))
    async def inventory(executable, spec, cwd, root, *, expected=None):
        return {str(path.resolve()) for path in (root / '.agents/skills').glob('*/SKILL.md')}
    monkeypatch.setattr(codex, 'native_inventory', inventory)
    plan = await codex.project(spec, binding, effective(tmp_path, binding), str(tmp_path), runtime_info=CODEX)
    assert plan.spec.env.get('MODEL_PROVIDER') == expected


def test_verified_capability_survives_same_probe_but_resets_on_adapter_or_spec_change(tmp_path):
    from runtime.capability_matrix import SupportLevel
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)), backend_key='claude-code', preset=get_preset('claude-code'))
    initialized = {'protocolVersion': 1, 'agentCapabilities': {}, 'agentInfo': {'name': CLAUDE[0], 'version': CLAUDE[1]}}
    driver._absorb_initialize(initialized)
    assert driver._capabilities.support_for('skills') == SupportLevel.UNSUPPORTED
    driver._remember_skill_projection(SimpleNamespace(root=tmp_path))
    driver._absorb_initialize(initialized)
    assert driver._capabilities.support_for('skills') == SupportLevel.ADAPTED
    driver.agent_spec = driver.agent_spec.with_env(CLAUDE_CODE_EXECUTABLE='/different-cli')
    driver._absorb_initialize(initialized)
    assert driver._capabilities.support_for('skills') == SupportLevel.UNSUPPORTED
    driver._remember_skill_projection(SimpleNamespace(root=tmp_path))
    driver._absorb_initialize({**initialized, 'agentInfo': {**initialized['agentInfo'], 'version': '0.89.0'}})
    assert driver._capabilities.support_for('skills') == SupportLevel.UNSUPPORTED
