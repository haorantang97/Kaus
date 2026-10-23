"""Read the existing DSH platform account grant and its wallet summary.

Official protocol checked at DeepSeek-Harness 1517a4dbca0a2789ff1d41ba439600f8547d23fa,
packages/credentials/deepseek-account-platform/src/{details,protocol,index}.ts.
No account sign-in, refresh, acknowledgement, credential write or model request.
"""
from pathlib import Path
from urllib.parse import urlsplit

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaReadResult, QuotaUnavailable
from drivers.native_quota import account_key, driver_env, finite, request_json
from drivers.provider_quota.discovery import document, mapping

ORIGIN = 'https://platform.deepseek.com'
RECORD = 'deepseek-account-platform/default'


def normalize_wallets(payload, *, key):
    if not isinstance(payload, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    data = mapping(payload.get('data'))
    if payload.get('code') == 40003 or data.get('biz_code') == 40003:
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    if payload.get('code') != 0 or data.get('biz_code') != 0:
        raise QuotaUnavailable(reason='quota_not_returned')
    summary = mapping(data.get('biz_data'))
    balances = []
    for field, label in [('normal_wallets', '充值余额'), ('bonus_wallets', '赠金余额')]:
        values = summary.get(field)
        if not isinstance(values, list):
            raise QuotaUnavailable(reason='quota_not_returned')
        for row in values:
            if not isinstance(row, dict) or row.get('currency') not in ('CNY', 'USD'):
                raise QuotaUnavailable(reason='quota_not_returned')
            value = finite(row.get('balance'))
            if value is None:
                raise QuotaUnavailable(reason='quota_not_returned')
            currency = row['currency']
            balances.append(QuotaBalance(field + ':' + currency, label, value, currency))
    if not balances:
        raise QuotaUnavailable(reason='quota_not_returned')
    return AccountQuota('DeepSeek', 'DeepSeek 账户', (), account_key=key, balances=tuple(balances), kind='balance')


async def read_account_quota(driver):
    env = driver_env(driver)
    home = Path(env.get('HOME') or Path.home())
    root = Path(env.get('DSH_HOME') or home / '.dsh').expanduser()
    store = document(root / '.credentials.yaml')
    record = mapping(store.get('records')).get(RECORD)
    if record is None:
        # DSH API-key users have no platform grant; their API balance is queried
        # by the provider reader without an extra "sign in" entry.
        return QuotaReadResult()
    if store.get('version') != 1 or not isinstance(record, dict) or record.get('kind') != 'grant':
        raise QuotaUnavailable(reason='credential_unavailable')
    grant = mapping(record.get('payload'))
    token, issuer = grant.get('token'), grant.get('issuer')
    if grant.get('version') != 1 or not isinstance(token, str) or not token or len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise QuotaUnavailable(reason='credential_unavailable')
    try:
        origin = urlsplit(issuer)
        valid = origin.scheme == 'https' and origin.hostname == 'platform.deepseek.com' and origin.port in (None, 443) and origin.path in ('', '/') and not any((origin.username, origin.password, origin.query, origin.fragment))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    payload = await request_json('GET', ORIGIN + '/api/v0/users/get_user_summary', headers={'x-dsh-auth-token': token, 'Accept': 'application/json'})
    return normalize_wallets(payload, key=account_key(ORIGIN, token))
