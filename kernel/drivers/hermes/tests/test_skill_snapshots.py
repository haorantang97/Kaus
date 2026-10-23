import json
import sys
from pathlib import Path

import pytest

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import Project
from drivers.hermes import skill_projection, skill_snapshots, skill_inventory
from drivers.hermes.driver import HermesDriver
from drivers.hermes.tests.test_skill_projection import setup, selection, asset, package
from drivers.skill_inventory import discover_skills
from runtime.capability_matrix import BackendCapabilities


def selected(home, binding, paths):
    return skill_projection.project_skills(home=home, binding=binding,
        effective=selection(binding, [asset(path) for path in paths]), dry_run=False)


def test_sessions_hold_separate_writable_copies_and_immutable_baseline(tmp_path):
    root, binding, home, first, second = setup(tmp_path)
    (home / 'memories').mkdir()
    (home / 'memories/notes.md').write_text('project memory')
    (home / 'state.db').write_bytes(b'project session store')
    original = first.read_bytes(), second.read_bytes()
    selected(home, binding, [first])
    one = skill_snapshots.pin_session(home, 'conversation:one')
    another = skill_snapshots.pin_session(home, 'conversation:another')
    baseline = skill_snapshots.capture(home)
    before = skill_snapshots.digest(one / 'skills')
    selected(home, binding, [second])
    two = skill_snapshots.pin_session(home, 'conversation:two')
    assert skill_snapshots.digest(one / 'skills') == before
    assert 'name: one' in next((one / 'skills').rglob('SKILL.md')).read_text()
    assert 'name: "two"' in next((two / 'skills').rglob('SKILL.md')).read_text()
    local = next((one / 'skills').rglob('SKILL.md'))
    local.write_text(local.read_text() + '\nRuntime evolution')
    package(one / 'skills', 'created-in-one')
    assert skill_snapshots.digest(another / 'skills') == before
    assert skill_snapshots.digest(baseline / 'skills') == before
    assert skill_snapshots.pin_session(home, 'conversation:one', resume=True) == one
    assert skill_snapshots.pin_session(home, 'conversation:two', resume=True) == two
    assert (home / 'memories/notes.md').read_text() == 'project memory'
    assert (home / 'state.db').read_bytes() == b'project session store'
    assert original == (first.read_bytes(), second.read_bytes())
    rows = discover_skills(skill_inventory.roots_for(root,
        [Project.create(slug='default'), Project.create(slug='work')], [binding]))
    generated = [row for row in rows if '.kaus-skill-sessions' in row['sourcePath']]
    assert {row['name'] for row in generated} == {'one', 'created-in-one'}
    assert all(row['defaultProjectId'] == binding.project_id and row['agentKey'] == 'hermes' for row in generated)


def test_generation_tampering_and_wrong_project_scope_rejected(tmp_path):
    _, binding, home, first, _ = setup(tmp_path)
    selected(home, binding, [first])
    baseline = skill_snapshots.capture(home)
    next((baseline / 'skills').rglob('SKILL.md')).write_text('changed')
    with pytest.raises(RuntimeError, match='Immutable'):
        skill_snapshots.capture(home)


async def test_managed_native_managed_transition_uses_current_intent_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ACP_PYTHON", sys.executable)
    root, binding, home, first, second = setup(tmp_path)
    owner = HermesDriver(hermes_root=root)
    owner._capabilities = BackendCapabilities()
    owner.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="synthetic")
    initial = await owner.session_options_for(conversation, selection(binding, [asset(first)]))
    one = Path(initial.options.metadata['kaus_skill_scope'])
    restored = conversation.evolve(native_session_id='synthetic-existing')
    native = await owner.session_options_for(restored, EffectiveCapabilities(project_id=binding.project_id))
    assert native.options.metadata['kaus_skill_mode'] == 'native'
    assert 'kaus_skill_scope' not in native.options.metadata
    assert one.exists()
    final = await owner.session_options_for(restored, selection(binding, [asset(second)]))
    two = Path(final.options.metadata['kaus_skill_scope'])
    assert one != two and one.exists()
    assert 'two' in next((two / 'skills').rglob('SKILL.md')).read_text()
    # A later tree change does not silently switch a cold-resumed native session.
    resumed = await owner.session_options_for(restored, selection(binding, [asset(first)]))
    assert Path(resumed.options.metadata['kaus_skill_scope']) == two


async def test_direct_acp_adapter_and_protocol_delegate_pin_same_owned_scope(tmp_path, monkeypatch):
    from drivers.acp.client import AcpAgentSpec
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset
    from drivers.hermes import skill_adapter as hermes
    root, binding, home, first, second = setup(tmp_path)
    from app.projects.models import AgentBinding
    binding = AgentBinding.create(project='work', backend='hermes-acp', runtime_config={'project_execution': 'isolated'})
    spec = AcpAgentSpec(command=('/synthetic/hermes', 'acp'), env={'HERMES_HOME': str(root)})
    monkeypatch.setattr(hermes, 'command_for', lambda command, home, args:
        ('/synthetic/python', '-B', str(Path(hermes.__file__).with_name('skill_launcher.py')), *args))
    driver = AcpDriver(spec, backend_key='hermes-acp', preset=get_preset('hermes-acp'))
    driver.register_binding(binding)
    driver._capabilities = BackendCapabilities()
    async def capabilities():
        return driver._capabilities
    monkeypatch.setattr(driver, 'get_capabilities', capabilities)
    one = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='one')
    two = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='two')
    await driver.session_options_for(one, selection(binding, [asset(first)]))
    first_plan = driver._skill_plans[one.id]
    await driver.session_options_for(two, selection(binding, [asset(second)]))
    second_plan = driver._skill_plans[two.id]
    scope1 = Path(first_plan.spec.env['KAUS_HERMES_SKILL_SCOPE'])
    scope2 = Path(second_plan.spec.env['KAUS_HERMES_SKILL_SCOPE'])
    assert scope1 != scope2
    assert first_plan.spec.env['HERMES_HOME'] == second_plan.spec.env['HERMES_HOME']
    assert 'one' in next((scope1 / 'skills').rglob('SKILL.md')).read_text()
    await driver.session_options_for(one.evolve(native_session_id='existing'), selection(binding, [asset(second)]))
    assert driver._skill_plans[one.id].spec.env['KAUS_HERMES_SKILL_SCOPE'] == str(scope1)
    await driver.session_options_for(one, EffectiveCapabilities(project_id=binding.project_id))
    assert one.id not in driver._skill_plans
    assert scope1.exists()
