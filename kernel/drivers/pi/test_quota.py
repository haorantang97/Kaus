import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable, QuotaWindow
from drivers.pi import quota, vendor_quota as vendor


def grant(**values):
    return {'type': 'oauth', 'access': 'pi-access', 'refresh': 'pi-refresh',
            'expires': (time.time() + 3600) * 1000, **values}


def driver(tmp_path, credentials=None, models=None):
    root = tmp_path / 'pi-specific'
    root.mkdir(exist_ok=True)
    if credentials is not None:
        (root / 'auth.json').write_text(json.dumps(credentials))
    if models is not None:
        (root / 'models.json').write_text(json.dumps({'providers': models}))
    env = {'HOME': str(tmp_path), 'PI_CODING_AGENT_DIR': str(root), 'ANTHROPIC_AUTH_TOKEN': 'other-cli-token'}
    return SimpleNamespace(agent_spec=SimpleNamespace(build_env=lambda: env)), root


async def test_missing_pi_store_does_not_borrow_other_cli_or_env(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path)
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/auth.json').write_text(json.dumps({'tokens': {'access_token': 'not-pi'}}))
    async def fail(*args, **kwargs):
        pytest.fail('No network or native CLI credential lookup expected')
    monkeypatch.setattr(vendor, 'request_json', fail)
    assert await quota.read_account_quota(instance) == QuotaReadResult()


async def test_api_keys_are_left_for_provider_adapter(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'deepseek': {'type': 'api_key', 'key': 'api'}})
    assert await quota.read_account_quota(instance) == QuotaReadResult()


async def test_codex_reads_pi_account_and_deduplicates_matching_native_account(tmp_path, monkeypatch):
    payload = {'https://api.openai.com/auth': {'chatgpt_account_id': 'workspace-pi'},
               'https://api.openai.com/profile': {'email': 'tester@example.test'}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    bearer = 'header.' + encoded + '.signature'
    instance, root = driver(tmp_path, {'openai-codex': grant(access=bearer, accountId='old-workspace')})
    before = (root / 'auth.json').read_bytes()
    calls = []
    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {'plan_type': 'pro', 'rate_limit': {'primary_window': {
            'used_percent': 0, 'limit_window_seconds': 18000, 'reset_at': 2_000_000_000}}}
    monkeypatch.setattr(vendor, 'request_json', request)
    result = await quota.read_account_quota(instance)
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.provider == 'OpenAI'
    assert account.account_key == hashlib.sha256(b'workspace-pi').hexdigest()
    assert account.account_label == 'ChatGPT · t***@example.test'
    assert account.windows[0].used_percent == 0
    assert account.windows[0].window_duration_mins == 300
    assert calls[0][:2] == ('GET', 'https://chatgpt.com/backend-api/wham/usage')
    assert calls[0][2]['headers']['Authorization'] == 'Bearer ' + bearer
    assert calls[0][2]['headers']['ChatGPT-Account-Id'] == 'workspace-pi'
    assert (root / 'auth.json').read_bytes() == before


def test_codex_missing_percent_is_unknown_and_additional_pools_are_preserved():
    with pytest.raises(QuotaUnavailable): vendor.normalize_codex({'rate_limit': {'primary_window': {}}})
    result = vendor.normalize_codex({'rate_limit': {'primary_window': {'used_percent': 100}},
        'additional_rate_limits': [{'limit_id': 'fast', 'limit_name': 'Fast',
            'rate_limit': {'secondary_window': {'used_percent': 20, 'limit_window_seconds': 604800}}}]})
    assert [(x.label, x.used_percent) for x in result.windows] == [('Codex', 100), ('Fast', 20)]
    assert result.windows[1].window_duration_mins == 10080


async def test_copilot_uses_pi_github_token_not_inference_token(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'github-copilot': grant(access='inference', refresh='github-token', expires=1)})
    calls = []
    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert kwargs['headers']['Authorization'] == 'token github-token'
        if url.endswith('/user') and 'copilot_internal' not in url:
            return {'id': 123, 'login': 'pi-user'}
        return {'quota_snapshots': {'premium_interactions': {'entitlement': 100, 'quota_remaining': 20}}}
    monkeypatch.setattr(vendor, 'request_json', request)
    result = await quota.read_account_quota(instance)
    assert result.accounts[0].account_label == 'GitHub Copilot · pi-user'
    assert result.accounts[0].windows[0].used_percent == 80
    assert {x[1] for x in calls} == {'https://api.github.com/copilot_internal/user', 'https://api.github.com/user'}


@pytest.mark.parametrize('host', ['https://github.com@evil.test', 'evil.test', 'github.com:443'])
async def test_copilot_rejects_unverified_enterprise_origin(tmp_path, monkeypatch, host):
    instance, _ = driver(tmp_path, {'github-copilot': grant(enterpriseUrl=host)})
    async def fail(*args, **kwargs): pytest.fail('Token must not leave an allowed origin')
    monkeypatch.setattr(vendor, 'request_json', fail)
    result = await quota.read_account_quota(instance)
    assert result.issues[0].reason == 'unsupported_provider'


async def test_anthropic_keeps_pi_identity_and_model_windows(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'anthropic': grant()})
    async def request(method, url, **kwargs):
        assert method == 'GET'
        assert kwargs['headers']['Authorization'] == 'Bearer pi-access'
        if url.endswith('/profile'):
            return {'account': {'uuid': 'a', 'email': 'pi@example.test'},
                    'organization': {'uuid': 'org', 'organization_type': 'claude_max'}}
        return {'limits': [{'kind': 'session', 'percent': 0},
            {'kind': 'weekly_scoped', 'percent': 40, 'resets_at': '2026-10-18T02:00:00Z',
             'scope': {'model': {'display_name': 'Sonnet'}}}]}
    monkeypatch.setattr(vendor, 'request_json', request)
    result = await quota.read_account_quota(instance)
    assert result.accounts[0].account_label == 'Claude · p***@example.test'
    assert [(x.label, x.used_percent) for x in result.accounts[0].windows] == [('Session', 0), ('Sonnet', 40)]
    assert result.accounts[0].windows[0].window_duration_mins is None


def test_anthropic_legacy_nulls_do_not_become_zero():
    result = vendor.normalize_anthropic({'five_hour': {'utilization': 0}, 'seven_day': None,
                                         'seven_day_opus': {'utilization': 100}})
    assert len(result.windows) == 2
    assert result.windows[0].used_percent == 0
    assert result.windows[0].window_duration_mins == 300
    with pytest.raises(QuotaUnavailable): vendor.normalize_anthropic({'five_hour': {}})


@pytest.mark.parametrize('provider', ['openai-codex', 'anthropic', 'kimi-coding', 'xai', 'google-gemini-cli'])
async def test_expired_grants_are_not_rotated_without_native_writeback(tmp_path, monkeypatch, provider):
    instance, root = driver(tmp_path, {provider: grant(expires=1, projectId='pi-project')})
    before = (root / 'auth.json').read_bytes()
    async def fail(*args, **kwargs): pytest.fail('Must not rotate Pi-owned refresh grants')
    monkeypatch.setattr(vendor, 'request_json', fail)
    result = await quota.read_account_quota(instance)
    assert result.issues == (QuotaIssue('unauthenticated', quota.BRANDS[provider], 'credential_expired'),)
    assert (root / 'auth.json').read_bytes() == before


@pytest.mark.parametrize('provider,antigravity', [('google-gemini-cli', False), ('google-antigravity', True)])
async def test_legacy_google_account_keeps_its_pi_project(tmp_path, monkeypatch, provider, antigravity):
    instance, _ = driver(tmp_path, {provider: grant(projectId='pi-project')})
    async def read(credentials, **kwargs):
        assert credentials == {}
        assert kwargs == {'direct_token': 'pi-access', 'project': 'pi-project', 'antigravity': antigravity}
        return AccountQuota('Antigravity' if antigravity else 'Gemini CLI', 'Google', (QuotaWindow('a', 'a', 20),))
    monkeypatch.setattr(vendor, 'read_google_quota', read)
    assert len((await quota.read_account_quota(instance)).accounts) == 1


@pytest.mark.parametrize('provider,base', [('openrouter', 'https://openrouter.ai/api/v1'),
                                         ('kimi-coding', 'https://api.kimi.com/coding/v1')])
async def test_api_shaped_oauth_uses_its_own_grant(tmp_path, monkeypatch, provider, base):
    instance, _ = driver(tmp_path, {provider: grant()})
    async def read(sources):
        assert sources[0].base_url == base
        assert sources[0].api_key == 'pi-access'
        return QuotaReadResult((AccountQuota(provider, provider, ()),))
    monkeypatch.setattr(vendor, 'read_sources', read)
    assert len((await quota.read_account_quota(instance)).accounts) == 1


async def test_xai_uses_native_coding_credit_endpoint(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'xai': grant()})
    async def request(method, url, **kwargs):
        assert url == 'https://cli-chat-proxy.grok.com/v1/billing?format=credits'
        assert kwargs['headers']['Authorization'] == 'Bearer pi-access'
        return {'config': {'creditUsagePercent': 0, 'prepaidBalance': {'val': 500}}}
    monkeypatch.setattr(vendor, 'request_json', request)
    account = (await quota.read_account_quota(instance)).accounts[0]
    assert account.balances[0].remaining == 5


async def test_independent_accounts_survive_one_provider_rejection(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'anthropic': grant(), 'openai-codex': grant(), 'custom-private-oauth': grant()})
    async def claude(_): raise QuotaUnavailable(reason='permission_required')
    async def codex(_): return AccountQuota('OpenAI', 'ChatGPT', (QuotaWindow('a', 'a', 1),))
    monkeypatch.setattr(vendor, 'read_anthropic', claude)
    monkeypatch.setattr(vendor, 'read_codex', codex)
    result = await quota.read_account_quota(instance)
    assert len(result.accounts) == 1
    assert result.issues == (QuotaIssue('unavailable', 'Anthropic', 'permission_required'),
                             QuotaIssue('unsupported', 'Pi OAuth', 'unsupported_provider'))


async def test_custom_routing_is_not_mislabeled_as_official_quota(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'anthropic': grant()}, {'anthropic': {'baseUrl': 'https://gateway.example.test'}})
    async def fail(_): pytest.fail('Custom routing must not silently use official subscription')
    monkeypatch.setattr(vendor, 'read_anthropic', fail)
    assert (await quota.read_account_quota(instance)).issues[0].reason == 'unsupported_provider'


@pytest.mark.parametrize('error,reason', [(TypeError('private details'), 'quota_not_returned'),
                                        (RuntimeError('private details'), 'request_failed')])
async def test_schema_or_runtime_failure_is_confined_to_one_account(tmp_path, monkeypatch, error, reason):
    instance, _ = driver(tmp_path, {'anthropic': grant(), 'openai-codex': grant()})
    async def broken(_): raise error
    async def good(_): return AccountQuota('OpenAI', 'ChatGPT', (QuotaWindow('a', 'a', 1),))
    monkeypatch.setattr(vendor, 'read_anthropic', broken)
    monkeypatch.setattr(vendor, 'read_codex', good)
    result = await quota.read_account_quota(instance)
    assert len(result.accounts) == 1
    assert result.issues == (QuotaIssue('unavailable', 'Anthropic', reason),)
    assert 'private details' not in repr(result)


async def test_budget_preserves_finished_accounts_and_cancels_pending(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'anthropic': grant(), 'openai-codex': grant()})
    canceled = asyncio.Event()
    async def pending(_):
        try:
            await asyncio.Future()
        finally:
            canceled.set()
    async def good(_): return AccountQuota('OpenAI', 'ChatGPT', (QuotaWindow('a', 'a', 1),))
    monkeypatch.setattr(vendor, 'read_anthropic', pending)
    monkeypatch.setattr(vendor, 'read_codex', good)
    result = await quota.read_account_quota(instance, total_timeout=0.01)
    assert len(result.accounts) == 1
    assert result.issues == (QuotaIssue('unavailable', 'Anthropic', 'request_failed'),)
    assert canceled.is_set()


async def test_parent_cancellation_leaves_no_account_requests_running(tmp_path, monkeypatch):
    instance, _ = driver(tmp_path, {'anthropic': grant()})
    started, canceled = asyncio.Event(), asyncio.Event()
    async def pending(_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            canceled.set()
    monkeypatch.setattr(vendor, 'read_anthropic', pending)
    task = asyncio.create_task(quota.read_account_quota(instance))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert canceled.is_set()


async def test_quota_rejection_cancels_pending_identity_request(monkeypatch):
    canceled = asyncio.Event()
    async def request(method, url, **kwargs):
        if url.endswith('/usage'):
            await asyncio.sleep(0)
            raise QuotaUnavailable(reason='permission_required')
        try:
            await asyncio.Future()
        finally:
            canceled.set()
    monkeypatch.setattr(vendor, 'request_json', request)
    with pytest.raises(QuotaUnavailable): await vendor.read_anthropic(grant())
    assert canceled.is_set()
