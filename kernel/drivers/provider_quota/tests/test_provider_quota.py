from types import SimpleNamespace
import json

import httpx
import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.provider_quota import ProviderSource, discover_provider_sources, endpoint_for, read_sources
from drivers.provider_quota.parsers import normalize, number, timestamp


@pytest.mark.parametrize('kind,payload,amount,currency', [
    ('deepseek', {'balance_infos': [{'currency': 'CNY', 'total_balance': '9.25'}]}, 9.25, 'CNY'),
    ('moonshot', {'code': 0, 'status': True, 'data': {'available_balance': '12.5'}}, 12.5, 'USD'),
    ('openrouter', {'data': {'total_credits': 100, 'total_usage': 24.75}}, 75.25, 'USD'),
    ('siliconflow', {'code': 20000, 'data': {'totalBalance': '3.45'}}, 3.45, 'USD'),
    ('stepfun', {'balance': 12, 'total_cash_balance': 900}, 12, 'USD'),
    ('aihubmix', {'total_usage': 3.5}, 3.5, 'USD'),
    ('commandcode', {'credits': {'monthlyCredits': 8, 'purchasedCredits': 3, 'freeCredits': 1}}, 12, 'USD'),
    ('ollama', {'purchased': {'balance_usd': 25}}, 25, 'USD'),
])
def test_vendor_balances(kind, payload, amount, currency):
    _, balances, _ = normalize(kind, payload)
    assert balances[0].remaining == amount
    assert balances[0].currency == currency


@pytest.mark.parametrize('kind,payload', [
    ('deepseek', {'balance_infos': [{'currency': 'USD'}]}),
    ('openrouter', {'data': {'total_credits': 10}}),
    ('stepfun', {'balance': True}),
    ('moonshot', {'data': {'available_balance': 'NaN'}}),
    ('zhipu', {'data': {'limits': [{'type': 'TOKENS_LIMIT', 'unit': 3}]}}),
    ('opencode-go', {'usage': {'rolling': {'status': 'ok'}}}),
    ('kimi-code', {'usage': {'limit': '100'}}),
    ('minimax', {'base_resp': {'status_code': 0}, 'model_remains': []}),
    ('ollama', {'included': {'allowance_usd': 20}}),
])
def test_missing_is_not_zero_or_full(kind, payload):
    with pytest.raises(QuotaUnavailable):
        normalize(kind, payload)


def test_deepseek_preserves_currencies_including_actual_zero():
    _, balances, _ = normalize('deepseek', {'balance_infos': [
        {'currency': 'CNY', 'total_balance': '20'}, {'currency': 'USD', 'total_balance': '0'}]})
    assert {(b.currency, b.remaining) for b in balances} == {('CNY', 20), ('USD', 0)}


def test_zhipu_scopes_reset_milliseconds_and_no_missing_percentage():
    windows, _, plan = normalize('zhipu', {'success': True, 'data': {'level': 'pro', 'limits': [
        {'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 20, 'nextResetTime': 1800000000000},
        {'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 40},
        {'type': 'TIME_LIMIT', 'unit': 5, 'percentage': 0},
        {'type': 'TOKENS_LIMIT', 'unit': 3},
    ]}})
    assert plan == 'pro' and len(windows) == 3
    assert windows[0].resets_at == 1800000000 and windows[0].window_duration_mins == 300
    assert windows[1].window_duration_mins == 10080
    assert windows[2].label == 'MCP · 每月'


def test_opencode_zero_does_not_show_artificial_reset():
    windows, _, _ = normalize('opencode-go', {'usage': {'rolling': {'percent': 0, 'resetsAt': '2026-10-20T01:00:00Z'}, 'weekly': {'percent': 35, 'resetsAt': '2026-10-23T01:00:00Z'}}})
    assert windows[0].resets_at is None and windows[1].resets_at


def test_kimi_short_and_weekly_pools():
    windows, _, _ = normalize('kimi-code', {'usage': {'limit': '100', 'used': '12', 'resetTime': '2026-10-23T01:00:00Z'}, 'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'}, 'detail': {'limit': '200', 'remaining': '150'}}]})
    assert [w.used_percent for w in windows] == [25, 12]
    assert [w.window_duration_mins for w in windows] == [300, 10080]


def test_minimax_exhausted_and_unlimited_are_distinct():
    windows, _, _ = normalize('minimax', {'base_resp': {'status_code': 0}, 'model_remains': [
        {'model_name': 'general', 'current_interval_remaining_percent': 80, 'current_interval_status': 2, 'current_weekly_status': 3},
        {'model_name': 'video', 'current_interval_remaining_percent': 80, 'current_interval_status': 1, 'current_interval_total_count': 5, 'current_interval_usage_count': 4},
    ]})
    assert len(windows) == 2
    assert windows[0].used_percent == 100
    assert windows[1].remaining_amount == 4 and windows[1].limit_amount == 5


def test_commandcode_and_ollama_balance_and_windows_coexist():
    windows, balances, _ = normalize('commandcode', {'credits': {'monthlyCredits': 8, 'purchasedCredits': 3, 'freeCredits': 1}, 'windowLimits': {'fiveHour': {'cap': 10, 'used': 2.5, 'resetAt': 1800000000000}}})
    assert windows[0].used_percent == 25 and balances[0].remaining == 12
    windows, balances, _ = normalize('ollama', {'included': {'allowance_usd': 20, 'balance_usd': 15, 'period': {'from': '2026-10-01T00:00:00Z', 'until': '2026-11-01T00:00:00Z'}}, 'purchased': {'balance_usd': 3}})
    assert windows[0].remaining_amount == 15 and windows[0].limit_amount == 20 and balances[0].remaining == 3


@pytest.mark.parametrize('base', [
    'http://api.deepseek.com', 'https://api.deepseek.com.evil.test', 'https://api.deepseek.com@evil.test',
    'https://user@api.deepseek.com', 'https://api.deepseek.com:444', 'https://api.deepseek.com?proxy=x',
    'https://localhost', 'https://api.kimi.com/v1', 'https://opencode.ai/zen/v1',
])
def test_unrelated_origin_or_non_plan_does_not_receive_key(base):
    assert endpoint_for(base) is None


async def test_exact_auth_dedup_partial_failure_and_no_redirect():
    requests = []
    def reply(request):
        requests.append(request)
        if request.url.host == 'api.deepseek.com':
            assert request.headers['Authorization'] == 'Bearer test-only-a'
            return httpx.Response(200, json={'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]})
        if request.url.host == 'open.bigmodel.cn':
            assert request.headers['Authorization'] == 'test-only-b'
            return httpx.Response(401, json={'error': 'never expose upstream text'})
        return httpx.Response(302, headers={'Location': 'https://untrusted.test/steal'})
    result = await read_sources([
        ProviderSource('https://api.deepseek.com/v1', 'test-only-a'),
        ProviderSource('https://api.deepseek.com', 'test-only-a'),
        ProviderSource('https://open.bigmodel.cn/api/coding/paas/v4', 'test-only-b'),
        ProviderSource('https://api.moonshot.cn/v1', 'test-only-c'),
        ProviderSource('https://api.deepseek.com.evil.test', 'must-never-send'),
    ], transport=httpx.MockTransport(reply))
    assert len(requests) == 3 and len(result.accounts) == 1 and len(result.issues) == 3
    assert {issue.state for issue in result.issues} == {'unauthenticated', 'unavailable', 'unsupported'}
    assert next(issue for issue in result.issues if issue.setup_provider == 'custom').setup_secrets == {'apiKey': 'must-never-send'}
    assert 'test-only-' not in repr(result)
    assert 'must-never-send' not in repr(ProviderSource('https://api.deepseek.com', 'must-never-send'))


async def test_retired_siliconflow_and_bounded_responses():
    result = await read_sources([ProviderSource('https://api.siliconflow.cn/v1', 'fixture')], transport=httpx.MockTransport(lambda r: httpx.Response(410)))
    assert result.issues[0].reason == 'endpoint_retired'
    result = await read_sources([ProviderSource('https://api.deepseek.com', 'fixture')], transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 1000001)))
    assert result.issues[0].reason == 'quota_not_returned'


async def test_openrouter_regular_key_budget_is_not_account_money():
    def reply(request):
        if request.url.path == '/api/v1/credits':
            return httpx.Response(403)
        assert request.url.path == '/api/v1/key'
        return httpx.Response(200, json={'data': {'limit': 10, 'limit_remaining': 7}})
    result = await read_sources([ProviderSource('https://openrouter.ai/api/v1', 'fixture')], transport=httpx.MockTransport(reply))
    assert result.accounts[0].balances[0].label == '密钥可用额度'
    assert result.accounts[0].balances[0].remaining == 7
    with pytest.raises(QuotaUnavailable, match='unsupported') as exc:
        normalize('openrouter-key', {'data': {'limit': None, 'limit_remaining': None, 'usage': 5}})
    assert exc.value.reason == 'management_key_required'


def driver(family, env):
    return SimpleNamespace(preset=SimpleNamespace(id=family, quota_provider_adapter='drivers.hermes.quota_sources' if family in ('hermes', 'hermes-acp') else None), agent_spec=SimpleNamespace(build_env=lambda: env))


def test_codex_reads_only_its_configured_provider_and_env(tmp_path):
    root = tmp_path / 'codex'; root.mkdir()
    (root / 'config.toml').write_text('[model_providers.ds]\nbase_url="https://api.deepseek.com/v1"\nenv_key="MY_DS_KEY"\n[model_providers.proxy]\nbase_url="https://relay.example/v1"\nenv_key="PROXY_KEY"\n')
    sources = discover_provider_sources(driver('codex', {'HOME': str(tmp_path), 'CODEX_HOME': str(root), 'MY_DS_KEY': 'synthetic', 'PROXY_KEY': 'do-not-forward'}))
    assert len(sources) == 2 and sources[0].api_key == 'synthetic'
    assert sources[1].base_url == 'https://relay.example/v1'


def test_claude_custom_endpoint_uses_native_settings(tmp_path):
    root = tmp_path / '.claude'; root.mkdir()
    (root / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://api.minimax.io/anthropic', 'ANTHROPIC_AUTH_TOKEN': 'synthetic'}}))
    sources = discover_provider_sources(driver('claude-code', {'HOME': str(tmp_path)}))
    assert len(sources) == 1 and sources[0].api_key == 'synthetic'


def test_hermes_does_not_eval_credentials_or_scan_other_homes(tmp_path, monkeypatch):
    root = tmp_path / 'hermes'; root.mkdir()
    (root / 'config.yaml').write_text('providers:\n  deepseek:\n    base_url: https://api.deepseek.com\n    key_env: DS_TEST\n  other:\n    base_url: https://api.moonshot.cn/v1\n    api_key: "!echo prohibited"\n')
    (root / '.env').write_text('DS_TEST="synthetic"\n')
    instance = driver('hermes', {'HOME': str(tmp_path)})
    instance.hermes_root = root
    sources = discover_provider_sources(instance)
    assert len(sources) == 1
    assert sources[0].api_key == 'synthetic'


def test_opencode_uses_matching_auth_entry(tmp_path):
    config = tmp_path / '.config/opencode'; config.mkdir(parents=True)
    auth = tmp_path / '.local/share/opencode'; auth.mkdir(parents=True)
    (config / 'opencode.json').write_text(json.dumps({'provider': {'deepseek': {'options': {'baseURL': 'https://api.deepseek.com/v1'}}}}))
    (auth / 'auth.json').write_text(json.dumps({'deepseek': {'type': 'api', 'key': 'right'}, 'other': {'type': 'api', 'key': 'wrong'}}))
    sources = discover_provider_sources(driver('opencode', {'HOME': str(tmp_path)}))
    assert len(sources) == 1 and sources[0].api_key == 'right'


def test_unknown_driver_does_not_collect_global_env_key(tmp_path):
    assert discover_provider_sources(driver('unrelated', {'HOME': str(tmp_path), 'OPENAI_BASE_URL': 'https://api.deepseek.com', 'OPENAI_API_KEY': 'unrelated'})) == ()


def test_hermes_relay_override_prevents_builtin_official_request(tmp_path):
    root = tmp_path / '.hermes'; root.mkdir()
    (root / 'config.yaml').write_text('model:\n  provider: deepseek\n')
    value = driver('hermes', {'HOME': str(tmp_path), 'OPENAI_BASE_URL': 'https://relay.example/v1', 'DEEPSEEK_API_KEY': 'must-stay-local'})
    sources = discover_provider_sources(value)
    assert len(sources) == 1 and sources[0].base_url == 'https://relay.example/v1' and sources[0].api_key == ''


def test_inherit_env_false_and_custom_home_do_not_read_process_credentials(tmp_path, monkeypatch):
    from drivers.acp.client import AcpAgentSpec
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('OPENAI_API_KEY', 'never-inherited')
    value = SimpleNamespace(preset=SimpleNamespace(id='codex'), agent_spec=AcpAgentSpec(command=('unused',), env={'HOME': str(tmp_path)}, inherit_env=False))
    assert discover_provider_sources(value) == ()


@pytest.mark.parametrize('value', [True, False, 'NaN', 'Infinity', float('inf'), None, {}])
def test_non_finite_and_non_numeric_are_unknown(value):
    assert number(value) is None


def test_opencode_builtin_auth_and_disabled_relay_override(tmp_path):
    config = tmp_path / '.config/opencode'; config.mkdir(parents=True)
    auth = tmp_path / '.local/share/opencode'; auth.mkdir(parents=True)
    (auth / 'auth.json').write_text(json.dumps({
        'deepseek': {'type': 'api', 'key': 'native-ds'},
        'openrouter': {'type': 'api', 'key': 'relay-key'},
        'opencode-go': {'type': 'api', 'key': 'go-key'},
        'zai': {'type': 'api', 'key': 'disabled-key'},
        'arbitrary': {'type': 'api', 'key': 'no-generic-scan'},
    }))
    (config / 'opencode.jsonc').write_text('''{ // trailing commas are valid JSONC
      "disabled_providers": ["zai",], "provider": {
        "openrouter": {"options": {"baseURL": "https://relay.example/v1",},},
      },
    }''')
    sources = discover_provider_sources(driver('opencode', {'HOME': str(tmp_path)}))
    assert {s.api_key for s in sources} == {'native-ds', 'go-key', 'relay-key'}
    assert endpoint_for(next(s.base_url for s in sources if s.api_key == 'go-key'))[1].parser == 'opencode-go'


@pytest.mark.parametrize('family,folder', [('pi', '.pi/agent'), ('oh-my-pi', '.omp/agent')])
def test_pi_builtin_native_auth_keeps_custom_origin(family, folder, tmp_path):
    root = tmp_path / folder; root.mkdir(parents=True)
    (root / 'auth.json').write_text(json.dumps({
        'deepseek': {'type': 'api_key', 'key': 'native-ds'},
        'openrouter': {'type': 'api_key', 'key': 'private-relay'},
        'kimi-coding': {'type': 'api_key', 'key': '!never-execute'},
        'google': {'type': 'oauth', 'access': 'not-api-key'},
    }))
    (root / 'models.json').write_text(json.dumps({'providers': {'openrouter': {'baseUrl': 'https://relay.example/v1'}}}))
    sources = discover_provider_sources(driver(family, {'HOME': str(tmp_path)}))
    assert {s.api_key for s in sources} == {'native-ds', '', 'private-relay'}
    assert next(s for s in sources if s.api_key == 'private-relay').base_url == 'https://relay.example/v1'


def test_hermes_profile_dotenv_overrides_inherited_key(tmp_path):
    root = tmp_path / '.hermes'; root.mkdir()
    (root / 'config.yaml').write_text('model:\n  provider: deepseek\n')
    (root / '.env').write_text('DEEPSEEK_API_KEY=profile-key\n')
    sources = discover_provider_sources(driver('hermes', {'HOME': str(tmp_path), 'DEEPSEEK_API_KEY': 'parent-key'}))
    assert sources[0].api_key == 'profile-key'


def test_dsh_native_credential_reference_and_route_precedence(tmp_path):
    root = tmp_path / '.dsh'; root.mkdir()
    (root / 'settings.yaml').write_text('llm-deepseek:\n  apiKeyEnv: PRIVATE_REF\n  baseURL: https://api.deepseek.com\n')
    (root / '.credentials.yaml').write_text('version: 1\nrefs:\n  PRIVATE_REF: stored-key\n')
    sources = discover_provider_sources(driver('dsh', {'HOME': str(tmp_path), 'DEEPSEEK_BASE_URL': 'https://relay.example'}))
    assert sources[0].api_key == 'stored-key'
    (root / 'settings.yaml').write_text('llm-deepseek:\n  apiKeyEnv: PRIVATE_REF\n  baseURL: https://relay.example\n')
    assert discover_provider_sources(driver('dsh', {'HOME': str(tmp_path)}))[0].base_url == 'https://relay.example'


def test_goose_reads_only_selected_provider_file_fallback(tmp_path):
    root = tmp_path / 'custom/config'; root.mkdir(parents=True)
    (root / 'config.yaml').write_text('GOOSE_PROVIDER: openrouter\nGOOSE_DISABLE_KEYRING: true\n')
    (root / 'secrets.yaml').write_text('OPENROUTER_API_KEY: stored-goose-key\n')
    env = {'HOME': str(tmp_path), 'GOOSE_PATH_ROOT': str(root.parent)}
    sources = discover_provider_sources(driver('goose', env))
    assert sources[0].api_key == 'stored-goose-key'
    env['OPENROUTER_HOST'] = 'https://relay.example'
    assert discover_provider_sources(driver('goose', env))[0].base_url == 'https://relay.example'


def test_zhipu_amount_only_team_and_unlimited():
    ws, _, _ = normalize('zhipu', {'data': {'limits': [
        {'type': 'CREDIT_LIMIT', 'unit': 3, 'usage': 2000, 'remaining': 1500},
        {'type': 'CREDIT_LIMIT', 'unit': 6, 'usage': 8000, 'currentValue': 2000},
        {'type': 'TIME_LIMIT', 'unit': 5, 'usage': 0, 'percentage': 100},
    ]}})
    assert [w.used_percent for w in ws] == [25, 25, 0]
    assert ws[0].remaining_amount == 1500 and ws[1].remaining_amount == 6000
    assert ws[2].unlimited is True


async def test_total_budget_retains_success_and_cancels_pending_requests():
    import asyncio
    cancelled = asyncio.Event()
    async def reply(request):
        if request.url.host == 'api.deepseek.com':
            return httpx.Response(200, json={'balance_infos': [{'currency': 'CNY', 'total_balance': '2'}]})
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
    result = await read_sources([ProviderSource('https://api.deepseek.com', 'one'), ProviderSource('https://api.moonshot.cn', 'two')], transport=httpx.MockTransport(reply), total_timeout=.01)
    assert len(result.accounts) == 1 and len(result.issues) == 1 and cancelled.is_set()


async def test_cancelling_refresh_cancels_children():
    import asyncio
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def reply(request):
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
    task = asyncio.create_task(read_sources([ProviderSource('https://api.deepseek.com', 'one')], transport=httpx.MockTransport(reply)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_claude_settings_override_inherited_shell_account(tmp_path):
    root=tmp_path/'.claude';root.mkdir()
    (root/'settings.json').write_text(json.dumps({'env':{'ANTHROPIC_BASE_URL':'https://api.minimax.io/anthropic','ANTHROPIC_AUTH_TOKEN':'settings-account'}}))
    sources=discover_provider_sources(driver('claude-code',{'HOME':str(tmp_path),'ANTHROPIC_AUTH_TOKEN':'parent-account'}))
    assert sources[0].api_key=='settings-account'


def test_opencode_deep_merge_order_and_explicit_empty_key(tmp_path):
    root=tmp_path/'.config/opencode';root.mkdir(parents=True)
    data=tmp_path/'.local/share/opencode';data.mkdir(parents=True)
    (data/'auth.json').write_text(json.dumps({'deepseek':{'type':'api','key':'stored-key'}}))
    (root/'opencode.json').write_text(json.dumps({'provider':{'deepseek':{'options':{'apiKey':'lower-precedence','baseURL':'https://relay.example'}},'moonshotai':{'options':{'apiKey':'retained-other'}}}}))
    (root/'opencode.jsonc').write_text(json.dumps({'provider':{'deepseek':{'options':{'apiKey':'','baseURL':'https://api.deepseek.com'}}}}))
    sources=discover_provider_sources(driver('opencode',{'HOME':str(tmp_path),'DEEPSEEK_API_KEY':'shell-key'}))
    assert {s.api_key for s in sources}=={'','retained-other'}


async def test_permission_denial_is_not_expired_login():
    result=await read_sources([ProviderSource('https://api.deepseek.com','fixture')],transport=httpx.MockTransport(lambda r:httpx.Response(403)))
    assert result.issues[0].reason=='permission_required' and result.issues[0].state=='unavailable'


def test_dsh_platform_grant_does_not_require_second_api_key(tmp_path):
    root=tmp_path/'.dsh';root.mkdir()
    (root/'.credentials.yaml').write_text('version: 1\nrecords:\n  deepseek-account-platform/default:\n    kind: grant\n    payload:\n      version: 1\n      issuer: https://platform.deepseek.com\n      token: fixture\n')
    assert discover_provider_sources(driver('dsh',{'HOME':str(tmp_path)}))==()


@pytest.mark.parametrize('family,folder',[('pi','.pi/agent'),('oh-my-pi','.omp/agent')])
def test_pi_stored_account_precedes_env_and_models(family,folder,tmp_path):
    root=tmp_path/folder;root.mkdir(parents=True)
    (root/'auth.json').write_text(json.dumps({'deepseek':{'type':'api_key','key':'stored-account'}}))
    (root/'models.json').write_text(json.dumps({'providers':{'deepseek':{'apiKey':'model-account'}}}))
    sources=discover_provider_sources(driver(family,{'HOME':str(tmp_path),'DEEPSEEK_API_KEY':'environment-account'}))
    assert len(sources)==1 and sources[0].api_key=='stored-account'


def test_pi_stored_oauth_never_falls_back_to_other_api_account(tmp_path):
    root=tmp_path/'.pi/agent';root.mkdir(parents=True)
    (root/'auth.json').write_text(json.dumps({'deepseek':{'type':'oauth','access':'opaque-native-token'}}))
    (root/'models.json').write_text(json.dumps({'providers':{'deepseek':{'apiKey':'other-account'}}}))
    assert discover_provider_sources(driver('pi',{'HOME':str(tmp_path),'DEEPSEEK_API_KEY':'environment-account'}))==()


def test_pi_conflicting_version_dependent_fallbacks_are_not_guessed(tmp_path):
    root=tmp_path/'.pi/agent';root.mkdir(parents=True)
    (root/'models.json').write_text(json.dumps({'providers':{'deepseek':{'apiKey':'model-account'}}}))
    assert discover_provider_sources(driver('pi',{'HOME':str(tmp_path),'DEEPSEEK_API_KEY':'environment-account'}))==()
    sources=discover_provider_sources(driver('pi',{'HOME':str(tmp_path),'DEEPSEEK_API_KEY':'model-account'}))
    assert len(sources)==1 and sources[0].api_key=='model-account'


def test_pi_stored_credential_environment_resolves_without_execution(tmp_path):
    root=tmp_path/'.pi/agent';root.mkdir(parents=True)
    (root/'auth.json').write_text(json.dumps({'deepseek':{'type':'api_key','key':'EXPLICIT_KEY','env':{'EXPLICIT_KEY':'stored-account'}}}))
    sources=discover_provider_sources(driver('pi',{'HOME':str(tmp_path),'EXPLICIT_KEY':'parent-account'}))
    assert len(sources)==1 and sources[0].api_key=='stored-account'
