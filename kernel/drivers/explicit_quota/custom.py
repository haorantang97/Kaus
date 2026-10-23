"""User-designated JSON balance queries; GET or POST, no scripts or redirects."""
import asyncio
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaUnavailable
from drivers.provider_quota.parsers import number
from .catalog import validate


async def read_connection(provider, config, secrets, *, transport=None):
    config = validate(provider, config, secrets)
    url = urlsplit(config['url'])
    if transport is None:
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, url.hostname, 443, type=socket.SOCK_STREAM), timeout=4)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise QuotaUnavailable(reason='invalid_endpoint')
        except (OSError, ValueError, asyncio.TimeoutError):
            raise QuotaUnavailable(reason='request_failed') from None
    headers = {'Accept': 'application/json'}
    scheme, key = config['authScheme'], secrets.get('apiKey', '').strip()
    if scheme == 'bearer': headers['Authorization'] = 'Bearer ' + key
    elif scheme == 'raw': headers['Authorization'] = key
    elif scheme == 'x-api-key': headers['X-API-Key'] = key
    if config['userId']: headers['New-Api-User'] = config['userId']
    async with httpx.AsyncClient(timeout=12, trust_env=False, follow_redirects=False, transport=transport) as client:
        async with client.stream(config['method'], config['url'], headers=headers) as response:
            if response.status_code == 401: raise QuotaUnavailable('unauthenticated', reason='credential_expired')
            if response.status_code == 403: raise QuotaUnavailable(reason='permission_required')
            if response.status_code != 200: raise QuotaUnavailable(reason='request_failed')
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1_000_000: raise QuotaUnavailable(reason='quota_not_returned')
    try:
        value = json.loads(body)
        if isinstance(value, dict) and value.get('success') is False:
            raise QuotaUnavailable(reason='request_failed')
        for part in config['remainingPath'].split('.'):
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
        amount = number(value)
        if amount is None: raise ValueError()
        amount = number(amount * float(config.get('multiplier') or '1'))
        if amount is None: raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError):
        raise QuotaUnavailable(reason='quota_not_returned') from None
    # One endpoint can expose several wallets, units or user scopes. Only the
    # same normalized query and credential prove the same displayed balance.
    normalized = {**config, 'url': str(httpx.URL(config['url']))}
    identity = hashlib.sha256((json.dumps(normalized, sort_keys=True, separators=(',', ':')) + '\0' + key).encode()).hexdigest()
    return AccountQuota('自定义余额', '自定义余额', (), account_key='explicit:' + identity,
                        balances=(QuotaBalance('balance', '可用余额', amount, config['currency']),), kind='balance')
