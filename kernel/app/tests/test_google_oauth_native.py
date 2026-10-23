import json
from types import SimpleNamespace

import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.acp.client import AcpAgentSpec
from drivers import google_oauth as native
from drivers import google_quota as google


def driver(command, **env):
    return SimpleNamespace(agent_spec=AcpAgentSpec(command=tuple(command), env=env, inherit_env=False))


async def test_fresh_token_does_not_require_application_metadata():
    async def forbidden():
        raise AssertionError('fresh credentials must not inspect another source')
    assert await google.access_token({'access_token': 'synthetic-access', 'expiry_date': 9999999999999},
                                     resolve_client=forbidden) == 'synthetic-access'


async def test_expired_token_uses_selected_native_client_without_mutating_credentials(monkeypatch):
    credentials = {'refresh_token': 'synthetic-refresh', 'client_id': 'native-client'}
    async def resolve():
        return 'native-client', 'native-application'
    async def request(method, url, **kwargs):
        assert url == google.TOKEN_URL
        assert kwargs['data'] == {'grant_type': 'refresh_token', 'refresh_token': 'synthetic-refresh',
                                 'client_id': 'native-client', 'client_secret': 'native-application'}
        return {'access_token': 'synthetic-access'}
    monkeypatch.setattr(google, 'request_json', request)
    assert await google.access_token(credentials, resolve_client=resolve) == 'synthetic-access'
    assert credentials == {'refresh_token': 'synthetic-refresh', 'client_id': 'native-client'}


@pytest.mark.parametrize('resolved,reason', [(None, 'credential_unavailable'),
                                           (('different-client', 'application'), 'unsupported_provider')])
async def test_missing_or_mismatched_client_never_sends_refresh_token(monkeypatch, resolved, reason):
    async def resolve():
        return resolved
    async def forbidden(*args, **kwargs):
        raise AssertionError('no credential request')
    monkeypatch.setattr(google, 'request_json', forbidden)
    with pytest.raises(QuotaUnavailable) as error:
        await google.access_token({'refresh_token': 'synthetic', 'client_id': 'native-client'},
                                  resolve_client=resolve)
    assert error.value.reason == reason


async def test_connection_override_is_local_and_partial_override_does_not_mix_sources():
    selected = driver(['missing'], KAUS_GEMINI_OAUTH_CLIENT_ID='custom-client',
                      KAUS_GEMINI_OAUTH_CLIENT_SECRET='custom-application')
    assert await native.native_client(selected) == ('custom-client', 'custom-application')
    assert await native.native_client(selected, antigravity=True) is None
    partial = driver(['missing'], KAUS_GEMINI_OAUTH_CLIENT_ID='custom-client')
    assert await native.native_client(partial) is None


async def test_gemini_reads_selected_package_literals_without_executing_it(tmp_path):
    package = tmp_path / 'gemini-cli'
    (package / 'bundle').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'name': '@google/gemini-cli'}))
    entry = package / 'bundle/gemini.js'
    entry.write_text('throw new Error("must not execute");')
    module = package / 'bundle/chunk-oauth.js'
    module.write_text('const OAUTH_CLIENT_ID = "gemini-native";\nconst OAUTH_CLIENT_SECRET = "application-v1";')
    selected = driver(['node', str(entry)])
    assert await native.native_client(selected) == ('gemini-native', 'application-v1')
    module.write_text('const OAUTH_CLIENT_ID = "gemini-native";\nconst OAUTH_CLIENT_SECRET = "application-v2";')
    assert await native.native_client(selected) == ('gemini-native', 'application-v2')
    other = package / 'bundle/chunk-conflicting.js'
    other.write_text('const OAUTH_CLIENT_ID = "other-native";\nconst OAUTH_CLIENT_SECRET = "other-application";')
    assert await native.native_client(selected) is None


async def test_antigravity_default_client_is_not_business_client(tmp_path):
    executable = tmp_path / 'agy_acp_server.par'
    executable.write_bytes(b'\0native-prefix\xff\n'
        b'_BUSINESS_CLIENT_ID = "business-client"\n_BUSINESS_CLIENT_SECRET = "business-application"\n'
        b'_DEFAULT_CLIENT_ID = (\n "personal-client"\n)\n_DEFAULT_CLIENT_SECRET = "personal-application"\n')
    executable.chmod(0o700)
    selected = driver(['/usr/bin/env', 'ANTIGRAVITY_HARNESS_PATH=/unused', str(executable)])
    assert await native.native_client(selected, antigravity=True) == ('personal-client', 'personal-application')


async def test_unrecognised_native_metadata_remains_unavailable(tmp_path):
    executable = tmp_path / 'agy_acp_server.par'
    executable.write_bytes(b'no supported native metadata')
    executable.chmod(0o700)
    assert await native.native_client(driver([str(executable)]), antigravity=True) is None
