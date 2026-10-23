"""Read the ACP server's account, never the separately signed-in agy CLI."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from drivers.account_quota import QuotaUnavailable
from drivers.google_quota import DAILY, official_endpoint, read_google_quota
from drivers.google_oauth import native_client
from drivers.native_quota import driver_env, read_json
from drivers.quota_keychain import read_password


async def read_account_quota(driver):
    env = driver_env(driver)
    root = Path(env.get('GEMINI_HOME') or Path(env.get('HOME') or Path.home()) / '.gemini') / 'antigravity-acp'
    settings = read_json(root / 'settings.json', jsonc=True)
    selected = (settings.get('auth') or {}).get('type')
    if selected and selected != 'oauth-personal':
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    if not selected and (env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY')):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    endpoint = official_endpoint(env.get('AGY_ACP_CCPA_BASE_URL'), DAILY)
    if env.get('AGY_ACP_CCPA_PROJECT'):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    use_file = sys.platform != 'darwin' or env.get('AGY_ACP_FORCE_FILE_STORAGE', '').lower() in ('1', 'true', 'yes')
    if use_file:
        credentials = read_json(root / 'acp_token.json')
    else:
        # No fallback to a potentially stale file when the native active store
        # is the Keychain, and no fallback to agy's distinct account.
        secret = await read_password('gemini', 'antigravity-acp')
        try:
            credentials = json.loads(secret) if secret else {}
        except (ValueError, TypeError):
            raise QuotaUnavailable(reason='credential_unavailable') from None
    if not isinstance(credentials, dict) or not credentials:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return await read_google_quota(credentials, antigravity=True,
        project=credentials.get('project_id') or '', endpoint=endpoint,
        resolve_client=lambda: native_client(driver, antigravity=True, env=env))
