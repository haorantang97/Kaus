import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.acp.client import AcpAgentSpec
from drivers.claude_code.quota import normalize_usage, read_account_quota, supports_local_usage, _reset_at
from drivers.codex.quota import _first_party_config


def test_only_positive_noninteractive_local_command_signature_is_supported(tmp_path):
    program = tmp_path / 'cli'
    program.write_bytes(b'var usage={type:"local",name:"usage",aliases:["cost"],supportsNonInteractive:!0};')
    assert supports_local_usage(str(program))
    program.write_bytes(b'var usage={type:"local-jsx",name:"usage",supportsNonInteractive:!0};')
    assert not supports_local_usage(str(program))
    program.write_bytes(b'var usage={type:"local",name:"usage"};')
    assert not supports_local_usage(str(program))
    assert not supports_local_usage(str(tmp_path / 'missing'))


def test_claude_windows_keep_model_scope_and_unknown_reset_is_null():
    payload = {'result': 'Current session: 13% used · resets Oct 18 at 3:30pm (Asia/Shanghai)\n'
        'Current week (all models): 4% used · resets Oct 20, 2pm (Asia/Shanghai)\n'
        'Current week (Sonnet only): 0% used\nTotal cost: $18'}
    result = normalize_usage({'subscriptionType': 'max', 'email': 'secret@example.invalid'}, payload)
    assert len(result.windows) == 3
    assert result.windows[2].label == '每周 · Sonnet only'
    assert result.windows[2].resets_at is None
    assert result.windows[2].to_wire()['remainingPercent'] == 100
    assert result.account_key is None
    assert result.account_label == 'Claude · s***@example.invalid'
    assert 'secret' not in repr(result)


@pytest.mark.parametrize('text', ['Usage available for subscribers only', 'Total cost: $0.00', '', 'Current session: unknown'])
def test_missing_quota_never_becomes_zero(text):
    with pytest.raises(QuotaUnavailable):
        normalize_usage({}, {'result': text})


def test_reset_dates_respect_timezone_and_year_rollover():
    now = datetime(2026, 12, 31, 22, tzinfo=ZoneInfo('Asia/Shanghai'))
    assert _reset_at('Jan 1 at 3pm (Asia/Shanghai)', now) == int(datetime(2027, 1, 1, 15, tzinfo=now.tzinfo).timestamp())
    assert _reset_at('3:04pm (Asia/Shanghai)', now) == int(datetime(2027, 1, 1, 15, 4, tzinfo=now.tzinfo).timestamp())
    assert _reset_at('3pm (Invalid/Timezone)', now) is None
    assert _reset_at('unknown', now) is None


@pytest.mark.parametrize('config', [
    {'model_provider': 'gateway'},
    {'model_providers': {'openai': {'base_url': 'https://gateway.example/v1'}}},
    {'chatgpt_base_url': 'https://gateway.example/backend-api'},
])
def test_codex_third_party_routes_do_not_show_chatgpt_quota(config):
    assert not _first_party_config(config, {})
    assert not _first_party_config({}, {'OPENAI_BASE_URL': 'https://gateway.example/v1'})
    assert _first_party_config({'model_provider': 'openai'}, {})


async def test_claude_read_uses_builtin_only_and_preserves_native_configuration(monkeypatch):
    calls = []
    env = {'CLAUDECODE': '1', 'CLAUDE_CONFIG_DIR': '/fixture/config', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
    driver = SimpleNamespace(agent_spec=AcpAgentSpec(command=('unused',), env=env, inherit_env=False))
    monkeypatch.setattr('drivers.claude_code.quota.native_path', lambda _: '/fixture/claude')
    monkeypatch.setattr('drivers.claude_code.quota.supports_local_usage', lambda _: True)
    async def command(executable, args, **kwargs):
        calls.append((executable, args, kwargs))
        if args[:2] == ['auth', 'status']:
            return {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'pro'}
        return {'result': 'Current session: 15% used'}
    monkeypatch.setattr('drivers.claude_code.quota._command', command)
    result = await read_account_quota(driver)
    assert result.windows[0].used_percent == 15
    assert len(calls) == 2
    _, args, kwargs = calls[1]
    assert args[:2] == ['-p', '/usage']
    assert '--no-session-persistence' in args
    assert args[args.index('--settings') + 1] == '{"disableAllHooks":true}'
    assert args[args.index('--setting-sources') + 1] == 'user'
    assert 'CLAUDECODE' not in kwargs['env']
    assert kwargs['env']['CLAUDE_CONFIG_DIR'] == '/fixture/config'
    assert kwargs['env']['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
    assert not Path(kwargs['cwd']).exists()
    assert env['CLAUDECODE'] == '1'


@pytest.mark.parametrize(('auth', 'state'), [
    ({'loggedIn': False}, 'unauthenticated'),
    ({'loggedIn': True, 'authMethod': 'api_key', 'apiProvider': 'firstParty'}, 'unsupported'),
    ({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'bedrock'}, 'unsupported'),
])
async def test_claude_wrong_account_never_executes_usage(monkeypatch, auth, state):
    calls = []
    monkeypatch.setattr('drivers.claude_code.quota.native_path', lambda _: '/fixture/claude')
    monkeypatch.setattr('drivers.claude_code.quota.supports_local_usage', lambda _: True)
    async def command(executable, args, **kwargs):
        calls.append(args)
        return auth
    monkeypatch.setattr('drivers.claude_code.quota._command', command)
    with pytest.raises(QuotaUnavailable) as exc:
        await read_account_quota(SimpleNamespace(agent_spec=AcpAgentSpec(command=('unused',))))
    assert exc.value.state == state
    assert len(calls) == 1


async def test_unsupported_claude_does_not_execute_any_command(monkeypatch):
    monkeypatch.setattr('drivers.claude_code.quota.native_path', lambda _: '/fixture/claude')
    monkeypatch.setattr('drivers.claude_code.quota.supports_local_usage', lambda _: False)
    async def forbidden(*args, **kwargs):
        raise AssertionError('unsupported local command must not run')
    monkeypatch.setattr('drivers.claude_code.quota._command', forbidden)
    with pytest.raises(QuotaUnavailable) as exc:
        await read_account_quota(SimpleNamespace(agent_spec=AcpAgentSpec(command=('unused',))))
    assert exc.value.state == 'unsupported'
