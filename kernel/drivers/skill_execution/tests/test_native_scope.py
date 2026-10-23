from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import pi
from drivers.skill_execution import opencode, gemini
from drivers.skill_execution.common import selection_of
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from test_selection import setup, effective


async def test_pi_uses_explicit_paths_and_keeps_original_home(tmp_path, monkeypatch):
    async def guard(plan, spec, executable, cwd): return spec
    monkeypatch.setattr(pi, "guard_spec", guard)
    spec, binding = setup(tmp_path, 'pi')
    executable = tmp_path / 'native path/pi'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nif [ "$1" = "--help" ]; then echo "--no-skills --skill"; else printf "%s\\n" "$@"; fi\n')
    executable.chmod(0o700)
    spec = spec.with_env(PI_ACP_PI_COMMAND=str(executable), PI_CODING_AGENT_DIR=str(tmp_path / 'native-home'))
    plan = await pi.project(spec, binding, effective(tmp_path, binding), tmp_path)
    assert plan.spec.env['PI_CODING_AGENT_DIR'] == spec.env['PI_CODING_AGENT_DIR']
    launcher = Path(plan.spec.env['PI_ACP_PI_COMMAND'])
    import subprocess
    result = subprocess.run([str(launcher), '--mode', 'rpc'], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['--no-skills', '--skill', str(plan.paths[0]), '--mode', 'rpc']
    empty = await pi.project(spec, binding, effective(tmp_path, binding, ()), tmp_path)
    assert '--skill ' not in Path(empty.spec.env['PI_ACP_PI_COMMAND']).read_text()
    assert launcher.read_text() != Path(empty.spec.env['PI_ACP_PI_COMMAND']).read_text()


async def test_explicit_managed_never_disappears_without_adapter(tmp_path):
    spec, binding = setup(tmp_path, 'unimplemented')
    driver = AcpDriver(spec, backend_key=binding.backend_key)
    driver.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="fixture")
    with pytest.raises(UnsupportedCapabilityError, match='项目技能范围'):
        await driver.session_options_for(conversation, effective(tmp_path, binding))
    await driver.session_options_for(conversation, EffectiveCapabilities(project_id=binding.project_id))
    explicit_native = effective(tmp_path, binding)
    explicit_native.entries[0].config['mode'] = 'native'
    assert selection_of(explicit_native) is None
    await driver.session_options_for(conversation, explicit_native)


async def test_nonempty_mcp_not_removed_on_rejected_shape(tmp_path):
    from drivers.acp.client import AcpRpcError
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    connection = SimpleNamespace(call=AsyncMock(side_effect=AcpRpcError(-32602, 'unsupported server')))
    with pytest.raises(AcpRpcError):
        await driver._resume_session(connection, 'same-id', str(tmp_path),
            metadata={'mcpServers': [{'name': 'fixture', 'command': 'fixture', 'args': [], 'env': []}]})
    assert connection.call.await_count == 1


async def test_native_mode_clears_future_plan_but_not_running_instance(tmp_path):
    spec, binding = setup(tmp_path, 'unimplemented')
    driver = AcpDriver(spec, backend_key=binding.backend_key)
    driver.register_binding(binding)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='fixture')
    old = SimpleNamespace(spec=spec)
    driver._skill_plans[conversation.id] = old
    await driver.session_options_for(conversation, EffectiveCapabilities(project_id=binding.project_id))
    assert conversation.id not in driver._skill_plans
    assert old.spec is spec


def test_open_permission_policy_rejects_specific_bypass():
    rules = [{'permission': 'skill', 'pattern': '*', 'action': 'deny'},
             {'permission': 'skill', 'pattern': 'selected', 'action': 'allow'}]
    assert opencode.scope_matches(rules, {'selected'})
    assert not opencode.scope_matches([*rules, {'permission': 'skill', 'pattern': 'other*', 'action': 'ask'}], {'selected'})
    assert not opencode.scope_matches([{'permission': '*', 'pattern': '*', 'action': 'allow'}], set())


async def test_open_overlay_preserves_other_settings_and_never_writes_secret(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'opencode')
    spec = spec.with_env(OPENCODE_CONFIG_CONTENT='{"provider":{"fixture":{"apiKey":"synthetic-private"}},"permission":{"edit":"ask"}}')
    monkeypatch.setattr(opencode.shutil, 'which', lambda *a, **k: '/native/fixture')
    async def inspect(executable, current, cwd, *args):
        import json
        if args == ('config',):
            return {'agent': {'custom': {'permission': {'skill': {'*': 'deny', 'blocked': 'allow'}}}}}
        config = json.loads(current.env['OPENCODE_CONFIG_CONTENT'])
        if args == ('skill',):
            return [{'name': p.parent.name, 'location': str(p)}
                    for p in Path(config['skills']['paths'][0]).glob('*/SKILL.md')]
        return {'permission': [{'permission': 'skill', 'pattern': pattern, 'action': action}
                               for pattern, action in config['agent'][args[1]]['permission']['skill'].items()]}
    monkeypatch.setattr(opencode, 'inspect', inspect)
    plan = await opencode.project(spec, binding, effective(tmp_path, binding), tmp_path)
    import json
    config = json.loads(plan.spec.env['OPENCODE_CONFIG_CONTENT'])
    assert config['provider']['fixture']['apiKey'] == 'synthetic-private'
    assert config['permission']['edit'] == 'ask'
    assert config['agent']['custom']['permission']['skill']['blocked'] == 'deny'
    assert all('synthetic-private' not in p.read_text() for p in plan.root.rglob('*') if p.is_file())
    manifest = json.loads(Path(plan.spec.env['KAUS_SKILL_TERMINAL_OVERLAY']).read_text())
    assert manifest['plugins'] == [config['plugin'][-1]]


async def test_open_native_mode_does_not_probe_missing_cli(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'opencode')
    probe = AsyncMock(side_effect=AssertionError('native mode must not project'))
    monkeypatch.setattr(opencode, 'inspect', probe)
    plan = await opencode.project(spec, binding, EffectiveCapabilities(project_id=binding.project_id), str(tmp_path))
    assert plan.root is None and plan.spec == spec
    probe.assert_not_called()


def test_open_terminal_keeps_user_plugins_and_reapplies_skill_guard(tmp_path):
    import json
    import os
    import subprocess
    import sys
    manifest = tmp_path / 'terminal.json'
    manifest.write_text(json.dumps({'overlay': {'skills': {'paths': ['/synthetic/selected']}},
                                   'skill': {'*': 'deny', 'selected': 'allow'},
                                   'plugins': ['file:///synthetic/guard.mjs']}))
    original = {'provider': {'fixture': {'apiKey': 'synthetic-private'}},
                'permission': {'edit': 'ask'},
                'plugin': ['file:///synthetic/user.mjs', 'file:///synthetic/guard.mjs']}
    result = subprocess.run([sys.executable, str(Path(opencode.__file__).with_name('opencode_terminal.py')),
                             str(manifest), sys.executable, '-c',
                             'import os,json;print(json.dumps([json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]),json.loads(os.environ["OPENCODE_PERMISSION"])]))'],
                            env={**os.environ, 'OPENCODE_CONFIG_CONTENT': json.dumps(original),
                                 'OPENCODE_PERMISSION': '{"bash":"ask"}'},
                            text=True, capture_output=True, check=True)
    config, permissions = json.loads(result.stdout)
    assert config['plugin'] == original['plugin']
    assert config['provider'] == original['provider']
    assert config['permission']['edit'] == 'ask'
    assert config['permission']['skill'] == permissions['skill'] == {'*': 'deny', 'selected': 'allow'}
    assert permissions['bash'] == 'ask'


def test_changed_loader_format_is_not_guessed(tmp_path):
    (tmp_path / 'bundle').mkdir()
    (tmp_path / 'bundle/gemini.js').write_text('import "./chunk-new.js";')
    (tmp_path / 'bundle/chunk-new.js').write_text('export class NewSkills {}')
    assert gemini.loader_module(tmp_path) is None


async def test_turn_always_checks_native_inventory_even_when_known_roots_unchanged(tmp_path, monkeypatch):
    from drivers.skill_execution import codex
    spec, _ = setup(tmp_path, 'codex')
    plan = SimpleNamespace(spec=spec.with_env(CODEX_PATH='/fixture'), root=tmp_path, paths=(),
                           watch_roots=(), watch_snapshot=())
    probe = AsyncMock(side_effect=UnsupportedCapabilityError('new plugin source'))
    monkeypatch.setattr(codex, 'native_inventory', probe)
    with pytest.raises(UnsupportedCapabilityError, match='重新应用'):
        await codex.validate_turn(plan, str(tmp_path))
    probe.assert_awaited_once()


async def test_scope_guard_cancels_prompt_and_closes_its_tasks(tmp_path, monkeypatch):
    import asyncio
    driver = AcpDriver(fake_agent_spec('text-stream'), backend_key='acp')
    cancelled = []
    started = asyncio.Event()
    stopped = asyncio.Event()
    def notify(method, params):
        cancelled.append(method)
        stopped.set()
    async def call(method, params, **kwargs):
        if method == 'session/prompt':
            started.set()
            await stopped.wait()
        else:
            cancelled.append(method)
            return {}
    async def watch(plan, cwd):
        await started.wait()
        raise UnsupportedCapabilityError('scope changed')
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(watch=watch))
    state = SimpleNamespace(connection=SimpleNamespace(call=call, notify=notify), skill_plan=object(), session_id='fixture',
                            handle=SimpleNamespace(binding_id='fixture'))
    with pytest.raises(UnsupportedCapabilityError, match='scope changed'):
        await driver._guarded_prompt(state, {})
    assert cancelled == ['session/cancel']


def test_transport_alias_uses_only_declared_skill_scope(tmp_path):
    from drivers.skill_execution.common import materialize
    spec, binding = setup(tmp_path, 'hermes-acp')
    value = effective(tmp_path, binding, scope='hermes:skills')
    with pytest.raises(UnsupportedCapabilityError):
        materialize(spec, binding, value, layout='roots', dry_run=False)
    value.entries[0].config['scopeKey'] = 'hermes'
    assert len(materialize(spec, binding, value, layout='roots', dry_run=False).paths) == 1


async def test_commandcode_keeps_bridge_and_selected_native_executable(tmp_path):
    from drivers.skill_execution import commandcode
    spec, binding = setup(tmp_path, 'commandcode')
    executable = tmp_path / 'native path/command-code'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nif [ "$1" = "--help" ]; then echo "--no-skills --skill <path>"; else printf "%s\\n" "$@"; fi\n')
    executable.chmod(0o700)
    spec = spec.model_copy(update={'command': ('python', 'bridge.py', '--command', str(executable), '--state-dir', str(tmp_path / 'bridge'))})
    plan = await commandcode.project(spec, binding, effective(tmp_path, binding), tmp_path)
    assert plan.spec.command[:3] == spec.command[:3]
    assert plan.spec.command[4:] == spec.command[4:]
    assert plan.spec.env == spec.env
    import subprocess
    result = subprocess.run([plan.spec.command[3], '-p'], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['--no-skills', '--skill', str(plan.paths[0].parent), '-p']
