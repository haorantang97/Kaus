import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from drivers.account_quota import QuotaUnavailable, quota_reader
from drivers.acp.client import AcpAgentSpec
from drivers.acp.presets import get_preset
from drivers.antigravity import quota as antigravity
from drivers.copilot import quota as copilot
from drivers.gemini import quota as gemini
from drivers import google_quota as google
from drivers import quota_keychain


def driver(tmp_path, **env):
    return SimpleNamespace(agent_spec=AcpAgentSpec(command=('unused',),
        env={'HOME': str(tmp_path), **env}, inherit_env=False))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize('agent', ['gemini', 'antigravity', 'copilot'])
def test_native_quota_capabilities_registered(agent):
    assert quota_reader(SimpleNamespace(preset=get_preset(agent))) is not None


def test_gemini_model_buckets_are_separate_and_missing_is_unknown():
    windows = google.gemini_windows({'buckets': [
        {'modelId': 'gemini-pro', 'remainingFraction': .8, 'resetTime': '2026-10-18T00:00:00Z'},
        {'modelId': 'gemini-flash', 'remainingFraction': 0},
        {'modelId': 'not-reported'}, {'modelId': 'bad', 'remainingFraction': 'NaN'},
        {'modelId': 'invalid', 'remainingFraction': 2},
    ]})
    assert len(windows) == 2
    assert windows[0].used_percent == pytest.approx(20)
    assert windows[0].resets_at == 1792281600
    assert windows[0].window_duration_mins is None
    assert windows[1].used_percent == 100


def test_antigravity_keeps_shared_groups_and_weekly_windows():
    windows = google.antigravity_windows({'groups': [{
        'displayName': 'Gemini Models', 'buckets': [
            {'bucketId': 'five', 'window': '5h', 'remainingFraction': .6},
            {'bucketId': 'weekly', 'window': 'weekly', 'remainingFraction': .9},
            {'bucketId': 'disabled', 'remainingFraction': 1, 'disabled': True},
            {'bucketId': 'unknown'},
        ]}, {'displayName': 'Claude and GPT models', 'buckets': [
            {'bucketId': 'empty', 'window': '5h', 'resetTime': '2026-10-18T00:00:00Z'},
        ]}]})
    assert [w.window_duration_mins for w in windows] == [300, 10080, 300]
    assert windows[2].used_percent == 100
    assert windows[1].label == 'Gemini Models · 7 days'
    assert windows[2].label.startswith('Claude and GPT models')


def test_antigravity_model_fallback_ignores_internal_missing_and_disabled():
    windows = google.antigravity_model_windows({'models': {
        'gemini-pro': {'displayName': 'Gemini Pro', 'quotaInfo': {'remainingFraction': .4}},
        'claude': {'quotaInfo': {'resetTime': '2026-10-18T00:00:00Z'}},
        'tab_something': {'quotaInfo': {'remainingFraction': .9}},
        'unknown': {'quotaInfo': {}}, 'disabled': {'disabled': True, 'quotaInfo': {'remainingFraction': 1}},
    }})
    assert len(windows) == 2
    assert windows[0].label == 'Gemini Pro'
    assert windows[1].used_percent == 100


async def test_google_only_official_reads_refresh_in_memory_and_no_onboarding(monkeypatch):
    calls = []
    credentials = {'refresh_token': 'synthetic-refresh', 'client_id': 'synthetic-client',
                   'client_secret': 'synthetic-application'}
    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == google.TOKEN_URL:
            return {'access_token': 'synthetic-access'}
        if url == google.USER_URL:
            return {'sub': 'native-account-123', 'email': 'test@example.invalid'}
        if url.endswith(':loadCodeAssist'):
            return {'cloudaicompanionProject': {'id': 'native-project'}, 'currentTier': {'name': 'Pro'}}
        if url.endswith(':retrieveUserQuota'):
            return {'buckets': [{'modelId': 'Gemini Pro', 'remainingFraction': .8}]}
        raise AssertionError('unexpected endpoint')
    monkeypatch.setattr(google, 'request_json', request)
    result = await google.read_google_quota(credentials)
    assert result.account_label == 'Gemini CLI · t***@example.invalid'
    assert result.account_key and result.plan == 'Pro'
    assert credentials == {'refresh_token': 'synthetic-refresh', 'client_id': 'synthetic-client',
                           'client_secret': 'synthetic-application'}
    assert all('onboardUser' not in url for _, url, _ in calls)
    assert all(url.startswith('https://') for _, url, _ in calls)
    assert 'synthetic' not in repr(result)


async def test_google_no_project_never_onboards(monkeypatch):
    async def request(method, url, **kwargs):
        assert url.endswith(':loadCodeAssist') or url == google.USER_URL
        return {}
    monkeypatch.setattr(google, 'request_json', request)
    with pytest.raises(QuotaUnavailable) as error:
        await google.read_google_quota({'access_token': 'synthetic', 'expiry_date': 9999999999999})
    assert error.value.reason == 'cloud_credentials_required'


async def test_antigravity_summary_then_model_fallback(monkeypatch):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(url)
        if url == google.USER_URL:
            return {}
        if url.endswith(':loadCodeAssist'):
            return {'cloudaicompanionProject': 'p', 'paidTier': {'id': 'paid', 'name': 'Ultra'}}
        if url.endswith(':retrieveUserQuotaSummary'):
            raise QuotaUnavailable()
        if url.endswith(':fetchAvailableModels'):
            assert kwargs['json']['entitlement'] == {'userTier': 'paid'}
            return {'models': {'gemini': {'quotaInfo': {'remainingFraction': .5}}}}
        raise AssertionError(url)
    monkeypatch.setattr(google, 'request_json', request)
    result = await google.read_google_quota({'access_token': 'synthetic', 'expiry_date': 9999999999999}, antigravity=True)
    assert result.provider == 'Antigravity'
    assert result.windows[0].used_percent == 50
    assert result.account_key is None


@pytest.mark.parametrize('value', ['https://attacker.example', 'http://cloudcode-pa.googleapis.com',
                                  'https://cloudcode-pa.googleapis.com.evil', 'https://cloudcode-pa.googleapis.com/path'])
async def test_google_refuses_credential_forwarding(monkeypatch, value):
    async def forbidden(*args, **kwargs):
        raise AssertionError('must not send token')
    monkeypatch.setattr(google, 'request_json', forbidden)
    with pytest.raises(QuotaUnavailable):
        await google.read_google_quota({'refresh_token': 'synthetic'}, endpoint=value)
    with pytest.raises(QuotaUnavailable):
        await google.access_token({'refresh_token': 'synthetic', 'token_uri': value}, ('synthetic-client', 'synthetic-application'))


async def test_gemini_custom_home_and_selected_auth_no_native_mutation(tmp_path, monkeypatch):
    custom = tmp_path / 'different'
    write(custom / '.gemini/settings.json', {'security': {'auth': {'selectedType': 'oauth-personal'}}})
    write(custom / '.gemini/oauth_creds.json', {'refresh_token': 'synthetic'})
    async def read(credentials, **kwargs):
        assert credentials == {'refresh_token': 'synthetic'}
        assert kwargs['project'] == 'p'
        return 'ok'
    monkeypatch.setattr(gemini, 'read_google_quota', read)
    assert await gemini.read_account_quota(driver(tmp_path, GEMINI_CLI_HOME=str(custom), GOOGLE_CLOUD_PROJECT='p', GEMINI_API_KEY='unused')) == 'ok'
    assert json.loads((custom / '.gemini/oauth_creds.json').read_text()) == {'refresh_token': 'synthetic'}


async def test_gemini_native_dotenv_project_and_environment_precedence(tmp_path, monkeypatch):
    root = tmp_path / '.gemini'
    write(root / 'oauth_creds.json', {'refresh_token': 'synthetic'})
    (root / '.env').write_text('GOOGLE_CLOUD_PROJECT="native-project" # comment\nIGNORED=$(whoami)\n')
    async def read(credentials, **kwargs):
        return kwargs['project']
    monkeypatch.setattr(gemini, 'read_google_quota', read)
    assert await gemini.read_account_quota(driver(tmp_path)) == 'native-project'
    assert await gemini.read_account_quota(driver(tmp_path, GOOGLE_CLOUD_PROJECT='explicit')) == 'explicit'


async def test_gemini_native_access_token_mode_does_not_read_different_oauth(tmp_path, monkeypatch):
    async def read(credentials, **kwargs):
        assert credentials == {} and kwargs['direct_token'] == 'synthetic'
        return 'ok'
    monkeypatch.setattr(gemini, 'read_google_quota', read)
    assert await gemini.read_account_quota(driver(tmp_path, GOOGLE_GENAI_USE_GCA='true', GOOGLE_CLOUD_ACCESS_TOKEN='synthetic')) == 'ok'


@pytest.mark.parametrize('auth', ['gemini-api-key', 'vertex-ai', 'gateway'])
async def test_gemini_selected_other_auth_is_not_google_subscription(tmp_path, auth):
    write(tmp_path / '.gemini/settings.json', {'security': {'auth': {'selectedType': auth}}})
    with pytest.raises(QuotaUnavailable) as error:
        await gemini.read_account_quota(driver(tmp_path))
    assert error.value.state == 'unsupported'


async def test_antigravity_file_store_uses_acp_identity_and_preserves_files(tmp_path, monkeypatch):
    root = tmp_path / 'native'
    write(root / 'antigravity-acp/acp_token.json', {'refresh_token': 'acp-synthetic', 'project_id': 'p'})
    async def read(credentials, **kwargs):
        assert credentials['refresh_token'] == 'acp-synthetic'
        assert kwargs['antigravity'] and kwargs['project'] == 'p'
        return 'ok'
    async def forbidden(*args):
        raise AssertionError('forced file does not inspect keychain')
    monkeypatch.setattr(antigravity, 'read_google_quota', read)
    monkeypatch.setattr(antigravity, 'read_password', forbidden)
    assert await antigravity.read_account_quota(driver(tmp_path, GEMINI_HOME=str(root), AGY_ACP_FORCE_FILE_STORAGE='1')) == 'ok'


async def test_antigravity_blocked_keychain_never_falls_back_to_agy_or_stale_file(tmp_path, monkeypatch):
    write(tmp_path / '.gemini/antigravity-acp/acp_token.json', {'refresh_token': 'stale'})
    monkeypatch.setattr(antigravity.sys, 'platform', 'darwin')
    async def blocked(service, account):
        assert (service, account) == ('gemini', 'antigravity-acp')
        raise QuotaUnavailable(reason='credential_access_required')
    monkeypatch.setattr(antigravity, 'read_password', blocked)
    with pytest.raises(QuotaUnavailable) as error:
        await antigravity.read_account_quota(driver(tmp_path))
    assert error.value.reason == 'credential_access_required'


def test_copilot_counts_strings_unlimited_exhausted_and_unknown():
    result = copilot.normalize_quota({'copilot_plan': 'business', 'quota_reset_date': '2026-11-01', 'quota_snapshots': {
        'chat': {'unlimited': True, 'has_quota': False},
        'completions': {'entitlement': '2000', 'quota_remaining': '1000'},
        'premium_interactions': {'unlimited': True, 'has_quota': False},
    }}, {'id': 3, 'login': 'native-user'})
    assert result.account_label == 'GitHub Copilot · native-user'
    assert result.account_key and result.plan == 'business'
    assert result.windows[0].unlimited
    assert result.windows[1].used_percent == 50
    assert result.windows[1].limit_amount == 2000
    assert result.windows[1].remaining_amount == 1000
    assert result.windows[1].resets_at == 1793491200
    assert not result.windows[2].unlimited and result.windows[2].used_percent == 100
    with pytest.raises(QuotaUnavailable):
        copilot.normalize_quota({'quota_snapshots': {'chat': {}, 'completions': {'entitlement': 0}}})


async def test_copilot_environment_token_precedence_and_official_host(tmp_path, monkeypatch):
    write(tmp_path / '.copilot/config.json', {'lastLoggedInUser': {'host': 'https://old.ghe.com', 'login': 'old'}})
    token, host = await copilot.native_token({'HOME': str(tmp_path), 'COPILOT_GITHUB_TOKEN': 'first', 'GH_TOKEN': 'second'})
    assert token == 'first' and host == 'github.com'
    token, host = await copilot.native_token({'HOME': str(tmp_path), 'GH_TOKEN': 'second', 'COPILOT_GH_HOST': 'acme.ghe.com'})
    assert token == 'second' and host == 'acme.ghe.com'


async def test_copilot_only_active_native_account_is_read(tmp_path, monkeypatch):
    write(tmp_path / '.copilot/config.json', {'lastLoggedInUser': {'host': 'https://acme.ghe.com', 'login': 'active'},
        'loggedInUsers': [{'host': 'https://github.com', 'login': 'old'}],
        'copilotTokens': {'https://github.com:old': 'wrong'}})
    async def password(service, account):
        assert service == 'copilot-cli' and account == 'https://acme.ghe.com:active'
        return 'synthetic'
    monkeypatch.setattr(copilot, 'read_password', password)
    assert await copilot.native_token({'HOME': str(tmp_path)}) == ('synthetic', 'acme.ghe.com')


@pytest.mark.parametrize('host', ['evil.invalid', 'https://github.com@evil.invalid', 'http://github.com',
                                 'https://github.com/evil', 'https://api.acme.ghe.com', 'acme.ghe.com:444'])
def test_copilot_refuses_nonofficial_credential_destinations(host):
    with pytest.raises(QuotaUnavailable):
        copilot.github_host(host)


async def test_copilot_reader_native_quota_and_verified_identity_only(tmp_path, monkeypatch):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(url)
        assert kwargs['headers']['Authorization'] == 'token synthetic'
        if url.endswith('/user') and not url.endswith('/copilot_internal/user'):
            return {'id': 45, 'login': 'current-user'}
        return {'quota_snapshots': {'premium_interactions': {'percent_remaining': 75}}}
    monkeypatch.setattr(copilot, 'request_json', request)
    result = await copilot.read_account_quota(driver(tmp_path, GH_TOKEN='synthetic'))
    assert result.windows[0].used_percent == 25
    assert set(calls) == {'https://api.github.com/user', 'https://api.github.com/copilot_internal/user'}
    with pytest.raises(QuotaUnavailable) as error:
        await copilot.read_account_quota(driver(tmp_path, COPILOT_PROVIDER_BASE_URL='https://custom.invalid', GH_TOKEN='synthetic'))
    assert error.value.state == 'unsupported' and len(calls) == 2


async def test_keychain_worker_never_prompts_or_exposes_errors(monkeypatch):
    monkeypatch.setattr(quota_keychain.sys, 'platform', 'darwin')
    class Process:
        returncode = 4
        async def communicate(self, data):
            assert json.loads(data) == ['service', 'account']
            return b'', b''
    async def process(*args, **kwargs):
        assert kwargs['stderr'] == asyncio.subprocess.DEVNULL
        return Process()
    monkeypatch.setattr(quota_keychain.asyncio, 'create_subprocess_exec', process)
    with pytest.raises(QuotaUnavailable) as error:
        await quota_keychain.read_password('service', 'account')
    assert error.value.reason == 'credential_access_required'


async def test_keychain_native_worker_is_read_only_and_noninteractive():
    if sys.platform != 'darwin':
        pytest.skip('macOS system API check')
    # A nonexistent synthetic service verifies the real no-UI path, without
    # opening existing credentials or creating/modifying a keychain item.
    assert await quota_keychain.read_password('Kaus-quota-nonexistent-test-20261017', 'synthetic') is None
