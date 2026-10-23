import base64
import json
import time

import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.explicit_quota import stepfun
from drivers.explicit_quota.web_login import LoginInvalid, capture_session, clean_session, login_url


def token(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'e30.' + body + '.signature'


def session(account='account-a', *, exp=None, activated=True, device='device-a'):
    return dict(access=token(activated=activated, oasis_id=account, exp=exp or int(time.time()) + 3600),
                refresh=token(oasis_id=account, device_id=device, exp=int(time.time()) + 86400),
                webId=device, account=account)


def usage():
    return dict(five_hour_usage_left_rate=0.6, five_hour_usage_reset_time='1790680000',
                weekly_usage_left_rate='0.9', weekly_usage_reset_time='1791200000000',
                plan_credit_rate_limit=dict(subscription_credit_left_rate=0.87, subscription_credit_reset_time=0,
                                            credit_buckets=[dict(type=1, credit_total='8000000000', credit_residual='7215728427')]))


async def test_reader_exact_platform_headers_and_no_credit_currency(monkeypatch):
    secrets, calls = session(), []

    async def request(method, url, *, headers, json):
        calls.append(url)
        assert method == 'POST' and json == {}
        assert url.startswith('https://platform.stepfun.com/')
        assert headers == {'Cookie': 'Oasis-Token=' + secrets['access'] + '...' + secrets['refresh'],
                           'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
                           'Oasis-Appid': '10300', 'Oasis-Platform': 'web', 'Oasis-Webid': 'device-a'}
        return dict(subscription=dict(name='Plus')) if url.endswith(stepfun.PLAN_PATH) else usage()

    monkeypatch.setattr(stepfun, 'request_json', request)
    result = await stepfun.read_connection('stepfun-plan', dict(site='com'), secrets)
    assert result.provider == 'StepFun' and result.kind == 'plan' and result.plan == 'Plus'
    assert [w.used_percent for w in result.windows] == pytest.approx([40, 10, 13])
    assert [w.resets_at for w in result.windows] == [1790680000, 1791200000, None]
    assert result.balances == () and len(calls) == 2
    assert secrets['account'] not in result.account_label


def test_absent_and_zero_windows_are_not_exhaustion():
    assert stepfun.parse_windows({}) == ()
    assert stepfun.parse_windows(dict(five_hour_usage_left_rate=0, five_hour_usage_reset_time=0,
                                     weekly_usage_left_rate=0, weekly_usage_reset_time=0,
                                     plan_credit_rate_limit=dict(subscription_credit_left_rate=0, subscription_credit_reset_time=0))) == ()
    data = usage()
    data['five_hour_usage_left_rate'] = 0
    assert stepfun.parse_windows(data)[0].used_percent == 100


@pytest.mark.parametrize('bad', [None, True, False, 'NaN', 'inf', {}, []])
def test_invalid_rates_are_absent(bad):
    assert stepfun.parse_windows(dict(five_hour_usage_left_rate=bad, five_hour_usage_reset_time=1790680000)) == ()


async def test_renewal_preserved_if_later_quota_fails(monkeypatch):
    secrets = session(exp=int(time.time()) + 10)
    renewed = session(device='device-new')
    calls = []

    async def request(method, url, **kw):
        calls.append(url)
        if url.endswith(stepfun.REFRESH_PATH):
            return dict(accessToken=dict(raw=renewed['access']), refreshToken=dict(raw=renewed['refresh']))
        raise QuotaUnavailable(reason='request_failed')

    monkeypatch.setattr(stepfun, 'request_json', request)
    with pytest.raises(QuotaUnavailable):
        await stepfun.read_connection('stepfun-plan', dict(site='ai'), secrets)
    assert secrets == renewed
    assert len(calls) == 2 and all(u.startswith('https://platform.stepfun.ai/') for u in calls)


@pytest.mark.parametrize('renewed', [session('another-account'), session(activated=False)])
async def test_anonymous_or_different_renewal_cannot_replace_account(monkeypatch, renewed):
    secrets = session(exp=int(time.time()) + 10)
    original = dict(secrets)

    async def request(*a, **kw):
        return dict(accessToken=dict(raw=renewed['access']), refreshToken=dict(raw=renewed['refresh']))

    monkeypatch.setattr(stepfun, 'request_json', request)
    with pytest.raises(QuotaUnavailable) as exc:
        await stepfun.read_connection('stepfun-plan', dict(site='com'), secrets)
    assert exc.value.state == 'unauthenticated' and secrets == original


async def test_unauthorized_retries_once_then_reports_login(monkeypatch):
    secrets, calls = session(), []

    async def request(method, url, **kw):
        calls.append(url)
        if url.endswith(stepfun.REFRESH_PATH):
            return dict(accessToken=dict(raw=secrets['access']))
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')

    monkeypatch.setattr(stepfun, 'request_json', request)
    with pytest.raises(QuotaUnavailable):
        await stepfun.read_connection('stepfun-plan', dict(site='ai'), secrets)
    assert [u.rsplit('/', 1)[1] for u in calls] == ['QueryStepPlanRateLimit', 'RefreshToken', 'QueryStepPlanRateLimit']


async def test_optional_plan_error_does_not_discard_windows(monkeypatch):
    async def request(method, url, **kw):
        if url.endswith(stepfun.PLAN_PATH):
            raise QuotaUnavailable(reason='request_failed')
        return usage()

    monkeypatch.setattr(stepfun, 'request_json', request)
    result = await stepfun.read_connection('stepfun-plan', dict(site='ai'), session())
    assert len(result.windows) == 3 and result.plan == 'Step Plan'


async def test_account_and_site_keys_are_distinct(monkeypatch):
    async def request(*a, **kw):
        return usage()
    monkeypatch.setattr(stepfun, 'request_json', request)
    results = [await stepfun.read_connection('stepfun-plan', dict(site=site), session(account))
               for site, account in [('ai', 'a'), ('ai', 'b'), ('com', 'a')]]
    assert len({r.account_key for r in results}) == 3


async def test_login_validation_checks_the_selected_official_site(monkeypatch):
    seen = []
    async def request(method, url, **kw):
        seen.append(url)
        return {}
    monkeypatch.setattr(stepfun, 'request_json', request)
    secrets = session()
    assert await stepfun.validate_session('ai', secrets) == secrets
    assert seen == ['https://platform.stepfun.ai' + stepfun.QUOTA_PATH]
    with pytest.raises(LoginInvalid):
        await stepfun.validate_session('attacker.test', secrets)
    assert len(seen) == 1


def test_cookie_capture_is_tied_to_new_view_site_domain_and_secure_cookie():
    data = session()
    cookie = dict(name='Oasis-Token', value=data['access'] + '...' + data['refresh'],
                  domain='.stepfun.ai', path='/', secure=True)
    assert capture_session('ai', login_url('ai'), [cookie]) == data
    assert capture_session('com', login_url('com'), [cookie]) is None
    assert capture_session('ai', 'https://platform.stepfun.ai.evil.test/', [cookie]) is None
    assert capture_session('ai', 'https://platform.stepfun.ai:444/', [cookie]) is None
    for change in [dict(domain='.evil.test'), dict(name='other'), dict(secure=False), dict(path='/other')]:
        assert capture_session('ai', login_url('ai'), [dict(cookie, **change)]) is None


def test_cookie_capture_waits_for_activated_login_and_requires_refresh_device():
    data = session(activated=False)
    cookie = dict(name='Oasis-Token', value=data['access'] + '...' + data['refresh'], domain='.stepfun.ai', secure=True)
    assert capture_session('ai', login_url('ai'), [cookie]) is None
    data = session()
    data['refresh'] = token(oasis_id=data['account'])
    cookie['value'] = data['access'] + '...' + data['refresh']
    assert capture_session('ai', login_url('ai'), [cookie]) is None


def test_refresh_device_and_account_must_match():
    data = session()
    data['webId'] = 'stale-device'
    assert clean_session(data)['webId'] == 'device-a'
    data['account'] = 'other-account'
    with pytest.raises(LoginInvalid):
        clean_session(data)


async def test_missing_all_quota_is_not_zero(monkeypatch):
    async def request(*a, **kw):
        return {}
    monkeypatch.setattr(stepfun, 'request_json', request)
    with pytest.raises(QuotaUnavailable) as exc:
        await stepfun.read_connection('stepfun-plan', dict(site='com'), session())
    assert exc.value.reason == 'quota_not_returned'
