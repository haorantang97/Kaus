import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from drivers.account_quota import QuotaReadResult, quota_reader
from drivers.hermes import quota
from drivers.hermes.quota_sources import discover


def bearer(owner='hermes-workspace', *, exp=None):
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': owner},
              'exp': exp if exp is not None else time.time() + 3600}
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.' + part + '.signature'


def state(value):
    return {'tokens': {'access_token': value, 'refresh_token': 'do-not-refresh', 'account_id': 'stale-local-account'}}


def setup(tmp_path, *, model=None, auth=True, home=None, gateway=None):
    home = home or tmp_path / '.hermes'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({'model': model if model is not None else {
        'provider': 'openai-codex', 'base_url': quota.CODEX_BASE}}))
    if auth is not False:
        (home / 'auth.json').write_text(json.dumps(auth if isinstance(auth, dict) else {
            'providers': {'openai-codex': state(bearer())}}))
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(home)}
    instance = SimpleNamespace(hermes_root=home, agent_spec=SimpleNamespace(build_env=lambda: env),
        default_gateway=SimpleNamespace(hermes_home=gateway) if gateway else None)
    return instance, home


def response():
    return {'plan_type': 'pro', 'rate_limit': {
        'primary_window': {'used_percent': 0, 'limit_window_seconds': 18000, 'reset_at': 2000000000},
        'secondary_window': {'used_percent': 50, 'limit_window_seconds': 604800, 'reset_at': 2000003600}}}


def capture(monkeypatch):
    requests = []
    async def request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return response()
    monkeypatch.setattr(quota.vendor, 'request_json', request)
    return requests


async def test_own_grant_fixed_endpoint_owner_identity_and_file_unchanged(tmp_path, monkeypatch):
    instance, home = setup(tmp_path)
    before = (home / 'auth.json').read_bytes()
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert len(result.accounts) == 1 and not result.issues
    account = result.accounts[0]
    assert len(account.windows) == 2 and account.account_key == hashlib.sha256(b'hermes-workspace').hexdigest()
    assert [w.label for w in account.windows] == ['Codex · 5 小时', 'Codex · 7 天']
    assert account.source_id and account.windows[0].used_percent == 0
    assert requests[0][:2] == ('GET', 'https://chatgpt.com/backend-api/wham/usage')
    assert requests[0][2]['headers']['ChatGPT-Account-Id'] == 'hermes-workspace'
    assert requests[0][2]['headers']['Authorization'].startswith('Bearer header.')
    assert (home / 'auth.json').read_bytes() == before
    assert 'do-not-refresh' not in repr(requests) and 'stale-local-account' not in repr(requests)
    assert not discover(instance)


async def test_missing_native_login_does_not_borrow_codex_cli_or_environment(tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, auth=False)
    other = tmp_path / '.codex'; other.mkdir()
    (other / 'auth.json').write_text(json.dumps({'tokens': state(bearer('other'))['tokens']}))
    monkeypatch.setenv('OPENAI_API_KEY', 'other-environment-key')
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and not result.accounts
    assert result.issues[0].reason == 'credential_unavailable'


@pytest.mark.parametrize('route', [None, 'https://chatgpt.com', 'https://CHATGPT.com:443/backend-api/codex/', 'https://chatgpt.com/backend-api/codex/responses'])
async def test_official_codex_routes_need_no_specific_path_suffix(route, tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, model={'provider': 'openai-codex', 'base_url': route})
    requests = capture(monkeypatch)
    assert (await quota.read_account_quota(instance)).accounts
    assert len(requests) == 1 and not discover(instance)


@pytest.mark.parametrize('route', ['https://relay.example/codex', 'http://chatgpt.com/backend-api/codex',
    'https://chatgpt.com.evil.example/backend-api/codex', 'https://user@chatgpt.com/backend-api/codex',
    'https://chatgpt.com:444/backend-api/codex', 'https://chatgpt.com/backend-api/codex?account=x'])
async def test_relay_or_invalid_route_does_not_send_native_token(route, tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, model={'provider': 'openai-codex', 'base_url': route})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'unsupported_provider'


async def test_expired_grant_does_not_refresh_or_send_request(tmp_path, monkeypatch):
    instance, home = setup(tmp_path, auth={'providers': {'openai-codex': state(bearer(exp=time.time() - 1))}})
    before = (home / 'auth.json').read_bytes()
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'credential_expired'
    assert result.issues[0].setup_provider is None
    assert (home / 'auth.json').read_bytes() == before


async def test_different_selected_provider_does_not_read_dormant_oauth(tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, model={'provider': 'deepseek', 'base_url': 'https://api.deepseek.com'})
    requests = capture(monkeypatch)
    assert await quota.read_account_quota(instance) == QuotaReadResult()
    assert not requests


@pytest.mark.parametrize('custom_root', [False, True])
async def test_native_profile_missing_provider_uses_only_same_hermes_root(tmp_path, monkeypatch, custom_root):
    root = tmp_path / ('custom-hermes' if custom_root else '.hermes')
    root.mkdir()
    (root / 'auth.json').write_text(json.dumps({'providers': {'openai-codex': state(bearer('root-owner'))}}))
    instance, _ = setup(tmp_path, home=root / 'profiles/work', auth={'providers': {}})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert result.accounts and requests[0][2]['headers']['ChatGPT-Account-Id'] == 'root-owner'


@pytest.mark.parametrize('profile_state,expected', [(None, 'profile-owner'), ({}, None)])
async def test_existing_profile_provider_shadows_root_even_when_empty(tmp_path, monkeypatch, profile_state, expected):
    root = tmp_path / '.hermes'; root.mkdir()
    (root / 'auth.json').write_text(json.dumps({'providers': {'openai-codex': state(bearer('root-owner'))}}))
    profile_state = state(bearer('profile-owner')) if profile_state is None else profile_state
    instance, _ = setup(tmp_path, home=root / 'profiles/work', auth={'providers': {'openai-codex': profile_state}})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    if expected:
        assert requests[0][2]['headers']['ChatGPT-Account-Id'] == expected
    else:
        assert not requests and not result.accounts and result.issues[0].reason == 'credential_unavailable'


async def test_unrelated_custom_home_does_not_inherit_user_native_root(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'; root.mkdir()
    (root / 'auth.json').write_text(json.dumps({'providers': {'openai-codex': state(bearer('unrelated'))}}))
    instance, _ = setup(tmp_path, home=tmp_path / 'custom-home', auth=False)
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'credential_unavailable'


async def test_gateway_profile_and_existing_native_auth_symlink_are_honored(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    instance, _ = setup(tmp_path, home=root, auth={'providers': {'openai-codex': state(bearer('root-owner'))}})
    profile = root / '.kaus-scopes/work'
    _, _ = setup(tmp_path, home=profile, auth=False)
    (profile / 'auth.json').symlink_to(root / 'auth.json')
    instance.default_gateway = SimpleNamespace(hermes_home=profile)
    requests = capture(monkeypatch)
    assert (await quota.read_account_quota(instance)).accounts
    assert requests[0][2]['headers']['ChatGPT-Account-Id'] == 'root-owner'


@pytest.mark.parametrize('suppressed', [['device_code'], {'device_code': True}])
async def test_explicitly_removed_native_source_stays_removed(tmp_path, monkeypatch, suppressed):
    instance, _ = setup(tmp_path, auth={'providers': {'openai-codex': state(bearer())},
        'suppressed_sources': {'openai-codex': suppressed}})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'sign_in_required'


async def test_native_profile_codex_route_override_blocks_first_party_request(tmp_path, monkeypatch):
    instance, home = setup(tmp_path)
    (home / '.env').write_text('HERMES_CODEX_BASE_URL=https://relay.example/codex\n')
    original = instance.agent_spec.build_env()
    instance.agent_spec.build_env = lambda: {**original, 'HERMES_CODEX_BASE_URL': quota.CODEX_BASE}
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'unsupported_provider'
    sources = discover(instance)
    assert len(sources) == 1 and sources[0].base_url == 'https://relay.example/codex'


async def test_http_effective_profile_model_inherits_root_by_key(tmp_path, monkeypatch):
    instance, root = setup(tmp_path)
    profile = root / 'profiles/work'
    setup(tmp_path, home=profile, model={'reasoning_effort': 'high'}, auth=False)
    instance.default_gateway = SimpleNamespace(hermes_home=profile)
    requests = capture(monkeypatch)
    assert (await quota.read_account_quota(instance)).accounts
    assert len(requests) == 1 and not discover(instance)
    (profile / 'config.yaml').write_text(json.dumps({'model': {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com'}}))
    assert await quota.read_account_quota(instance) == QuotaReadResult()
    assert len(requests) == 1


async def test_native_acp_profile_does_not_invent_root_model_inheritance(tmp_path, monkeypatch):
    _, root = setup(tmp_path)
    instance, _ = setup(tmp_path, home=root / 'profiles/work', model={'reasoning_effort': 'high'}, auth=False)
    requests = capture(monkeypatch)
    assert await quota.read_account_quota(instance) == QuotaReadResult()
    assert not requests


async def test_http_profile_model_does_not_borrow_root_provider_or_route(tmp_path, monkeypatch):
    instance, root = setup(tmp_path, model={'default': 'gpt', 'provider': 'openai-codex', 'base_url': quota.CODEX_BASE})
    profile = root / 'profiles/work'
    setup(tmp_path, home=profile, model={'default': 'different-model'}, auth=False)
    instance.default_gateway = SimpleNamespace(hermes_home=profile)
    requests = capture(monkeypatch)
    assert await quota.read_account_quota(instance) == QuotaReadResult()
    assert not requests


async def test_current_native_pool_fallback_uses_first_non_cooling_entry(tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, auth={'providers': {}, 'credential_pool': {'openai-codex': [
        {'access_token': bearer('cooling'), 'last_error_reset_at': time.time() + 60},
        {'access_token': bearer('available')}, {'access_token': bearer('later')}]}})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert result.accounts and requests[0][2]['headers']['ChatGPT-Account-Id'] == 'available'


async def test_other_provider_pool_is_never_used(tmp_path, monkeypatch):
    instance, _ = setup(tmp_path, auth={'providers': {}, 'credential_pool': {'another-provider': [
        {'access_token': bearer('wrong')}]}})
    requests = capture(monkeypatch)
    result = await quota.read_account_quota(instance)
    assert not requests and result.issues[0].reason == 'credential_unavailable'


def test_unused_empty_provider_is_not_a_connected_account(tmp_path):
    instance, home = setup(tmp_path)
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': 'openai-codex', 'base_url': quota.CODEX_BASE},
        'providers': {'placeholder': {'base_url': 'https://empty.example/v1', 'api_key': ''},
                      'configured': {'base_url': 'https://configured.example/v1', 'api_key': 'available-key'}}}))
    sources = discover(instance)
    assert len(sources) == 1 and sources[0].base_url == 'https://configured.example/v1'


@pytest.mark.parametrize('selection', [{'provider': 'selected'}, {'provider': 'custom', 'base_url': 'https://selected.example/v1'}])
def test_selected_custom_source_remains_visible_when_key_missing(tmp_path, selection):
    instance, home = setup(tmp_path, model=selection)
    (home / 'config.yaml').write_text(json.dumps({'model': selection, 'providers': {
        'selected': {'base_url': 'https://selected.example/v1', 'api_key': ''},
        'unused': {'base_url': 'https://unused.example/v1', 'api_key': ''}}}))
    sources = discover(instance)
    assert sources and all(s.base_url == 'https://selected.example/v1' for s in sources)


async def test_timeout_and_outer_cancel_terminate_request(tmp_path, monkeypatch):
    instance, _ = setup(tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()
    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
    monkeypatch.setattr(quota.vendor, 'request_json', pending)
    result = await quota.read_account_quota(instance, total_timeout=.01)
    assert stopped.is_set() and result.issues[0].reason == 'request_failed'
    started.clear(); stopped.clear()
    task = asyncio.create_task(quota.read_account_quota(instance))
    await started.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_http_and_acp_capability_wiring_uses_same_reader(tmp_path, monkeypatch):
    from drivers.acp.presets import get_preset
    from drivers.hermes.driver import HermesDriver
    instance, home = setup(tmp_path)
    calls = capture(monkeypatch)
    http = HermesDriver(hermes_root=home)
    http.agent_spec = instance.agent_spec
    assert (await quota_reader(http)()).accounts
    acp = SimpleNamespace(preset=get_preset('hermes-acp'), agent_spec=instance.agent_spec)
    assert (await quota_reader(acp)()).accounts
    assert len(calls) == 2
