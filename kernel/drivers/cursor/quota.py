"""Cursor's native dashboard usage RPCs; no browser cookies or login flows."""
from pathlib import Path
import sys

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow
from drivers.native_quota import account_key, driver_env, finite, read_json, request_json, reset_at

BASE = 'https://api2.cursor.sh'


def normalize_usage(period, hard=None, *, key=None):
    if not isinstance(period, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    windows = []
    reset = reset_at(period.get('billingCycleEnd'))
    plan = period.get('planUsage') if isinstance(period.get('planUsage'), dict) else {}
    for field, label in (('autoPercentUsed', 'Cursor Models'), ('apiPercentUsed', 'Other Models')):
        used = finite(plan.get(field))
        if used is not None and used >= 0:
            windows.append(QuotaWindow(field, label, used, resets_at=reset))
    spend = period.get('spendLimitUsage') if isinstance(period.get('spendLimitUsage'), dict) else {}
    used = finite(spend.get('individualUsed'))
    own = finite(spend.get('individualLimit'))
    hard = hard if isinstance(hard, dict) else {}
    hard_limit = finite(hard.get('hardLimit'))
    limit = None
    unlimited = False
    label = 'On-demand'
    if spend.get('limitType') == 'team':
        if own is not None:
            limit = own
        elif hard.get('noUsageBasedAllowed') or (hard_limit is not None and hard_limit <= 0):
            limit = 0
        elif (finite(spend.get('pooledLimit')) or 0) > 0:
            limit = finite(spend['pooledLimit']); used = finite(spend.get('pooledUsed'))
            label = 'Team on-demand'
        elif hard_limit is not None:
            unlimited = True
    elif hard.get('noUsageBasedAllowed'):
        limit = 0
    elif hard_limit is not None:
        unlimited = hard_limit >= 2147483647
        limit = None if unlimited else hard_limit * 100
    elif own is not None:
        limit = own
    if unlimited:
        windows.append(QuotaWindow('on-demand', label, 0, resets_at=reset, unlimited=True, unit='USD'))
    elif limit is not None and limit > 0 and used is not None:
        windows.append(QuotaWindow('on-demand', label, max(0, used / limit * 100), resets_at=reset,
            remaining_amount=max(0, limit - used) / 100, limit_amount=limit / 100, unit='USD'))
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    return AccountQuota('Cursor', 'Cursor', tuple(windows), account_key=key)


async def read_account_quota(driver):
    env = driver_env(driver)
    token = env.get('CURSOR_AUTH_TOKEN') or env.get('CURSOR_API_KEY')
    home = Path(env.get('HOME') or Path.home())
    if not token:
        if env.get('CURSOR_CONFIG_DIR'):
            path = Path(env['CURSOR_CONFIG_DIR']).expanduser() / 'auth.json'
        elif sys.platform == 'win32':
            path = Path(env.get('APPDATA') or home / 'AppData/Roaming') / 'Cursor/auth.json'
        elif sys.platform == 'darwin':
            path = home / '.cursor/auth.json'
        else:
            path = Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'cursor/auth.json'
        if sys.platform == 'darwin' and not env.get('CURSOR_CONFIG_DIR'):
            from drivers.quota_keychain import read_password
            token = await read_password('cursor-access-token', 'cursor-user')
        if not token:
            token = read_json(path).get('accessToken')
    if not isinstance(token, str) or not token.strip():
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    headers = {'Authorization': 'Bearer ' + token, 'Connect-Protocol-Version': '1'}
    endpoint = BASE + '/aiserver.v1.DashboardService/'
    period = await request_json('POST', endpoint + 'GetCurrentPeriodUsage', headers=headers, json={})
    try:
        hard = await request_json('POST', endpoint + 'GetHardLimit', headers=headers, json={})
    except QuotaUnavailable:
        hard = None
    return normalize_usage(period, hard, key=account_key(BASE, token))
