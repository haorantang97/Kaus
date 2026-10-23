"""The native-loader boundary is fixed for both ACP and original remote TUI."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.codex.scoped_launcher import command_for
from drivers.skill_execution import codex
from drivers.acp.client import AcpAgentSpec
from app.projects.models import AgentBinding
from app.capabilities.models import EffectiveCapabilities
from drivers.base import UnsupportedCapabilityError


def test_launcher_routes_only_server_and_remote_tui(tmp_path, monkeypatch):
    server = tmp_path / 'server'
    server.write_bytes(b'private-binary')
    digest = hashlib.sha256(server.read_bytes()).hexdigest()
    allowed = [str(tmp_path / 'scope/SKILL.md')]
    monkeypatch.setattr('drivers.codex.scoped_launcher.subprocess.run',
                        lambda *a, **k: SimpleNamespace(stdout='codex-cli 0.160.0\n'))
    command, env = command_for('/native/codex', str(server), allowed, '0.160.0', digest,
                              ['app-server', '--stdio'], {'KAUS_CODEX_SKILL_ALLOWLIST': 'stale', 'CODEX_HOME': '/native/home'})
    assert command == [str(server), '-c', codex.config_argument([{'path': allowed[0], 'enabled': True}])]
    assert json.loads(env['KAUS_CODEX_SKILL_ALLOWLIST']) == allowed
    assert env['CODEX_HOME'] == '/native/home'
    assert env['KAUS_CODEX_PERSIST_THREAD_START'] == '1'
    command, env = command_for('/native/codex', str(server), allowed, '0.160.0', digest,
                              ['--remote', 'unix:///socket', 'resume', 'same-id'], env)
    assert command == ['/native/codex', '--remote', 'unix:///socket', 'resume', 'same-id']
    assert 'KAUS_CODEX_SKILL_ALLOWLIST' not in env
    assert 'KAUS_CODEX_PERSIST_THREAD_START' not in env
    with pytest.raises(ValueError, match='受管会话'):
        command_for('/native/codex', str(server), allowed, '0.160.0', digest, ['exec', 'unsafe'], {})


def test_native_precreate_preserves_all_native_skill_sources(tmp_path):
    server = tmp_path / 'server'
    server.write_bytes(b'private-binary')
    command, env = command_for('/native/codex', str(server), None, '0.160.0',
        hashlib.sha256(server.read_bytes()).hexdigest(), ['app-server', '--stdio'],
        {'CODEX_CONFIG': '{"skills":{"config":[]}}', 'KAUS_CODEX_SKILL_ALLOWLIST': '[]'})
    assert command == [str(server)]
    assert env['KAUS_CODEX_PERSIST_THREAD_START'] == '1'
    assert 'KAUS_CODEX_SKILL_ALLOWLIST' not in env
    assert env['CODEX_CONFIG'] == '{"skills":{"config":[]}}'


async def test_precreate_hook_is_limited_to_fresh_external_session(tmp_path, monkeypatch):
    from app.conversations.models import Conversation
    binding = AgentBinding.create(project='fixture', backend='codex')
    conversation = Conversation.create(title='Synthetic', project_id=binding.project_id,
        agent_binding_id=binding.id)
    server = tmp_path / 'server'; server.write_bytes(b'private-binary')
    spec = AcpAgentSpec(command=('native',), env={'CODEX_PATH': '/native/codex',
        'KAUS_SKILL_STATE_ROOT': str(tmp_path/'state'), 'CODEX_CONFIG': '{"keep":true}'}, inherit_env=False)
    resolve = AsyncMock(return_value=(server, {'sourceVersion':'0.160.0'}))
    monkeypatch.setattr(codex, 'managed_executable', resolve)
    assert await codex.prepare_runtime_spec(spec, conversation, {}) is spec
    assert await codex.prepare_runtime_spec(spec, conversation.evolve(native_session_id='existing'),
        {'_kausExternalPrecreate': True}) is spec
    resolve.assert_not_awaited()
    prepared = await codex.prepare_runtime_spec(spec, conversation, {'_kausExternalPrecreate': True})
    resolve.assert_awaited_once_with(spec, '/native/codex', require_matching_tui=True)
    assert prepared.env['CODEX_CONFIG'] == spec.env['CODEX_CONFIG']
    assert "--allowed null" in Path(prepared.env['CODEX_PATH']).read_text()
    assert spec.env['CODEX_PATH'] == '/native/codex'


def test_changed_binary_and_tui_version_rejected(tmp_path, monkeypatch):
    server = tmp_path / 'server'
    server.write_bytes(b'changed')
    with pytest.raises(ValueError, match='校验失败'):
        command_for('/native/codex', str(server), [], '0.160.0', 'wrong', ['app-server'], {})
    monkeypatch.setattr('drivers.codex.scoped_launcher.subprocess.run',
                        lambda *a, **k: SimpleNamespace(stdout='codex-cli 0.161.0\n'))
    with pytest.raises(ValueError, match='版本已变更'):
        command_for('/native/codex', str(server), [], '0.160.0', '', ['--remote', 'unix:///socket'], {})


async def test_native_mode_keeps_original_without_private_component(monkeypatch):
    spec = AcpAgentSpec(command=('native',), inherit_env=False)
    binding = AgentBinding.create(project='project:fixture', backend='codex')
    helper = AsyncMock(side_effect=AssertionError('native must not resolve component'))
    monkeypatch.setattr(codex, 'managed_executable', helper)
    plan = await codex.project(spec, binding, EffectiveCapabilities(project_id=binding.project_id, entries=()), None)
    assert plan.spec is spec and plan.root is None
    helper.assert_not_awaited()


async def test_card_server_does_not_depend_on_native_tui_version(tmp_path, monkeypatch):
    server = tmp_path / 'verified-server'
    verified = []
    def resolve(spec, **kwargs):
        verified.append(kwargs)
        return server
    query = AsyncMock(side_effect=AssertionError('card must not launch the optional TUI'))
    monkeypatch.setattr(codex, 'resolve_managed_binary', resolve)
    monkeypatch.setattr(codex, 'resolve_companion', lambda *args: tmp_path / 'host')
    monkeypatch.setattr('drivers.skill_execution.native_catalog.query', query)
    result, build = await codex.managed_executable(
        AcpAgentSpec(command=('unused',), inherit_env=False), '/native/codex')
    assert result == server
    assert verified == [{'engine_key': 'codex', 'source_version': build['sourceVersion'],
        'patch_version': build['patchVersion'], 'expected_source_commit': build['sourceCommit'],
        'expected_patch_hash': build['patchHash']}]
    query.assert_not_awaited()


async def test_card_still_rejects_unverified_server(monkeypatch):
    def reject(*args, **kwargs):
        raise UnsupportedCapabilityError('运行组件校验失败')
    monkeypatch.setattr(codex, 'resolve_managed_binary', reject)
    with pytest.raises(UnsupportedCapabilityError, match='校验失败'):
        await codex.managed_executable(AcpAgentSpec(command=('unused',), inherit_env=False), '/native/codex')


async def test_terminal_precreate_still_requires_matching_native_tui(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, 'resolve_managed_binary', lambda *a, **k: tmp_path / 'verified-server')
    monkeypatch.setattr(codex, 'resolve_companion', lambda *args: tmp_path / 'host')
    monkeypatch.setattr('drivers.skill_execution.native_catalog.query', AsyncMock(return_value='codex-cli 99.0.0'))
    with pytest.raises(UnsupportedCapabilityError, match='终端与'):
        await codex.managed_executable(AcpAgentSpec(command=('unused',), inherit_env=False),
                                      '/native/codex', require_matching_tui=True)


def test_cold_terminal_edits_reach_native_resume_and_native_cli(tmp_path, monkeypatch):
    from app.conversations.models import Conversation
    binding = AgentBinding.create(project='fixture', backend='codex')
    conversation = Conversation.create(title='Synthetic', project_id=binding.project_id,
        agent_binding_id=binding.id).evolve(native_session_id='same-id', model_id='selected[low]',
        reasoning_mode='high', approval_mode='auto')
    monkeypatch.setattr(codex, 'cli_command', lambda plan, cmd: ('python', 'terminal', '--', *cmd[1:]))
    plan = SimpleNamespace(root=tmp_path)
    result = codex.external_cli_command(plan, ('codex', 'resume', 'same-id'),
        conversation=conversation, binding=binding, spec=None)
    value = json.loads(result[result.index('--thread-settings') + 1])
    assert value == {'threadId': 'same-id', 'model': 'selected',
        'config': {'model_reasoning_effort': 'high'}, 'approvalPolicy': 'on-request',
        'approvalsReviewer': 'auto_review', 'sandbox': 'workspace-write'}
    native = codex.external_cli_command(None, ('codex', 'resume', 'same-id'),
        conversation=conversation, binding=binding, spec=None)
    assert native[:3] == ('codex', '-m', 'selected')
    assert 'approvals_reviewer="auto_review"' in native
    with pytest.raises(UnsupportedCapabilityError, match='权限模式'):
        codex.external_cli_command(plan, ('codex', 'resume', 'same-id'),
            conversation=conversation.evolve(approval_mode='host:auto'), binding=binding, spec=None)


async def test_terminal_bootstrap_checks_before_interactive_client(tmp_path, monkeypatch):
    from drivers.skill_execution import codex_terminal
    calls = []
    async def request(_connection, _identity, method, params):
        calls.append((method, params))
        if method == 'thread/resume':
            return {'model': 'selected', 'reasoningEffort': 'high', 'approvalPolicy': 'on-request',
                    'approvalsReviewer': 'auto_review'}
        return {}
    monkeypatch.setattr(codex_terminal, 'request', request)
    monkeypatch.setattr(codex_terminal, 'verify', AsyncMock())
    settings = {'threadId': 'same-id', 'model': 'selected', 'config': {'model_reasoning_effort': 'high'},
                'approvalPolicy': 'on-request', 'approvalsReviewer': 'auto_review'}
    await codex_terminal.configure(SimpleNamespace(send=AsyncMock()), tmp_path, str(tmp_path), settings)
    assert calls[-1] == ('thread/resume', settings)
    with pytest.raises(RuntimeError, match='当前会话设置'):
        await codex_terminal.configure(SimpleNamespace(send=AsyncMock()), tmp_path, str(tmp_path),
            {**settings, 'model': 'rejected'})
