"""Native quota paths with synthetic credentials and isolated HTTP/process fakes."""
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from drivers.account_quota import QuotaUnavailable
from drivers import native_quota
from drivers.cursor import quota as cursor
from drivers.grok import quota as grok
from drivers.kilo import quota as kilo
from drivers.zcode import quota as zcode
from drivers.oh_my_pi import quota as omp


def driver(env):
    return SimpleNamespace(agent_spec=SimpleNamespace(build_env=lambda: env))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def encrypted(value, seed='synthetic-seed'):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = b'0123456789ab'
    data = AESGCM(hashlib.sha256(seed.encode()).digest()).encrypt(iv, value.encode(), None)
    enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    return 'enc:v1:' + '.'.join(map(enc, (iv, data[-16:], data[:-16])))


@pytest.mark.parametrize('value', [True, None, 'NaN', float('inf'), 'text'])
def test_missing_or_invalid_numbers_are_not_zero(value):
    assert native_quota.finite(value) is None


def test_config_bounded_jsonc_and_reset(tmp_path):
    path = tmp_path / 'native.json'
    path.write_text('{"url":"https://example.test/a,//x",/*comment*/"n":1,}')
    assert native_quota.read_json(path, jsonc=True)['url'] == 'https://example.test/a,//x'
    path.write_text('[]')
    with pytest.raises(QuotaUnavailable) as exc: native_quota.read_json(path)
    assert exc.value.reason == 'credential_unavailable'
    assert native_quota.reset_at('2026-10-17T00:00:00Z') == native_quota.reset_at(1792195200000)
    assert native_quota.reset_at('2026-10-17T00:00:00') is None


@pytest.mark.parametrize('status,body,reason', [(302, b'{}', 'request_failed'), (401, b'{}', 'sign_in_required'), (403, b'{}', 'permission_required'), (200, b'[]', None), (200, b'null', 'quota_not_returned'), (200, b'broken', 'request_failed')])
async def test_transport_is_bounded_no_redirect_or_proxy(monkeypatch, status, body, reason):
    original = httpx.AsyncClient
    seen = []
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False
        assert kwargs['trust_env'] is False
        async def response(request):
            seen.append(request)
            return httpx.Response(status, content=body, headers={'Location': 'https://other.test'})
        return original(**kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr(native_quota.httpx, 'AsyncClient', factory)
    if reason:
        with pytest.raises(QuotaUnavailable) as exc: await native_quota.request_json('GET', 'https://official.test/usage')
        assert exc.value.reason == reason
    else:
        assert await native_quota.request_json('GET', 'https://official.test/usage') == []
    assert len(seen) == 1


async def test_transport_response_size_limit(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(native_quota, 'LIMIT', 12)
    monkeypatch.setattr(native_quota.httpx, 'AsyncClient', lambda **kw: original(**kw, transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'{"value":"123456789"}'))))
    with pytest.raises(QuotaUnavailable) as exc: await native_quota.request_json('GET', 'https://official.test/usage')
    assert exc.value.reason == 'quota_not_returned'


def test_cursor_split_pools_on_demand_and_unlimited():
    data = {'planUsage': {'autoPercentUsed': 25, 'apiPercentUsed': 80}, 'spendLimitUsage': {'individualUsed': 400}, 'billingCycleEnd': 1792195200000}
    result = cursor.normalize_usage(data, {'hardLimit': 10})
    assert [w.used_percent for w in result.windows] == [25, 80, 40]
    assert result.windows[-1].remaining_amount == 6
    assert cursor.normalize_usage(data, {'hardLimit': 2147483647}).windows[-1].unlimited
    assert len(cursor.normalize_usage(data, {'noUsageBasedAllowed': True}).windows) == 2
    data['spendLimitUsage'] = {'limitType': 'team', 'pooledLimit': 1000, 'pooledUsed': 200}
    assert cursor.normalize_usage(data).windows[-1].used_percent == 20


async def test_cursor_current_keychain_before_stale_file(monkeypatch, tmp_path):
    write(tmp_path / '.cursor/auth.json', {'accessToken': 'stale-synthetic'})
    monkeypatch.setattr(cursor.sys, 'platform', 'darwin')
    from drivers import quota_keychain
    async def keychain(service, account): return 'current-synthetic'
    monkeypatch.setattr(quota_keychain, 'read_password', keychain)
    seen = []
    async def query(method, url, **kw):
        seen.append(kw['headers']['Authorization'])
        if url.endswith('GetHardLimit'): raise QuotaUnavailable(reason='request_failed')
        return {'planUsage': {'autoPercentUsed': 10}}
    monkeypatch.setattr(cursor, 'request_json', query)
    result = await cursor.read_account_quota(driver({'HOME': str(tmp_path)}))
    assert seen == ['Bearer current-synthetic'] * 2
    assert result.windows[0].used_percent == 10


def test_grok_protobuf_zero_prepaid_and_legacy():
    result = grok.normalize_usage({'config': {'creditUsagePercent': 40, 'currentPeriod': {'type': 'USAGE_PERIOD_TYPE_WEEKLY', 'end': '2026-10-17T00:00:00Z'}, 'onDemandCap': {'val': 1000}, 'onDemandUsed': {}, 'prepaidBalance': {}}})
    assert result.windows[0].window_duration_mins == 10080
    assert result.windows[1].remaining_amount == 10
    assert result.balances[0].remaining == 0
    assert grok.normalize_usage({'config': {'monthlyLimit': {'val': 500}, 'used': {'val': 100}}}).windows[0].used_percent == 20
    with pytest.raises(QuotaUnavailable): grok.normalize_usage({'config': {}})


async def test_grok_native_login_and_custom_route(monkeypatch, tmp_path):
    write(tmp_path / '.grok/auth.json', {'user-1': {'key': 'synthetic-login'}})
    requests = []
    async def query(method, url, **kw):
        requests.append((method, url, kw['headers']))
        return {'config': {'creditUsagePercent': 10}}
    monkeypatch.setattr(grok, 'request_json', query)
    result = await grok.read_account_quota(driver({'HOME': str(tmp_path)}))
    assert requests[0] == ('GET', grok.BASE + '/billing?format=credits', {'Authorization': 'Bearer synthetic-login', 'X-XAI-Token-Auth': 'xai-grok-cli'})
    assert 'synthetic-login' not in result.account_key
    with pytest.raises(QuotaUnavailable) as exc: await grok.read_account_quota(driver({'HOME': str(tmp_path), 'GROK_CLI_CHAT_PROXY_BASE_URL': 'https://relay.test'}))
    assert exc.value.reason == 'unsupported_provider'
    assert len(requests) == 1


async def test_kilo_override_and_organization_balance(monkeypatch, tmp_path):
    write(tmp_path / '.local/share/kilo/auth.json', {'kilo': {'type': 'oauth', 'access': 'stale'}})
    requests = []
    async def query(method, url, **kw):
        requests.append((url, kw['headers']))
        return {'balance': 12.34}
    monkeypatch.setattr(kilo, 'request_json', query)
    env = {'HOME': str(tmp_path), 'KILO_AUTH_CONTENT': json.dumps({'kilo': {'type': 'oauth', 'access': 'selected-synthetic', 'accountId': 'org-1'}})}
    result = await kilo.read_account_quota(driver(env))
    assert result.balances[0].remaining == 12.34
    assert requests == [(kilo.BASE + '/api/profile/balance', {'Authorization': 'Bearer selected-synthetic', 'x-kilocode-organizationid': 'org-1'})]
    with pytest.raises(QuotaUnavailable): kilo.normalize_balance({})
    env['KILO_API_URL'] = 'https://relay.test'
    with pytest.raises(QuotaUnavailable): await kilo.read_account_quota(driver(env))
    assert len(requests) == 1


def test_zcode_native_encryption():
    value = encrypted('synthetic-native-token')
    assert zcode.decrypt(value, {'ZCODE_CREDENTIAL_SECRET': 'synthetic-seed'}) == 'synthetic-native-token'
    with pytest.raises(QuotaUnavailable) as exc: zcode.decrypt(value, {'ZCODE_CREDENTIAL_SECRET': 'wrong'})
    assert exc.value.reason == 'credential_unavailable'


async def test_zcode_personal_full_request_pipeline_and_custom_base(monkeypatch, tmp_path):
    home = tmp_path / '.zcode/v2'
    config = {'provider': {'builtin:zai-coding-plan': {'options': {'apiKey': encrypted('synthetic-key'), 'baseURL': 'https://api.z.ai/api/anthropic'}}}}
    write(home / 'config.json', config)
    requests = []
    async def query(method, url, **kw):
        requests.append((method, url, kw['headers']))
        return {'code': 200, 'success': True, 'data': {'level': 'pro', 'limits': [{'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 20}]}}
    monkeypatch.setattr(zcode, 'request_json', query)
    env = {'HOME': str(tmp_path), 'ZCODE_CREDENTIAL_SECRET': 'synthetic-seed'}
    result = await zcode.read_account_quota(driver(env))
    assert result.accounts[0].windows[0].used_percent == 20
    assert requests[0][0:2] == ('GET', 'https://api.z.ai/api/monitor/usage/quota/limit')
    assert requests[0][2]['Authorization'] == 'synthetic-key'
    config['provider']['builtin:zai-coding-plan']['options']['baseURL'] = 'https://relay.test/api/anthropic'
    write(home / 'config.json', config)
    result = await zcode.read_account_quota(driver(env))
    assert not result.accounts and result.issues[0].reason == 'unsupported_provider'
    assert result.issues[0].provider == 'Z.ai'
    assert len(requests) == 1


async def test_zcode_team_uses_existing_key_get_only(monkeypatch, tmp_path):
    home = tmp_path / '.zcode/v2'
    write(home / 'credentials.json', {'oauth:bigmodel:access_token': encrypted('synthetic-oauth')})
    write(home / 'setting.json', {'providerFamilyConnectionSelections': {'bigmodel': {'kind': 'team-coding-plan', 'organizationId': 'org', 'projectId': 'project'}}})
    requests = []
    async def query(method, url, **kw):
        requests.append((method, url, kw['headers']))
        if url.endswith('/api_keys'): return {'code': 200, 'data': [{'apiKey': 'existing', 'name': 'zcode-team-api-key', 'keyType': 2}]}
        if '/copy/' in url: return {'code': 200, 'data': {'secretKey': 'secret'}}
        return {'code': 200, 'data': {'limits': [{'type': 'TOKENS_LIMIT', 'unit': 3, 'usage': 100, 'remaining': 60, 'currentValue': 40}]}}
    monkeypatch.setattr(zcode, 'request_json', query)
    result = await zcode.read_account_quota(driver({'HOME': str(tmp_path), 'ZCODE_CREDENTIAL_SECRET': 'synthetic-seed'}))
    assert result.accounts[0].windows[0].used_percent == 40
    assert [req[0] for req in requests] == ['GET', 'GET', 'GET']
    assert requests[-1][2]['Authorization'] == 'existing.secret'
    assert requests[-1][2]['Bigmodel-Organization'] == 'org'


async def test_zcode_team_does_not_create_missing_key(monkeypatch, tmp_path):
    home = tmp_path / '.zcode/v2'
    write(home / 'credentials.json', {'oauth:zai:access_token': 'synthetic'})
    write(home / 'setting.json', {'modelProviderFamilySelectedKeys': {'zai': 'team-plan:builtin:zai-coding-plan:account:org:project'}})
    requests = []
    async def query(method, url, **kw):
        requests.append((method, url)); return {'code': 200, 'data': []}
    monkeypatch.setattr(zcode, 'request_json', query)
    result = await zcode.read_account_quota(driver({'HOME': str(tmp_path)}))
    assert not result.accounts and result.issues[0].reason == 'credential_unavailable'
    assert len(requests) == 1 and requests[0][0] == 'GET'


async def test_zcode_start_plan_keeps_active_balance(monkeypatch, tmp_path):
    home = tmp_path / '.zcode/v2'
    write(home / 'credentials.json', {'zcodejwttoken': 'synthetic-jwt'})
    async def query(method, url, **kw):
        assert kw['headers']['Authorization'] == 'Bearer synthetic-jwt'
        return {'code': 200, 'data': {'plans': [{'user_plan_id': 'active', 'status': 'active'}, {'user_plan_id': 'old', 'status': 'expired'}], 'balances': [{'user_plan_id': 'active', 'total_units': 100, 'remaining_units': 65}, {'user_plan_id': 'old', 'total_units': 200, 'remaining_units': 180}]}}
    monkeypatch.setattr(zcode, 'request_json', query)
    result = await zcode.read_account_quota(driver({'HOME': str(tmp_path)}))
    assert len(result.accounts[0].windows) == 1
    assert result.accounts[0].windows[0].used_percent == 35


def omp_payload():
    return {'reports': [{'provider': 'synthetic-provider', 'metadata': {'accountId': 'id-1', 'email': 'demo@example.test'}, 'limits': [{'id': 'hour', 'amount': {'usedFraction': 0.25}, 'window': {'durationMs': 3600000}}, {'id': 'cash', 'amount': {'unit': 'usd', 'remaining': 2}}]}], 'accountsWithoutUsage': [{'provider': 'unavailable-provider'}]}


def test_omp_usage_accounts_scope_and_missing():
    result = omp.normalize_usage(omp_payload())
    assert result.accounts[0].windows[0].used_percent == 25
    assert result.accounts[0].windows[0].window_duration_mins == 60
    assert result.accounts[0].balances[0].remaining == 2
    assert 'demo@' not in result.accounts[0].account_label
    assert result.issues[0].state == 'unavailable'


async def test_omp_command_has_no_model_or_extensions_and_temp_cwd(monkeypatch, tmp_path):
    binary = tmp_path / 'omp'
    trace = tmp_path / 'trace'
    binary.write_text('#!/usr/bin/env python3\n# accountsWithoutUsage handleUsageCommand noExtensions\nimport json,os,sys\nif \"--help\" in sys.argv:\n print(\"usage [ACTION] --json --no-extensions\"); sys.exit()\nopen(os.environ["TRACE"],"w").write(json.dumps([sys.argv[1:],os.getcwd()]))\nprint(' + repr(json.dumps(omp_payload())) + ')\n')
    binary.chmod(0o755)
    monkeypatch.setattr(omp, 'native_path', lambda _: str(binary))
    env = dict(os.environ, TRACE=str(trace), HOME=str(tmp_path))
    result = await omp.read_account_quota(driver(env))
    args, cwd = json.loads(trace.read_text())
    assert args == ['usage', '--json', '--no-extensions']
    assert cwd != str(tmp_path) and not Path(cwd).exists()
    assert result.accounts[0].windows[0].used_percent == 25


async def test_old_omp_never_launches_unknown_usage(monkeypatch, tmp_path):
    binary = tmp_path / 'old-omp'
    binary.write_text('#!/bin/sh\nexit 99\n'); binary.chmod(0o755)
    monkeypatch.setattr(omp, 'native_path', lambda _: str(binary))
    async def fail(*args, **kwargs): raise AssertionError('must not launch')
    monkeypatch.setattr(omp.asyncio, 'create_subprocess_exec', fail)
    with pytest.raises(QuotaUnavailable) as exc: await omp.read_account_quota(driver({}))
    assert exc.value.reason == 'native_version_unsupported'


@pytest.mark.parametrize('module,payload', [(cursor, {'planUsage': 'bad'}), (grok, {'config': {'currentPeriod': 1}}), (kilo, {'balance': True})])
def test_malformed_native_payloads_are_not_fake_allowance(module, payload):
    with pytest.raises(QuotaUnavailable):
        (module.normalize_balance if module is kilo else module.normalize_usage)(payload)


async def test_old_omp_with_usage_cannot_load_extensions(monkeypatch, tmp_path):
    binary = tmp_path / 'old-omp'
    binary.write_text('#!/usr/bin/env python3\n# accountsWithoutUsage handleUsageCommand noExtensions\nimport sys\nif sys.argv[1:] == ["--version"]: print("omp/0.0.1")\nelse:\n assert sys.argv[1:] == ["usage", "--help"]\n print("usage [ACTION] --json")\n')
    binary.chmod(0o755)
    monkeypatch.setattr(omp, 'native_path', lambda _: str(binary))
    with pytest.raises(QuotaUnavailable) as exc: await omp.read_account_quota(driver(dict(os.environ)))
    assert exc.value.reason == 'native_version_unsupported'


async def test_verified_omp_legacy_usage_needs_no_extension_flag(monkeypatch, tmp_path):
    binary = tmp_path / 'omp-18'
    trace = tmp_path / 'trace'
    binary.write_text('#!/usr/bin/env python3\n# accountsWithoutUsage runUsageCommand\nimport json,os,sys\nif "--help" in sys.argv: print("usage [ACTION] --json"); sys.exit()\nif "--version" in sys.argv: print("omp/18.3.2"); sys.exit()\nopen(os.environ["TRACE"],"w").write(json.dumps(sys.argv[1:]))\nprint(' + repr(json.dumps(omp_payload())) + ')\n')
    binary.chmod(0o755)
    monkeypatch.setattr(omp, 'native_path', lambda _: str(binary))
    result = await omp.read_account_quota(driver(dict(os.environ, TRACE=str(trace))))
    assert result.accounts[0].windows[0].used_percent == 25
    assert json.loads(trace.read_text()) == ['usage', '--json']
