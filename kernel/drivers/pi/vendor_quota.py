"""Read-only subscription endpoints for Pi's own OAuth credentials.

No credential is loaded here. Rotating OAuth grants stay owned by Pi: a quota
read never refreshes one and leaves the native file holding an invalid grant.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.copilot.quota import github_host, normalize_quota as copilot_quota
from drivers.google_quota import gather_account_reads, read_google_quota, safe_text
from drivers.grok.quota import BASE as GROK_BASE, normalize_usage as grok_quota
from drivers.native_quota import account_key, finite, request_json, reset_at
from drivers.provider_quota import ProviderSource, read_sources


def token(value):
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise QuotaUnavailable('unauthenticated', reason='credential_unavailable')
    return value


def access(credential):
    expiry = reset_at(credential.get('expires'))
    if expiry is not None and expiry <= time.time():
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    return token(credential.get('access'))


def claims(value):
    """Routing hints only; a successful first-party response verifies access."""
    try:
        part = value.split('.')[1]
        result = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def normalize_codex(payload, *, owner=None, email=None):
    if not isinstance(payload, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    pools = [('codex', 'Codex', payload.get('rate_limit'))]
    for index, item in enumerate(payload.get('additional_rate_limits') or []):
        if isinstance(item, dict):
            pools.append((safe_text(item.get('limit_id')) or f'pool-{index}',
                          safe_text(item.get('limit_name'), 'Codex'), item.get('rate_limit')))
    windows = []
    for pool_id, label, pool in pools:
        if not isinstance(pool, dict):
            continue
        for slot in ('primary_window', 'secondary_window'):
            item = pool.get(slot)
            if not isinstance(item, dict):
                continue
            used = finite(item.get('used_percent'))
            if used is None or used < 0:
                continue
            seconds = finite(item.get('limit_window_seconds'))
            duration = int(seconds / 60) if seconds and seconds >= 60 else None
            reset = reset_at(item.get('reset_at'))
            after = finite(item.get('reset_after_seconds'))
            if reset is None and after is not None and after >= 0:
                reset = int(time.time() + after)
            windows.append(QuotaWindow(f'{pool_id}:{slot.removesuffix("_window")}', label,
                                       used, duration, reset))
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    key = hashlib.sha256(owner.encode()).hexdigest() if owner else None
    return AccountQuota('OpenAI', native_account_label('ChatGPT', email), tuple(windows),
                        safe_text(payload.get('plan_type')) or None, key)


async def read_codex(credential):
    bearer = access(credential)
    who = claims(bearer)
    auth = who.get('https://api.openai.com/auth')
    auth = auth if isinstance(auth, dict) else {}
    # Pi's native request headers derive the workspace from the access token,
    # not an older accountId field left beside a rotated token in auth.json.
    owner = safe_text(auth.get('chatgpt_account_id'))
    if not owner:
        raise QuotaUnavailable('unauthenticated', reason='credential_unavailable')
    headers = {'Authorization': 'Bearer ' + bearer, 'Accept': 'application/json'}
    headers['ChatGPT-Account-Id'] = owner
    payload = await request_json('GET', 'https://chatgpt.com/backend-api/wham/usage', headers=headers)
    profile = who.get('https://api.openai.com/profile')
    email = profile.get('email') if isinstance(profile, dict) else who.get('email')
    return normalize_codex(payload, owner=owner, email=email)


def normalize_anthropic(payload, profile=None):
    if not isinstance(payload, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    windows = []
    # Current native replies describe the quota scope explicitly. A session is
    # not assigned a five-hour duration unless the response says five_hour.
    for index, item in enumerate(payload.get('limits') or []):
        if not isinstance(item, dict):
            continue
        used = finite(item.get('percent'))
        kind = item.get('kind')
        if used is None or used < 0 or kind not in ('session', 'weekly_all', 'weekly_scoped'):
            continue
        scope = item.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = safe_text(model.get('display_name')) if isinstance(model, dict) else ''
        label = 'Session' if kind == 'session' else 'All models' if kind == 'weekly_all' else name or 'Model quota'
        windows.append(QuotaWindow(f'{kind}:{name or index}', label, used,
                                   10080 if kind.startswith('weekly') else None, reset_at(item.get('resets_at'))))
    if not windows:
        for key, item in payload.items():
            if key != 'five_hour' and not key.startswith('seven_day'):
                continue
            if not isinstance(item, dict):
                continue
            used = finite(item.get('utilization'))
            if used is None or used < 0:
                continue
            name = {'five_hour': 'Session', 'seven_day': 'All models',
                    'seven_day_sonnet': 'Sonnet', 'seven_day_opus': 'Opus',
                    'seven_day_fable': 'Fable', 'seven_day_oauth_apps': 'OAuth apps',
                    'seven_day_cowork': 'Cowork'}.get(key, 'Other models')
            windows.append(QuotaWindow(key, name, used, 300 if key == 'five_hour' else 10080,
                                       reset_at(item.get('resets_at'))))
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    profile = profile if isinstance(profile, dict) else {}
    who = profile.get('account') or {}
    org = profile.get('organization') or {}
    who, org = (who if isinstance(who, dict) else {}), (org if isinstance(org, dict) else {})
    subject, organization = safe_text(who.get('uuid')), safe_text(org.get('uuid'))
    key = account_key('anthropic', subject + '\0' + organization) if subject and organization else None
    return AccountQuota('Anthropic', native_account_label('Claude', who.get('email')),
                        tuple(windows), safe_text(org.get('organization_type')) or None, key)


async def _optional_get(url, headers):
    try:
        return await request_json('GET', url, headers=headers)
    except QuotaUnavailable:
        return {}


async def read_anthropic(credential):
    bearer = access(credential)
    headers = {'Authorization': 'Bearer ' + bearer, 'anthropic-beta': 'oauth-2025-04-20',
               'Accept': 'application/json'}
    payload, profile = await gather_account_reads(
        request_json('GET', 'https://api.anthropic.com/api/oauth/usage', headers=headers),
        _optional_get('https://api.anthropic.com/api/oauth/profile', headers))
    return normalize_anthropic(payload, profile)


async def read_copilot(credential):
    # Pi calls this long-lived GitHub OAuth token "refresh"; its access field
    # is a short-lived Copilot inference token and cannot read account usage.
    bearer = token(credential.get('refresh'))
    host = github_host(credential.get('enterpriseUrl'))
    base = 'https://api.' + host
    headers = {'Authorization': 'token ' + bearer, 'Accept': 'application/json',
               'Editor-Version': 'vscode/1.107.0', 'Copilot-Integration-Id': 'vscode-chat',
               'X-GitHub-Api-Version': '2026-06-01', 'User-Agent': 'Kaus-account-quota'}
    payload, who = await gather_account_reads(
        request_json('GET', base + '/copilot_internal/user', headers=headers),
        _optional_get(base + '/user', headers))
    return copilot_quota(payload, who, host)


async def read_google(credential, *, antigravity=False):
    bearer = access(credential)
    project = safe_text(credential.get('projectId'))
    if not project:
        raise QuotaUnavailable(reason='cloud_credentials_required')
    return await read_google_quota({}, direct_token=bearer, project=project, antigravity=antigravity)


async def read_api_oauth(credential, base):
    return await read_sources((ProviderSource(base, access(credential)),))


async def read_xai(credential):
    bearer = access(credential)
    payload = await request_json('GET', GROK_BASE + '/billing?format=credits', headers={
        'Authorization': 'Bearer ' + bearer, 'X-XAI-Token-Auth': 'xai-grok-cli'})
    return grok_quota(payload, key=account_key(GROK_BASE, bearer))
