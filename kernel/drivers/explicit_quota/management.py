"""Account balances queried using explicitly connected management credentials."""
from __future__ import annotations

import asyncio

import httpx

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaUnavailable, native_account_label
from drivers.native_quota import account_key, finite
from drivers.provider_quota.parsers import normalize
from .cloud import _request_json, _secret


async def read_connection(provider: str, config: dict, secrets: dict, *, transport=None, total_timeout=15):
    """No inference key discovery, cookies, redirects, token creation or writes."""
    if provider not in ('aihubmix', 'openrouter'):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    name = 'accessToken' if provider == 'aihubmix' else 'managementKey'
    token = _secret(secrets, name, reason='management_key_required')
    url = 'https://aihubmix.com/api/user/self' if provider == 'aihubmix' else 'https://openrouter.ai/api/v1/credits'
    # AiHubMix's official platform CLI sends the Manage Key without Bearer.
    headers = {'Authorization': token if provider == 'aihubmix' else 'Bearer ' + token,
               'Accept': 'application/json'}
    try:
        async with asyncio.timeout(total_timeout):
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
                payload = await _request_json(client, 'GET', url, headers=headers, management=provider == 'openrouter')
    except TimeoutError:
        raise QuotaUnavailable(reason='request_failed') from None
    if provider == 'openrouter':
        windows, balances, plan = normalize('openrouter', payload)
        # Same explicit key as an existing provider connection shares identity.
        # A different inference key's budget remains a separate quota pool.
        return AccountQuota('OpenRouter', 'OpenRouter', windows, plan,
            'provider-key:' + account_key('openrouter.ai', token), balances, 'balance')
    data = payload.get('data')
    if payload.get('success') is not True or not isinstance(data, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    quota = finite(data.get('quota'))
    if quota is None:
        raise QuotaUnavailable(reason='quota_not_returned')
    identity = data.get('id')
    if isinstance(identity, int) and not isinstance(identity, bool) and identity > 0:
        identity = 'provider-account:' + account_key('aihubmix.com', str(identity))
    else:
        identity = 'provider-key:' + account_key('aihubmix.com', token)
    return AccountQuota('AiHubMix', native_account_label('AiHubMix', data.get('email')), (),
        account_key=identity, balances=(QuotaBalance('balance', '账户余额', quota / 500000, 'USD'),), kind='balance')
