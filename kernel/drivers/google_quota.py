"""Code Assist quota reads using an already configured native Google account.

These are account reads, never model calls or account onboarding. OAuth access
tokens are refreshed in memory only; native credential files remain untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.native_quota import finite, request_json, reset_at
from drivers.google_oauth import client_pair

PROD = 'https://cloudcode-pa.googleapis.com'
DAILY = 'https://daily-cloudcode-pa.googleapis.com'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USER_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'


def safe_text(value, default=''):
    if isinstance(value, str) and len(value) <= 200 and not any(ord(ch) < 32 for ch in value):
        return value.strip()
    return default


def official_endpoint(value, default=PROD):
    value = (value or default).rstrip('/')
    if value not in (PROD, DAILY):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    return value


async def access_token(credentials, client=None, *, resolve_client=None):
    if client and credentials.get('client_id') and credentials['client_id'] != client[0]:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    if credentials.get('token_uri') and credentials['token_uri'] != TOKEN_URL:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    token = credentials.get('access_token') or credentials.get('token')
    expiry = reset_at(credentials.get('expiry_date') or credentials.get('expiry'))
    if isinstance(token, str) and token and expiry and expiry > time.time() + 60:
        return token
    refresh = credentials.get('refresh_token')
    if not isinstance(refresh, str) or not refresh:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    client = client or client_pair(credentials.get('client_id'), credentials.get('client_secret'))
    if client is None and resolve_client is not None:
        client = await resolve_client()
    if client is None:
        raise QuotaUnavailable(reason='credential_unavailable')
    if credentials.get('client_id') and credentials['client_id'] != client[0]:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    reply = await request_json('POST', TOKEN_URL, data={
        'grant_type': 'refresh_token', 'refresh_token': refresh,
        'client_id': client[0], 'client_secret': client[1],
    })
    if not isinstance(reply, dict) or not isinstance(reply.get('access_token'), str) or not reply['access_token']:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return reply['access_token']


async def rpc(token, base, method, body):
    if base not in (PROD, DAILY) or method not in ('loadCodeAssist', 'retrieveUserQuota',
                                                  'retrieveUserQuotaSummary', 'fetchAvailableModels'):
        raise QuotaUnavailable('unsupported', reason='quota_not_returned')
    result = await request_json('POST', f'{base}/v1internal:{method}',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, json=body)
    if not isinstance(result, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    return result


async def identity(token):
    try:
        result = await request_json('GET', USER_URL, headers={'Authorization': 'Bearer ' + token})
        return result if isinstance(result, dict) else {}
    except QuotaUnavailable:
        return {}


async def gather_account_reads(*coroutines):
    """A rejected account read must not leave its identity request running."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def project_info(token, project='', *, antigravity=False):
    metadata = {'ideType': 'ANTIGRAVITY'} if antigravity else {
        'ideType': 'IDE_UNSPECIFIED', 'platform': 'PLATFORM_UNSPECIFIED', 'pluginType': 'GEMINI'}
    body = {'metadata': metadata}
    if project:
        body['cloudaicompanionProject'] = project
        metadata['duetProject'] = project
    reply = await rpc(token, PROD, 'loadCodeAssist', body)
    found = reply.get('cloudaicompanionProject')
    if isinstance(found, dict):
        found = found.get('id')
    # An explicit native billing project stays explicit; don't move usage to an
    # unrelated auto-assigned project while reading a quota card.
    found = project or safe_text(found)
    if not found:
        raise QuotaUnavailable(reason='cloud_credentials_required')
    tier = reply.get('paidTier') or reply.get('currentTier') or {}
    if not isinstance(tier, dict):
        tier = {}
    return found, safe_text(tier.get('name')) or None, safe_text(tier.get('id'))


def quota_account(provider, brand, windows, who, project, plan=None):
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    subject = who.get('sub')
    key = (hashlib.sha256(f'{provider}\0{subject}\0{project}'.encode()).hexdigest()
           if isinstance(subject, str) and subject else None)
    return AccountQuota(provider, native_account_label(brand, who.get('email')),
                        tuple(windows), plan=plan, account_key=key)


def gemini_windows(reply):
    windows = []
    seen = set()
    for index, bucket in enumerate(reply.get('buckets') or []):
        if not isinstance(bucket, dict):
            continue
        remaining = finite(bucket.get('remainingFraction'))
        if remaining is None or not 0 <= remaining <= 1:
            continue
        model = safe_text(bucket.get('modelId'), 'Gemini')
        token_type = safe_text(bucket.get('tokenType'))
        key = model + ':' + token_type
        if key in seen:
            continue
        seen.add(key)
        label = model + (f' · {token_type}' if token_type else '')
        windows.append(QuotaWindow(key, label, (1 - remaining) * 100,
                                   resets_at=reset_at(bucket.get('resetTime'))))
    return windows


def antigravity_windows(reply):
    windows = []
    for group_index, group in enumerate(reply.get('groups') or []):
        if not isinstance(group, dict):
            continue
        group_name = safe_text(group.get('displayName'), 'Antigravity')
        for index, bucket in enumerate(group.get('buckets') or []):
            if not isinstance(bucket, dict) or bucket.get('disabled'):
                continue
            # Google protobuf JSON omits a numeric zero. A returned, enabled
            # bucket with a reset time but omitted fraction means exhausted.
            remaining = finite(bucket.get('remainingFraction'))
            reset = reset_at(bucket.get('resetTime'))
            if remaining is None and 'remainingFraction' not in bucket and reset:
                remaining = 0
            if remaining is None or not 0 <= remaining <= 1:
                continue
            period = safe_text(bucket.get('window'))
            duration = {'5h': 300, 'weekly': 10080}.get(period.lower())
            label = safe_text(bucket.get('displayName')) or {'5h': '5 hours', 'weekly': '7 days'}.get(period.lower()) or period
            key = f'{group_index}:' + (safe_text(bucket.get('bucketId')) or str(index))
            windows.append(QuotaWindow(key, f'{group_name} · {label}' if label else group_name,
                                       (1 - remaining) * 100, duration, reset))
    return windows


def antigravity_model_windows(reply):
    windows = []
    models = reply.get('models')
    if not isinstance(models, dict):
        return windows
    for model, info in models.items():
        if not isinstance(info, dict) or model.startswith(('chat_', 'tab_')) or info.get('disabled'):
            continue
        quota = info.get('quotaInfo')
        if not isinstance(quota, dict):
            continue
        remaining = finite(quota.get('remainingFraction'))
        reset = reset_at(quota.get('resetTime'))
        if remaining is None and 'remainingFraction' not in quota and reset:
            remaining = 0
        if remaining is not None and 0 <= remaining <= 1:
            windows.append(QuotaWindow(model, safe_text(info.get('displayName')) or safe_text(model, 'Antigravity'),
                                       (1 - remaining) * 100, 300, reset))
    return windows


async def read_google_quota(credentials, *, antigravity=False, project='', endpoint=None, direct_token=None,
                            resolve_client=None):
    provider = brand = 'Antigravity' if antigravity else 'Gemini CLI'
    endpoint = official_endpoint(endpoint, DAILY if antigravity else PROD)
    token = direct_token or await access_token(credentials, resolve_client=resolve_client)
    (project, plan, tier), who = await gather_account_reads(
        project_info(token, project, antigravity=antigravity), identity(token))
    if antigravity:
        try:
            reply = await rpc(token, endpoint, 'retrieveUserQuotaSummary', {'project': project})
            windows = antigravity_windows(reply)
        except QuotaUnavailable as error:
            if error.state == 'unauthenticated':
                raise
            windows = []
        if not windows:
            body = {'project': project}
            if tier:
                body['entitlement'] = {'userTier': tier}
            reply = await rpc(token, endpoint, 'fetchAvailableModels', body)
            windows = antigravity_model_windows(reply)
    else:
        reply = await rpc(token, endpoint, 'retrieveUserQuota', {'project': project})
        windows = gemini_windows(reply)
    return quota_account(provider, brand, windows, who, project, plan)
