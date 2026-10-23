"""StepFun Plan windows from a user-authorized platform session.

These are the platform's own quota/renewal requests, not model requests. The
connection service serializes reads and persists rotated grants atomically.
"""
from __future__ import annotations

import time

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow
from drivers.native_quota import account_key, finite, request_json, reset_at
from .web_login import LoginInvalid, clean_session, site_details, token_claims

QUOTA_PATH = '/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit'
PLAN_PATH = '/api/step.openapi.devcenter.Dashboard/GetStepPlanStatus'
REFRESH_PATH = '/passport/proto.api.passport.v1.PassportService/RefreshToken'


def _clean(captured):
    try:
        return clean_session(captured)
    except LoginInvalid as exc:
        raise QuotaUnavailable('unauthenticated', reason=exc.reason) from None


async def _call(site, session, path):
    origin, app_id = site_details(site)
    result = await request_json('POST', origin + path, headers={
        'Cookie': 'Oasis-Token=' + session['access'] + '...' + session['refresh'],
        'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
        'Oasis-Appid': app_id, 'Oasis-Platform': 'web', 'Oasis-Webid': session['webId'],
    }, json={})
    if not isinstance(result, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    return result


async def _renew(site, secrets):
    raw = await _call(site, secrets, REFRESH_PATH)
    access, refresh = raw.get('accessToken'), raw.get('refreshToken')
    if not isinstance(access, dict) or (refresh is not None and not isinstance(refresh, dict)):
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    renewed = _clean({
        'access': access.get('raw'),
        'refresh': (refresh or {}).get('raw') or secrets['refresh'],
        'account': secrets['account'], 'webId': secrets['webId'],
    })
    expiration = finite(token_claims(renewed['access']).get('exp'))
    if expiration is None or expiration <= time.time():
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    # Persist even if a subsequent quota request fails: the old refresh grant
    # may have been consumed. Never overwrite with an anonymous/switched login.
    secrets.update(renewed)


async def _ask(site, secrets, path):
    try:
        return await _call(site, secrets, path)
    except QuotaUnavailable as exc:
        if exc.state != 'unauthenticated':
            raise
    await _renew(site, secrets)
    return await _call(site, secrets, path)


async def validate_session(site, captured):
    """Authenticate a login result against its exact selected platform."""
    site_details(site)
    session = _clean(captured)
    await _ask(site, session, QUOTA_PATH)
    return session


def parse_windows(raw):
    if not isinstance(raw, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    windows = []

    def add(key, label, duration, left, reset, resetless=False):
        remaining, until = finite(left), reset_at(reset)
        if remaining is None or (until is None and not resetless):
            return
        if remaining > 1:
            remaining /= 100
        remaining = max(0, min(1, remaining))
        windows.append(QuotaWindow(key, label, (1 - remaining) * 100, duration, until))

    add('five-hour', '5小时', 300, raw.get('five_hour_usage_left_rate'), raw.get('five_hour_usage_reset_time'))
    add('weekly', '7天', 10080, raw.get('weekly_usage_left_rate'), raw.get('weekly_usage_reset_time'))
    credit = raw.get('plan_credit_rate_limit')
    if isinstance(credit, dict):
        buckets = credit.get('credit_buckets')
        add('credits', '订阅额度', None, credit.get('subscription_credit_left_rate'),
            credit.get('subscription_credit_reset_time'), isinstance(buckets, list) and bool(buckets))
    return tuple(windows)


async def read_connection(provider, config, secrets):
    if provider != 'stepfun-plan':
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    site = config.get('site', 'com')
    origin, _ = site_details(site)
    session = _clean(secrets)
    # Normalization derives the web id from this account's own refresh grant.
    secrets.update(session)
    expires = finite(token_claims(secrets['access']).get('exp'))
    if expires is None or expires - time.time() <= 300:
        await _renew(site, secrets)
    windows = parse_windows(await _ask(site, secrets, QUOTA_PATH))
    plan = None
    try:
        status = await _ask(site, secrets, PLAN_PATH)
        subscription = status.get('subscription')
        name = subscription.get('name') if isinstance(subscription, dict) else None
        if isinstance(name, str) and 0 < len(name.strip()) <= 100 and not any(ord(c) < 32 for c in name):
            plan = name.strip()
    except QuotaUnavailable:
        pass
    if not windows and not plan:
        raise QuotaUnavailable(reason='quota_not_returned')
    return AccountQuota('StepFun', 'StepFun · ' + ('中国站' if site == 'com' else '国际站'), windows,
                        plan=plan or 'Step Plan', account_key=account_key(origin, secrets['account']), kind='plan')
