"""Current Hermes Codex OAuth account, through its native read-only grant.

Hermes owns this grant. No Codex/Pi credential directories are read, and quota
requests never refresh or write the native token chain.
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from drivers.account_quota import QuotaIssue, QuotaReadResult, QuotaUnavailable
from drivers.native_quota import driver_env, read_json, reset_at
from drivers.pi import vendor_quota as vendor
from drivers.provider_quota.discovery import document, dotenv, mapping, merge
from drivers.provider_quota.source_context import source_id_for

PROVIDER = 'openai-codex'
CODEX_BASE = 'https://chatgpt.com/backend-api/codex'


def is_codex_route(model, env=None):
    if str(model.get('provider') or '').strip().lower() != PROVIDER:
        return False
    override = (env or {}).get('HERMES_CODEX_BASE_URL')
    value = override.strip() if isinstance(override, str) and override.strip() else model.get('base_url')
    if value is None:
        return True
    try:
        route = urlsplit(value)
        return bool(route.scheme == 'https' and route.hostname == 'chatgpt.com'
                    and not route.username and not route.password and route.port in (None, 443)
                    and not route.query and not route.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def _homes(driver):
    env = driver_env(driver)
    user_home = Path(env.get('HOME') or Path.home())
    native_root = Path(env.get('LOCALAPPDATA') or user_home / 'AppData/Local') / 'hermes' if sys.platform == 'win32' else user_home / '.hermes'
    gateway = getattr(driver, 'default_gateway', None)
    selected = Path(getattr(gateway, 'hermes_home', None) or getattr(driver, 'hermes_root', None)
                    or env.get('HERMES_HOME') or native_root).expanduser()
    # Matches Hermes get_default_hermes_root: native profiles inherit their
    # own root; an unrelated custom HERMES_HOME is itself a root.
    try:
        selected.resolve().relative_to(native_root.resolve())
        root = native_root
    except ValueError:
        root = selected.parent.parent if selected.parent.name == 'profiles' else selected
    return selected, root


def configuration(driver):
    home, auth_root = _homes(driver)
    gateway = getattr(driver, 'default_gateway', None)
    base = getattr(driver, 'hermes_root', None) if getattr(gateway, 'hermes_home', None) else None
    # HTTP settings inherit the root, but model identity/provider/route stay
    # in one layer. Native ACP configuration is its own HERMES_HOME.
    roots = list(dict.fromkeys([Path(base).expanduser(), home])) if base else [home]
    config, env = {}, driver_env(driver)
    for root in roots:
        layer = document(root / 'config.yaml')
        config = merge(config, layer)
        model = layer.get('model')
        if 'model' in layer and (not isinstance(model, dict) or any(
                key in model for key in ('default', 'model_id', 'id', 'provider', 'provider_id', 'base_url', 'api_key'))):
            config['model'] = model
        env = {**env, **dotenv(root / '.env')}
    return home, auth_root, config, env


def _credential(driver):
    home, root, config, env = configuration(driver)
    model = mapping(config.get('model'))
    if str(model.get('provider') or '').strip().lower() != PROVIDER:
        return None
    if not is_codex_route(model, env):
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    auth = read_json(home / 'auth.json')
    suppressed = mapping(auth.get('suppressed_sources')).get(PROVIDER) or []
    if isinstance(suppressed, (list, dict)) and 'device_code' in suppressed:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    # Native provider state is authoritative even when empty or expired. Only
    # absence allows the documented same-Hermes root fallback.
    state = mapping(auth.get('providers')).get(PROVIDER)
    if not isinstance(state, dict) and home.resolve() != root.resolve():
        fallback = read_json(root / 'auth.json')
        state = mapping(fallback.get('providers')).get(PROVIDER)
    tokens = mapping(mapping(state).get('tokens'))
    bearer = tokens.get('access_token')
    if not isinstance(bearer, str) or not bearer.strip():
        # Native resolve_codex_runtime_credentials falls back to the first
        # usable entry in this auth.json's exact provider pool. It does not
        # import another CLI or scan other provider pools.
        entries = mapping(auth.get('credential_pool')).get(PROVIDER)
        for entry in entries if isinstance(entries, list) else ():
            entry = mapping(entry)
            until = reset_at(entry.get('last_error_reset_at'))
            value = entry.get('access_token')
            if isinstance(value, str) and value.strip() and (until is None or until <= time.time()):
                bearer = value
                break
    bearer = vendor.token(bearer.strip() if isinstance(bearer, str) else bearer)
    expires = reset_at(vendor.claims(bearer).get('exp'))
    if expires is None:
        expires = reset_at(tokens.get('expires_at') or mapping(state).get('expires_at'))
    return {'access': bearer, 'expires': expires}, source_id_for(CODEX_BASE, bearer)


async def read_account_quota(driver, *, total_timeout=15):
    source_id = None
    try:
        credential = _credential(driver)
        if credential is None:
            return QuotaReadResult()
        value, source_id = credential
        account = await asyncio.wait_for(vendor.read_codex(value), total_timeout)
        windows = []
        for window in account.windows:
            duration = window.window_duration_mins
            period = (f'{duration // 1440} 天' if duration and duration % 1440 == 0 else
                      f'{duration // 60} 小时' if duration and duration % 60 == 0 else
                      f'{duration} 分钟' if duration else
                      '主要额度' if window.id.endswith(':primary') else '其他额度')
            windows.append(replace(window, label=f'{window.label} · {period}'))
        return QuotaReadResult((replace(account, windows=tuple(windows), source_id=source_id),))
    except QuotaUnavailable as error:
        return QuotaReadResult(issues=(QuotaIssue(error.state, 'OpenAI', error.reason, source_id),))
    except (TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return QuotaReadResult(issues=(QuotaIssue('unavailable', 'OpenAI', 'request_failed', source_id),))
