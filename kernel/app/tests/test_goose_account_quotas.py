import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow
from drivers.acp.client import AcpAgentSpec
from drivers.acp.presets import get_preset
from drivers.acp.native_cli import native_path
from drivers.goose import quota as goose


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, 'system_config_path', lambda _: tmp_path / 'system.yaml')
    return {'HOME': str(tmp_path), 'GOOSE_PATH_ROOT': str(tmp_path / 'goose'), 'PATH': str(tmp_path / 'other-bin')}


def write_config(env, config):
    path = goose.config_dir(env) / 'config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config))


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 99\n'); path.chmod(0o755)
    return str(path)


def driver(env):
    return SimpleNamespace(preset=get_preset('goose'), agent_spec=AcpAgentSpec(command=('goose', 'acp'), env=env, inherit_env=False))


async def test_codex_uses_goose_chosen_binary_and_account_home(monkeypatch, tmp_path, env):
    path = executable(tmp_path / 'selected/codex-custom')
    executable(tmp_path / 'other-bin/codex')
    write_config(env, {'GOOSE_PROVIDER': 'codex', 'CODEX_COMMAND': 'codex-custom', 'GOOSE_SEARCH_PATHS': [str(tmp_path / 'selected')]})
    env.update(CODEX_HOME=str(tmp_path / 'selected-account'), OPENAI_BASE_URL='https://api.openai.com')
    seen = []
    async def read(child):
        seen.append(child)
        assert native_path(child) == path
        assert child.agent_spec.build_env()['CODEX_HOME'] == env['CODEX_HOME']
        assert child.agent_spec.build_env()['OPENAI_BASE_URL'] == env['OPENAI_BASE_URL']
        return AccountQuota('OpenAI', 'Chosen', (QuotaWindow('five', '5h', 12),), account_key='selected-scope')
    monkeypatch.setattr('drivers.codex.quota.read_account_quota', read)
    result = await goose.read_account_quota(driver(env))
    assert result.account_key == 'selected-scope' and len(seen) == 1
    assert env.get('CODEX_PATH') is None


async def test_claude_inherits_config_and_auth_route_removes_nested_flag(monkeypatch, tmp_path, env):
    path = executable(tmp_path / 'selected/claude-custom')
    write_config(env, {'GOOSE_PROVIDER': 'claude-code', 'CLAUDE_CODE_COMMAND': path})
    env.update(CLAUDE_CONFIG_DIR=str(tmp_path / 'chosen-account'), CLAUDECODE='1', ANTHROPIC_BASE_URL='https://relay.test', ANTHROPIC_AUTH_TOKEN='synthetic-route')
    async def read(child):
        assert native_path(child) == path
        scoped = child.agent_spec.build_env()
        assert scoped['CLAUDE_CONFIG_DIR'] == env['CLAUDE_CONFIG_DIR']
        assert scoped['ANTHROPIC_AUTH_TOKEN'] == 'synthetic-route'
        assert scoped['ANTHROPIC_BASE_URL'] == 'https://relay.test'
        assert 'CLAUDECODE' not in scoped and scoped['PATH'] == env['PATH']
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    monkeypatch.setattr('drivers.claude_code.quota.read_account_quota', read)
    with pytest.raises(QuotaUnavailable) as exc: await goose.read_account_quota(driver(env))
    assert exc.value.reason == 'unsupported_provider'
    assert env['CLAUDECODE'] == '1'


def test_config_layer_and_environment_priority(tmp_path, env):
    (tmp_path / 'system.yaml').write_text('GOOSE_PROVIDER: codex\nCODEX_COMMAND: system\n')
    additional = tmp_path / 'extra.yaml'
    additional.write_text('GOOSE_PROVIDER: claude-code\nCODEX_COMMAND: additional\n')
    env['GOOSE_ADDITIONAL_CONFIG_FILES'] = str(additional)
    write_config(env, {'GOOSE_PROVIDER': 'codex', 'CODEX_COMMAND': 'user'})
    config = goose.configuration(env)
    assert config['GOOSE_PROVIDER'] == 'codex' and config['CODEX_COMMAND'] == 'user'
    env['GOOSE_PROVIDER'] = '"claude-code"'
    assert goose.parameter('GOOSE_PROVIDER', config, env) == 'claude-code'


@pytest.mark.parametrize('selected', ['openrouter', 'anthropic', 'chatgpt_codex', None])
async def test_non_delegated_provider_never_inspects_unrelated_cli(monkeypatch, env, selected):
    write_config(env, {'GOOSE_PROVIDER': selected})
    def fail(*a, **k): raise AssertionError('unselected CLI must not be inspected')
    monkeypatch.setattr(goose.shutil, 'which', fail)
    with pytest.raises(QuotaUnavailable) as exc: await goose.read_account_quota(driver(env))
    assert exc.value.state == 'unsupported'


async def test_missing_explicit_command_does_not_fall_back(monkeypatch, tmp_path, env):
    executable(tmp_path / 'other-bin/codex')
    write_config(env, {'GOOSE_PROVIDER': 'codex', 'CODEX_COMMAND': str(tmp_path / 'missing')})
    with pytest.raises(QuotaUnavailable) as exc: await goose.read_account_quota(driver(env))
    assert exc.value.reason == 'dependency_missing'


@pytest.mark.parametrize('provider,variable,target', [('codex-acp', 'CODEX_PATH', 'codex'), ('claude-acp', 'CLAUDE_CODE_EXECUTABLE', 'claude-code')])
def test_acp_uses_only_explicit_native_installation(tmp_path, env, provider, variable, target):
    config = {'GOOSE_PROVIDER': provider}
    with pytest.raises(QuotaUnavailable): goose.selected_cli(config, env)
    binary = executable(tmp_path / 'selected-native')
    env[variable] = binary
    preset, child_env = goose.selected_cli(config, env)
    assert preset.id == target and child_env[variable] == binary


async def test_codex_delegation_reaches_only_native_account_reads(monkeypatch, tmp_path, env):
    path = executable(tmp_path / 'codex-selected')
    write_config(env, {'GOOSE_PROVIDER': 'codex', 'CODEX_COMMAND': path})
    env['CODEX_HOME'] = str(tmp_path / 'selected-home')
    calls = []
    class Connection:
        async def call(self, method, params=None, **kw):
            calls.append(method)
            if method == 'config/read': return {'config': {}}
            if method == 'account/read': return {'account': {'type': 'chatgpt'}, 'workspaceRouting': {'chatgptAccountId': 'selected-account'}}
            if method == 'account/rateLimits/read': return {'rateLimits': {'primary': {'usedPercent': 10}}}
            return {}
        def notify(self, method, params): calls.append(method)
        async def aclose(self): calls.append('closed')
    async def spawn(spec):
        assert spec.command == (path, 'app-server')
        assert spec.build_env()['CODEX_HOME'] == env['CODEX_HOME']
        assert spec.cwd != str(tmp_path)
        return Connection()
    monkeypatch.setattr('drivers.codex.quota.AcpConnection.spawn', spawn)
    result = await goose.read_account_quota(driver(env))
    assert result.windows[0].used_percent == 10
    assert calls == ['initialize', 'initialized', 'config/read', 'account/read', 'account/rateLimits/read', 'closed']


def test_goose_relative_root_ignored_like_native(tmp_path, env):
    env['GOOSE_PATH_ROOT'] = 'relative/root'
    env['XDG_CONFIG_HOME'] = str(tmp_path / 'xdg')
    assert goose.config_dir(env) == tmp_path / 'xdg/goose'


def test_bad_goose_config_does_not_use_default_account(tmp_path, env):
    write_config(env, {'GOOSE_PROVIDER': 'codex'})
    (goose.config_dir(env) / 'config.yaml').write_text('invalid: [')
    with pytest.raises(QuotaUnavailable) as exc: goose.configuration(env)
    assert exc.value.reason == 'credential_unavailable'
