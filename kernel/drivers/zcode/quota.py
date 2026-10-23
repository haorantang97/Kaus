"""Read ZCode's existing account credentials and official quota endpoints.

Credential decryption follows ZCode's documented storage format. No credential
is created, refreshed, persisted, or sent outside its original official site.
"""
import base64
import getpass
import hashlib
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote, unquote, urlsplit

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable, QuotaWindow
from drivers.native_quota import account_key, driver_env, finite, read_json, request_json, reset_at

ROOTS = {'zai': 'https://api.z.ai', 'bigmodel': 'https://open.bigmodel.cn'}
LABELS = {'zai': 'Z.ai', 'bigmodel': 'BigModel'}

def mapping(value):
    return value if isinstance(value, dict) else {}

def native_username():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return getpass.getuser().split('\\')[-1]

def verify_provider_root(options, root):
    value = options.get('baseURL') or options.get('baseUrl')
    if value:
        parsed, expected = urlsplit(str(value)), urlsplit(root)
        if parsed.scheme != 'https' or parsed.hostname != expected.hostname or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise QuotaUnavailable('unsupported', reason='unsupported_provider')


def decrypt(value, env):
    if not isinstance(value, str):
        return ''
    if not value.startswith('enc:'):
        return value.strip()
    if not value.startswith('enc:v1:'):
        raise QuotaUnavailable(reason='credential_unavailable')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise QuotaUnavailable(reason='dependency_missing') from None
    seed = env.get('ZCODE_CREDENTIAL_SECRET', '').strip()
    if not seed:
        # Node's os.homedir() honors HOME; userInfo().username uses the OS account.
        platform = 'win32' if sys.platform == 'win32' else sys.platform
        seed = f"zcode-credential-fallback:{platform}:{env.get('HOME') or Path.home()}:{native_username()}"
    try:
        parts = value[len('enc:v1:'):].split('.')
        if len(parts) != 3:
            raise ValueError()
        iv, tag, data = [base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)) for part in parts]
        if len(iv) != 12 or len(tag) != 16:
            raise ValueError()
        return AESGCM(hashlib.sha256(seed.encode()).digest()).decrypt(iv, data + tag, None).decode().strip()
    except Exception:
        raise QuotaUnavailable(reason='credential_unavailable') from None


def unwrap(value):
    if not isinstance(value, dict) or value.get('success') is False or str(value.get('code', 0)) not in ('0', '200', 'None'):
        raise QuotaUnavailable(reason='quota_not_returned')
    return value.get('data', value)


async def query(url, token, extra=None):
    return unwrap(await request_json('GET', url, headers={'Authorization': token, 'Accept': 'application/json', **(extra or {})}))


def normalize_start(payload, *, key=None, now=None):
    if not isinstance(payload, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    now = time.time() if now is None else now
    plans = {(p.get('user_plan_id') or p.get('plan_id')): p for p in payload.get('plans', [])
        if isinstance(p, dict) and p.get('status') == 'active' and (reset_at(p.get('ends_at')) or float('inf')) > now}
    windows = []
    for row in payload.get('balances', []):
        if not isinstance(row, dict) or (row.get('user_plan_id') or row.get('plan_id')) not in plans:
            continue
        total, used, left = finite(row.get('total_units')), finite(row.get('used_units')), finite(row.get('remaining_units'))
        if total is None or total <= 0 or (used is None and left is None):
            continue
        used = total - left if used is None else used
        left = max(0, total - used) if left is None else left
        end = reset_at(row.get('period_end')) or reset_at(row.get('expires_at'))
        start = reset_at(row.get('period_start'))
        if end and end <= now:
            continue
        windows.append(QuotaWindow(str(row.get('entitlement_id') or len(windows)), row.get('show_name') or 'Credits',
            max(0, used / total * 100), int((end - start) / 60) if end and start and end > start else None,
            end, max(0, left), total, 'credits'))
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    names = {p.get('name') for p in plans.values() if isinstance(p.get('name'), str)}
    return AccountQuota('Z.ai', 'ZCode', tuple(windows), ' / '.join(sorted(names)) or 'Start Plan', key, kind='plan')


async def team_quota(family, selection, store, env):
    org, project = selection.get('organizationId'), selection.get('projectId')
    token = decrypt(store.get('oauth:' + family + ':access_token'), env)
    if not all(isinstance(value, str) and value for value in (org, project, token)):
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    root = 'https://bigmodel.cn' if family == 'bigmodel' else ROOTS[family]
    headers = {'Bigmodel-Organization': org, 'Bigmodel-Project': project,
               'Set-Language': 'zh' if family == 'bigmodel' else 'en', 'Accept-Language': 'en-US,en'}
    path = root + '/api/biz/v1/organization/' + quote(org, safe='') + '/projects/' + quote(project, safe='') + '/api_keys'
    keys = await query(path, token, headers)
    existing = [row.get('apiKey') for row in keys if isinstance(row, dict) and row.get('name') == 'zcode-team-api-key' and str(row.get('keyType')) == '2'] if isinstance(keys, list) else []
    ident = next((value for value in existing if isinstance(value, str) and value), None)
    if not ident:
        # Native ZCode may create this key. A quota refresh must never do so.
        raise QuotaUnavailable(reason='credential_unavailable')
    secret = await query(path + '/copy/' + quote(ident, safe=''), token, headers)
    credential = ident + '.' + secret['secretKey'] if isinstance(secret, dict) and secret.get('secretKey') else ident
    payload = await query(root + '/api/monitor/usage/quota/limit?type=2', credential, headers)
    from drivers.provider_quota.parsers import normalize
    windows, balances, plan = normalize('zhipu', {'data': payload})
    return AccountQuota('Z.ai' if family == 'zai' else 'BigModel', 'ZCode · Team', windows, plan,
        account_key(root, token + '\0' + org + '\0' + project), balances, 'plan')


async def read_account_quota(driver):
    env = driver_env(driver)
    home = Path(env.get('ZCODE_DATA_BASE_DIR') or env.get('HOME') or Path.home()).expanduser() / '.zcode/v2'
    store = read_json(home / 'credentials.json')
    config = read_json(home / 'config.json', jsonc=True)
    settings = read_json(home / 'setting.json', jsonc=True)
    selected = mapping(settings.get('providerFamilyConnectionSelections'))
    legacy = mapping(settings.get('modelProviderFamilySelectedKeys'))
    accounts, issues = [], []
    for family, root in ROOTS.items():
        selection = mapping(selected.get(family))
        if family not in selected:
            old = legacy.get(family, '')
            prefix = 'team-plan:builtin:' + family + '-coding-plan:'
            if isinstance(old, str) and old.startswith(prefix):
                parts = old[len(prefix):].split(':')
                if len(parts) == 3:
                    selection = {'kind': 'team-coding-plan', 'organizationId': unquote(parts[1]), 'projectId': unquote(parts[2])}
        try:
            if selection.get('kind') == 'team-coding-plan':
                accounts.append(await team_quota(family, selection, store, env)); continue
            credential = ''
            providers = mapping(config.get('provider'))
            for ident in ('builtin:' + family + '-coding-plan', 'account:' + family + '-individual-coding-plan'):
                options = mapping(mapping(providers.get(ident)).get('options'))
                verify_provider_root(options, root)
                value = options.get('apiKey')
                if value:
                    credential = decrypt(value, env); break
            if not credential:
                candidates = [decrypt(value, env) for name, value in store.items()
                    if ':coding-plan:' in name and ':' + family + '-' in name and name.endswith(':api-key')]
                candidates = list(dict.fromkeys(value for value in candidates if value))
                if len(candidates) > 1:
                    raise QuotaUnavailable(reason='credential_unavailable')
                credential = candidates[0] if candidates else ''
            if not credential:
                continue
            payload = await query(root + '/api/monitor/usage/quota/limit', credential)
            from drivers.provider_quota.parsers import normalize
            windows, balances, plan = normalize('zhipu', {'data': payload})
            accounts.append(AccountQuota('Z.ai' if family == 'zai' else 'BigModel', 'ZCode', windows, plan,
                account_key(root, credential), balances, 'plan'))
        except QuotaUnavailable as exc:
            issues.append(QuotaIssue(exc.state, LABELS[family], exc.reason))
    if not accounts and store.get('zcodejwttoken'):
        token = decrypt(store['zcodejwttoken'], env).removeprefix('Bearer ').strip()
        payload = await query('https://zcode.z.ai/api/v1/zcode-plan/billing/balance', 'Bearer ' + token)
        accounts.append(normalize_start(payload, key=account_key('https://zcode.z.ai', token)))
    if not accounts and not issues:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return QuotaReadResult(tuple(accounts), tuple(issues))
