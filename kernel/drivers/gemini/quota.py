"""Gemini CLI OAuth quota, scoped to this connection's native home."""
from __future__ import annotations

from pathlib import Path
import re
import shlex

from drivers.account_quota import QuotaUnavailable
from drivers.google_quota import official_endpoint, read_google_quota
from drivers.google_oauth import native_client
from drivers.native_quota import driver_env, read_json


def native_environment(env, root):
    """Read the native user's dotenv without executing shell expressions."""
    result = {}
    try:
        with (root / '.env').open('r') as stream:
            content = stream.read(1024 * 1024)
    except FileNotFoundError:
        return env
    except (OSError, UnicodeError):
        raise QuotaUnavailable(reason='credential_unavailable') from None
    for line in content.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if not match:
            continue
        try:
            values = shlex.split(match[2], comments=True)
        except ValueError:
            continue
        if len(values) == 1:
            result[match[1]] = values[0]
    return {**result, **env}


async def read_account_quota(driver):
    env = driver_env(driver)
    root = Path(env.get('GEMINI_CLI_HOME') or env.get('HOME') or Path.home()) / '.gemini'
    env = native_environment(env, root)
    settings = read_json(root / 'settings.json', jsonc=True)
    selected = ((settings.get('security') or {}).get('auth') or {}).get('selectedType')
    if selected and selected != 'oauth-personal':
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    if not selected and (env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY')
                         or env.get('GOOGLE_GENAI_USE_VERTEXAI', '').lower() in ('1', 'true')):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    endpoint = official_endpoint(env.get('CODE_ASSIST_ENDPOINT'))
    project = env.get('GOOGLE_CLOUD_PROJECT') or env.get('GOOGLE_CLOUD_PROJECT_ID') or ''
    if env.get('GOOGLE_GENAI_USE_GCA') and env.get('GOOGLE_CLOUD_ACCESS_TOKEN'):
        return await read_google_quota({}, project=project, endpoint=endpoint,
                                       direct_token=env['GOOGLE_CLOUD_ACCESS_TOKEN'])
    if env.get('GEMINI_FORCE_ENCRYPTED_FILE_STORAGE') == 'true':
        raise QuotaUnavailable(reason='credential_access_required')
    credentials = read_json(root / 'oauth_creds.json')
    if not credentials:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return await read_google_quota(credentials, project=project, endpoint=endpoint,
                                   resolve_client=lambda: native_client(driver, env=env))
