"""Subscription allowances for the account selected by Copilot CLI."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow
from drivers.native_quota import driver_env, finite, read_json, request_json, reset_at
from drivers.quota_keychain import read_password


def github_host(value):
    """Only official GitHub.com / GitHub Enterprise Cloud can receive a token."""
    value = (value or 'github.com').strip().lower()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    host = parsed.hostname or ''
    if host != 'github.com' and not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com', host):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    return host


async def native_token(env):
    root = Path(env.get('COPILOT_HOME') or Path(env.get('HOME') or Path.home()) / '.copilot')
    config = read_json(root / 'config.json', jsonc=True)
    active = config.get('lastLoggedInUser') or {}
    if not isinstance(active, dict):
        active = {}
    explicit_host = env.get('COPILOT_GH_HOST') or env.get('GH_HOST')
    # Native env authentication wins over an old /login entry, including host.
    for key in ('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
        token = env.get(key)
        if token:
            if key == 'GITHUB_TOKEN' and env.get('CODESPACES') == 'true' and active:
                continue  # native CLI keeps its /login account in Codespaces
            return token, github_host(explicit_host)
    host = github_host(explicit_host or active.get('host'))
    if explicit_host and active and github_host(active.get('host')) != host:
        active = next((user for user in config.get('loggedInUsers', [])
                       if isinstance(user, dict) and github_host(user.get('host')) == host), {})
    login = active.get('login')
    if not isinstance(login, str) or not login:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    account = f'https://{host}:{login}'
    tokens = config.get('copilotTokens') or {}
    token = tokens.get(account) if isinstance(tokens, dict) else None
    if not token:
        token = await read_password('copilot-cli', account)
    if not isinstance(token, str) or not token:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return token, host


def normalize_quota(reply, who=None, host='github.com'):
    if not isinstance(reply, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    snapshots = reply.get('quota_snapshots')
    if not isinstance(snapshots, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    reset = reset_at(reply.get('quota_reset_date_utc'))
    if not reset and isinstance(reply.get('quota_reset_date'), str):
        reset = reset_at(reply['quota_reset_date'] + 'T00:00:00Z')
    windows = []
    for key, label in (('chat', 'Chat requests'), ('completions', 'Completions'),
                       ('premium_interactions', 'Premium requests')):
        item = snapshots.get(key)
        if not isinstance(item, dict):
            continue
        at = reset_at(item.get('quota_reset_at')) or reset
        if item.get('unlimited') is True:
            # GitHub organization premium pools use has_quota=false to signal
            # exhaustion even when their seat's entitlement says unlimited.
            if key == 'premium_interactions' and item.get('has_quota') is False:
                windows.append(QuotaWindow(key, label, 100, resets_at=at, remaining_amount=0, unit='requests'))
            else:
                windows.append(QuotaWindow(key, label, 0, resets_at=at, unlimited=True))
            continue
        total, remaining = finite(item.get('entitlement')), finite(item.get('quota_remaining'))
        if total is not None and total <= 0:
            continue
        percent = finite(item.get('percent_remaining'))
        if total is not None and remaining is not None:
            used = min(100, max(0, 100 * (total - remaining) / total))
        elif percent is not None and 0 <= percent <= 100:
            used = 100 - percent
        else:
            continue
        windows.append(QuotaWindow(key, label, used, resets_at=at,
                                   remaining_amount=max(0, remaining) if remaining is not None else None,
                                   limit_amount=total, unit='requests'))
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    plan = reply.get('copilot_plan')
    if not isinstance(plan, str) or not re.fullmatch(r'[A-Za-z0-9_ +.-]{1,80}', plan):
        plan = None
    if reply.get('access_type_sku') == 'free_educational_quota':
        plan = 'Education'
    elif reply.get('access_type_sku') == 'free_limited_copilot':
        plan = 'Free'
    who = who if isinstance(who, dict) else {}
    user_id = who.get('id')
    account_key = hashlib.sha256(f'{host}\0{user_id}'.encode()).hexdigest() if isinstance(user_id, int) and not isinstance(user_id, bool) else None
    login = who.get('login')
    label = 'GitHub Copilot'
    if isinstance(login, str) and re.fullmatch(r'[A-Za-z0-9-]{1,100}', login):
        label += ' · ' + login
    return AccountQuota('GitHub Copilot', label, tuple(windows), plan=plan, account_key=account_key)


async def _identity(base, headers):
    try:
        return await request_json('GET', base + '/user', headers=headers)
    except QuotaUnavailable:
        return {}


async def read_account_quota(driver):
    env = driver_env(driver)
    if env.get('COPILOT_PROVIDER_BASE_URL') or env.get('COPILOT_OFFLINE', '').lower() == 'true':
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    token, host = await native_token(env)
    base = 'https://api.' + host
    headers = {'Authorization': 'token ' + token, 'Accept': 'application/json',
               'Editor-Version': 'copilot/1.0.88', 'Copilot-Integration-Id': 'copilot-developer-cli',
               'X-GitHub-Api-Version': '2026-07-01', 'User-Agent': 'Kaus-account-quota'}
    reply, who = await asyncio.gather(request_json('GET', base + '/copilot_internal/user', headers=headers),
                                      _identity(base, headers))
    return normalize_quota(reply, who, host)
