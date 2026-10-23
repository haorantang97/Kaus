"""Kilo Gateway credit balance using the CLI's current native account."""
import json
from pathlib import Path

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaUnavailable
from drivers.native_quota import LIMIT, account_key, driver_env, finite, read_json, request_json

BASE = 'https://api.kilo.ai'


def normalize_balance(payload, *, organization=None, key=None):
    balance = finite(payload.get('balance')) if isinstance(payload, dict) else None
    if balance is None:
        raise QuotaUnavailable(reason='quota_not_returned')
    label = 'Kilo · Team' if organization else 'Kilo'
    return AccountQuota('Kilo', label, (), account_key=key,
        balances=(QuotaBalance('credits', 'Credits', balance, 'USD'),), kind='balance')


async def read_account_quota(driver):
    env = driver_env(driver)
    base = env.get('KILO_API_URL', BASE).rstrip('/')
    if base not in (BASE, 'https://app.kilo.ai'):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    if 'KILO_AUTH_CONTENT' in env:
        try:
            if len(env['KILO_AUTH_CONTENT']) > LIMIT:
                raise ValueError()
            store = json.loads(env['KILO_AUTH_CONTENT'])
        except (ValueError, TypeError):
            raise QuotaUnavailable(reason='credential_unavailable') from None
    else:
        home = Path(env.get('HOME') or Path.home())
        data = Path(env.get('XDG_DATA_HOME') or home / '.local/share')
        store = read_json(data / 'kilo/auth.json')
    credential = store.get('kilo') if isinstance(store, dict) else None
    if not isinstance(credential, dict) or credential.get('type') != 'oauth' or not isinstance(credential.get('access'), str):
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    token = credential['access'].strip()
    if not token:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    organization = credential.get('accountId')
    headers = {'Authorization': 'Bearer ' + token}
    if isinstance(organization, str) and organization:
        headers['x-kilocode-organizationid'] = organization
    else:
        organization = None
    payload = await request_json('GET', base + '/api/profile/balance', headers=headers)
    return normalize_balance(payload, organization=organization, key=account_key(base, token + '\0' + (organization or 'personal')))
