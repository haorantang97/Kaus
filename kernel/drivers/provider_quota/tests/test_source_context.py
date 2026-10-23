from dataclasses import asdict
from types import SimpleNamespace
import json

import httpx
import pytest

from drivers.account_quota import AccountQuota, QuotaIssue
from drivers.provider_quota import ProviderSource, discover_provider_sources, read_sources
from drivers.provider_quota.source_context import normalized_source_url, setup_issue_for, source_id_for


def driver(family, env):
    return SimpleNamespace(preset=SimpleNamespace(id=family,
        quota_provider_adapter='drivers.hermes.quota_sources' if family == 'hermes' else None),
        agent_spec=SimpleNamespace(build_env=lambda: env))


def no_request(request):
    raise AssertionError('Setup discovery must not make any request')


def test_additive_contract_defaults_preserve_native_readers():
    account = AccountQuota('Native', 'Native', ())
    issue = QuotaIssue('unavailable', 'Native', 'request_failed')
    assert account.source_id is None
    assert issue.source_id is None and issue.setup_provider is None and issue.setup_secrets is None


def test_source_identity_normalizes_origin_but_keeps_route_and_credential():
    key = 'synthetic-sensitive-key'
    source_id = source_id_for('https://API.STEPFUN.COM:443/step_plan/v1/', key)
    assert source_id == source_id_for('https://api.stepfun.com/step_plan/v1', key)
    assert source_id.startswith('provider-source:') and len(source_id) == 80
    assert key not in source_id and 'stepfun' not in source_id
    assert source_id != source_id_for('https://api.stepfun.com/v1', key)
    assert source_id != source_id_for('https://api.stepfun.com/step_plan/v1', 'rotated-key')


@pytest.mark.parametrize('first,second', [
    ('https://api.deepseek.com', 'https://api.deepseek.com/v1/'),
    ('https://openrouter.ai/api/v1', 'https://openrouter.ai/'),
    ('https://aihubmix.com/v1', 'https://aihubmix.com'),
    ('https://ark.cn-beijing.volces.com/api/coding', 'https://ark.cn-beijing.volces.com/api/coding/v3'),
    ('https://tokenhub.tencentmaas.com/plan', 'https://tokenhub.tencentmaas.com/plan/v1'),
    ('https://api.stepfun.com/step_plan', 'https://api.stepfun.com/step_plan/v1'),
    ('https://api.z.ai/api/coding/paas/v4', 'https://api.z.ai/api/coding/anthropic/v1'),
])
def test_equivalent_vendor_query_or_plan_aliases_have_stable_source_identity(first, second):
    assert source_id_for(first, 'same-account') == source_id_for(second, 'same-account')


@pytest.mark.parametrize('first,second', [
    ('https://relay.example/first/v1', 'https://relay.example/second/v1'),
    ('https://ark.cn-beijing.volces.com/api/coding/v3', 'https://ark.cn-beijing.volces.com/api/plan/v3'),
    ('https://ark.cn-beijing.volces.com/api/plan/v3', 'https://ark.ap-southeast-1.volces.com/api/plan/v3'),
    ('https://api.z.ai/api/coding/paas/v4', 'https://api.z.ai/api/paas/v4'),
    ('https://api.stepfun.com/step_plan/v1', 'https://api.stepfun.com/v1'),
])
def test_custom_routes_and_distinct_plan_scopes_never_share_source_identity(first, second):
    assert source_id_for(first, 'same-key') != source_id_for(second, 'same-key')


async def test_alias_order_cannot_change_management_credential_binding():
    urls = ['https://openrouter.ai/api/v1', 'https://openrouter.ai/']
    calls = []
    def reply(request):
        calls.append(request)
        return httpx.Response(403) if request.url.path == '/api/v1/credits' else httpx.Response(200, json={'data': {'limit': None}})
    one = await read_sources([ProviderSource(url, 'key') for url in urls], transport=httpx.MockTransport(reply))
    two = await read_sources([ProviderSource(url, 'key') for url in reversed(urls)], transport=httpx.MockTransport(reply))
    assert len(one.issues) == len(two.issues) == 1
    assert one.issues[0].source_id == two.issues[0].source_id
    assert len(calls) == 4


async def test_distinct_configured_plan_scopes_keep_all_source_context_even_when_query_shared():
    sources = [ProviderSource(url, 'key') for url in ['https://api.z.ai/api/coding/paas/v4', 'https://api.z.ai/api/paas/v4']]
    result = await read_sources(sources, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': {'limits': [{'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 20}]}})))
    assert len(result.accounts) == 2
    assert {a.source_id for a in result.accounts} == {s.source_id for s in sources}


@pytest.mark.parametrize('url', [
    'http://relay.example/v1', 'https://user:secret@relay.example/v1',
    'https://relay.example:444/v1', 'https://relay.example/v1?api_key=secret',
    'https://relay.example/#secret', 'https://relay.example/\nsecret',
    'https://relay.example /v1', 'https:///v1', None, {},
])
async def test_invalid_source_metadata_is_not_an_actionable_setup(url):
    assert normalized_source_url(url) is None and source_id_for(url, 'secret') is None
    result = await read_sources([ProviderSource(url, 'secret')], transport=httpx.MockTransport(no_request))
    assert not result.accounts and not result.issues


@pytest.mark.parametrize('url,provider,config,reason', [
    ('https://ark.cn-beijing.volces.com/api/coding/v3', 'volcengine', {'region': 'cn-beijing', 'plan': 'coding'}, 'cloud_credentials_required'),
    ('https://ark.ap-southeast-1.volces.com/api/plan/v3', 'volcengine', {'region': 'ap-southeast-1', 'plan': 'agent'}, 'cloud_credentials_required'),
    ('https://tokenhub.tencentmaas.com/plan/v1', 'tencent', {'region': 'ap-guangzhou'}, 'cloud_credentials_required'),
    ('https://tokenhub-intl.tencentmaas.com/plan', 'tencent', {'region': 'ap-singapore'}, 'cloud_credentials_required'),
    ('https://api.stepfun.com/step_plan/v1', 'stepfun-plan', {'site': 'com'}, 'web_session_required'),
    ('https://api.stepfun.ai/step_plan/v1/', 'stepfun-plan', {'site': 'ai'}, 'web_session_required'),
])
async def test_plan_origin_and_route_choose_exact_credential_without_sending_inference_key(url, provider, config, reason):
    source = ProviderSource(url, 'inference-key-only')
    result = await read_sources([source, source], transport=httpx.MockTransport(no_request))
    assert not result.accounts and len(result.issues) == 1
    issue = result.issues[0]
    assert issue.source_id == source.source_id and issue.reason == reason
    assert issue.setup_provider == provider and issue.setup_config == config
    assert issue.setup_secrets is None
    assert 'inference-key-only' not in repr(issue) and 'inference-key-only' not in json.dumps(asdict(issue))


@pytest.mark.parametrize('url', [
    'https://ark.cn-beijing.volces.com/api/v3',
    'https://ark.cn-beijing.volces.com/api/coding-other',
    'https://tokenhub.tencentmaas.com/v1',
    'https://tokenhub.tencentmaas.com/plans/v1',
    'https://api.lkeap.cloud.tencent.com/coding/v3',
    'https://api.openai.com/v1', 'https://api.anthropic.com/v1',
])
async def test_non_plan_first_party_endpoint_does_not_invent_custom_or_plan_setup(url):
    result = await read_sources([ProviderSource(url, 'key')], transport=httpx.MockTransport(no_request))
    assert result.issues[0].source_id and result.issues[0].setup_provider is None


@pytest.mark.parametrize('url', [
    'https://ark.cn-beijing.volces.com.evil.example/api/coding/v3',
    'https://tokenhub.tencentmaas.com.evil.example/plan/v1',
    'https://api.stepfun.com.evil.example/step_plan/v1',
])
async def test_similar_host_does_not_receive_official_plan_classification(url):
    result = await read_sources([ProviderSource(url, 'key')], transport=httpx.MockTransport(no_request))
    assert result.issues[0].setup_provider == 'custom'


async def test_custom_source_only_offers_metadata_and_private_same_origin_credential():
    source = ProviderSource('https://relay.example/inference/v1', 'keep-private')
    result = await read_sources([source], transport=httpx.MockTransport(no_request))
    issue = result.issues[0]
    assert issue.setup_provider == 'custom' and issue.setup_config == {}
    assert issue.source_id == source.source_id and issue.setup_origin == 'https://relay.example'
    assert issue.setup_secrets == {'apiKey': 'keep-private'}
    assert 'keep-private' not in repr(issue) and 'inference/v1' not in repr(issue)


@pytest.mark.parametrize('key', ['', '\nkey', 'x' * 16385])
def test_custom_invalid_or_missing_credential_is_never_reused(key):
    issue = setup_issue_for('https://relay.example', key)
    assert issue.setup_provider == 'custom' and issue.setup_secrets is None


@pytest.mark.parametrize('status', [401, 403, 429, 503])
@pytest.mark.parametrize('url', ['https://api.stepfun.com/v1', 'https://api.deepseek.com', 'https://aihubmix.com/v1'])
async def test_ordinary_login_permission_or_transient_failure_does_not_create_extra_login_form(url, status):
    source = ProviderSource(url, 'fixture')
    result = await read_sources([source], transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    assert result.issues[0].source_id == source.source_id
    assert result.issues[0].setup_provider is None and result.issues[0].setup_secrets is None


async def test_successful_api_accounts_have_context_and_never_setup():
    sources = [ProviderSource('https://api.stepfun.com/v1', 'step'), ProviderSource('https://api.deepseek.com', 'ds')]
    def reply(request):
        return httpx.Response(200, json={'balance': 0} if request.url.host == 'api.stepfun.com' else {'balance_infos': [{'currency': 'CNY', 'total_balance': '1'}]})
    result = await read_sources(sources, transport=httpx.MockTransport(reply))
    assert len(result.accounts) == 2 and not result.issues
    assert {a.source_id for a in result.accounts} == {s.source_id for s in sources}


@pytest.mark.parametrize('limited', [True, False])
async def test_openrouter_key_budget_satisfies_reading_only_unlimited_key_needs_management(limited):
    calls = []
    def reply(request):
        calls.append(request)
        if request.url.path == '/api/v1/credits':
            return httpx.Response(403)
        return httpx.Response(200, json={'data': {'limit': 10 if limited else None, 'limit_remaining': 0 if limited else None}})
    source = ProviderSource('https://openrouter.ai/api/v1', 'inference')
    result = await read_sources([source], transport=httpx.MockTransport(reply))
    assert len(calls) == 2
    if limited:
        assert result.accounts[0].balances[0].remaining == 0 and not result.issues
        assert result.accounts[0].source_id == source.source_id
    else:
        issue = result.issues[0]
        assert not result.accounts and issue.source_id == source.source_id
        assert issue.setup_provider == 'openrouter' and issue.setup_config == {}
        assert issue.setup_secrets is None and issue.setup_origin == 'https://openrouter.ai'


async def test_aihubmix_explicit_unlimited_key_has_precise_management_setup():
    source = ProviderSource('https://aihubmix.com/v1', 'inference')
    result = await read_sources([source], transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'total_usage': -1})))
    issue = result.issues[0]
    assert issue.source_id == source.source_id and issue.setup_provider == 'aihubmix'
    assert issue.setup_config == {} and issue.setup_secrets is None


@pytest.mark.parametrize('payload', [{}, {'data': {}}, {'data': {'limit': 'NaN', 'limit_remaining': 1}}, {'data': {'limit': 10}}])
async def test_openrouter_malformed_key_response_is_not_management_credential_requirement(payload):
    def reply(request):
        return httpx.Response(403) if request.url.path == '/api/v1/credits' else httpx.Response(200, json=payload)
    result = await read_sources([ProviderSource('https://openrouter.ai/api/v1', 'fixture')], transport=httpx.MockTransport(reply))
    assert result.issues[0].reason == 'quota_not_returned'
    assert result.issues[0].setup_provider is None


@pytest.mark.parametrize('family', ['codex', 'claude-code', 'hermes', 'openclaw'])
async def test_configured_sources_are_retained_without_scanning_unrelated_agent_credentials(family, tmp_path):
    plan_url = 'https://ark.cn-beijing.volces.com/api/coding/v3'
    unknown_url = 'https://relay.example/v1'
    (tmp_path / '.pi/agent').mkdir(parents=True)
    (tmp_path / '.pi/agent/auth.json').write_text(json.dumps({'openrouter': {'type': 'api_key', 'key': 'must-not-read'}}))
    if family == 'codex':
        root = tmp_path / '.codex'; root.mkdir()
        (root / 'config.toml').write_text(f'[model_providers.plan]\nbase_url="{plan_url}"\nenv_key="PLAN_KEY"\n[model_providers.relay]\nbase_url="{unknown_url}"\nenv_key="RELAY_KEY"\n')
    elif family == 'claude-code':
        root = tmp_path / '.claude'; root.mkdir()
        (root / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': plan_url, 'ANTHROPIC_AUTH_TOKEN': 'plan-key'}}))
    elif family == 'hermes':
        root = tmp_path / '.hermes'; root.mkdir()
        (root / 'config.yaml').write_text(f'model:\n  base_url: {plan_url}\n  api_key: plan-key\nproviders:\n  relay:\n    base_url: {unknown_url}\n    api_key: relay-key\n')
    else:
        root = tmp_path / '.openclaw'; root.mkdir()
        (root / 'openclaw.json').write_text(json.dumps({'models': {'providers': {'plan': {'baseUrl': plan_url, 'apiKey': 'plan-key'}, 'relay': {'baseUrl': unknown_url, 'apiKey': 'relay-key'}}}}))
    sources = discover_provider_sources(driver(family, {'HOME': str(tmp_path), 'PLAN_KEY': 'plan-key', 'RELAY_KEY': 'relay-key'}))
    assert sources and all(s.api_key != 'must-not-read' for s in sources)
    result = await read_sources(sources, transport=httpx.MockTransport(no_request))
    assert any(issue.setup_provider == 'volcengine' for issue in result.issues)
    if family != 'claude-code':
        assert any(issue.setup_provider == 'custom' for issue in result.issues)
