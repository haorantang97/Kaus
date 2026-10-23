"""Grok Build's native coding-credit endpoint using its existing login."""
from pathlib import Path

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaUnavailable, QuotaWindow
from drivers.native_quota import account_key, driver_env, finite, read_json, request_json, reset_at

BASE = 'https://cli-chat-proxy.grok.com/v1'


def amount(value):
    # Native protobuf omits a zero scalar but retains its enclosing Cent object.
    return finite(value.get('val', 0)) if isinstance(value, dict) else None


def normalize_usage(payload, *, key=None):
    config = payload.get('config') if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    period = config.get('currentPeriod') if isinstance(config.get('currentPeriod'), dict) else {}
    reset = reset_at(period.get('end') or config.get('billingPeriodEnd'))
    interval = str(period.get('type') or '').removeprefix('USAGE_PERIOD_TYPE_')
    duration = {'DAILY': 1440, 'WEEKLY': 10080, 'MONTHLY': 43200}.get(interval)
    label = {'DAILY': 'Daily', 'WEEKLY': 'Weekly', 'MONTHLY': 'Monthly'}.get(interval, 'Allowance')
    used = finite(config.get('creditUsagePercent'))
    windows = []
    if used is None:
        total, spent = amount(config.get('monthlyLimit')), amount(config.get('used'))
        if total is not None and total > 0 and spent is not None:
            used = spent / total * 100
    if used is not None and used >= 0:
        windows.append(QuotaWindow('included', label, used, duration, reset))
    cap, spent = amount(config.get('onDemandCap')), amount(config.get('onDemandUsed'))
    if cap is not None and cap > 0 and spent is not None:
        windows.append(QuotaWindow('on-demand', 'On-demand', max(0, spent / cap * 100), resets_at=reset,
            remaining_amount=max(0, cap - spent) / 100, limit_amount=cap / 100, unit='USD'))
    prepaid = amount(config.get('prepaidBalance'))
    balances = (QuotaBalance('prepaid', 'Prepaid credits', prepaid / 100, 'USD'),) if prepaid is not None else ()
    if not windows and not balances:
        raise QuotaUnavailable(reason='quota_not_returned')
    plan = payload.get('subscription_tier') or payload.get('subscriptionTier')
    return AccountQuota('xAI', 'Grok Build', tuple(windows), plan if isinstance(plan, str) else None, key, balances)


async def read_account_quota(driver):
    env = driver_env(driver)
    if env.get('GROK_CLI_CHAT_PROXY_BASE_URL', BASE).rstrip('/') != BASE:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    if env.get('XAI_API_KEY') or env.get('GROK_DEPLOYMENT_KEY'):
        raise QuotaUnavailable('unsupported', reason='unsupported_auth')
    home = Path(env.get('GROK_HOME') or Path(env.get('HOME') or Path.home()) / '.grok').expanduser()
    credentials = read_json(home / 'auth.json')
    candidates = [value for value in credentials.values() if isinstance(value, dict) and value.get('key')]
    # Ambiguous stores must not report the allowance of a different login.
    if len(candidates) != 1:
        raise QuotaUnavailable('unauthenticated' if not candidates else 'unavailable', reason='sign_in_required' if not candidates else 'credential_unavailable')
    token = candidates[0]['key']
    if not isinstance(token, str):
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    payload = await request_json('GET', BASE + '/billing?format=credits', headers={
        'Authorization': 'Bearer ' + token, 'X-XAI-Token-Auth': 'xai-grok-cli'})
    return normalize_usage(payload, key=account_key(BASE, token))
