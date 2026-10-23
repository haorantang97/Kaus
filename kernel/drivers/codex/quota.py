"""Codex's public read-only app-server account/rateLimits/read capability.

The native CLI owns credentials. This reader does not open auth files, create
threads, prompt models, change configuration, or consume reset credits.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from urllib.parse import urlparse

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.acp.client import AcpAgentSpec, AcpConnection, AcpRpcError
from drivers.acp.native_cli import native_path


def _positive_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _label(value, fallback):
    if isinstance(value, str) and value.strip() and len(value) <= 100 and not re.search(r'[\x00-\x1f\x7f]', value):
        return value.strip()
    return fallback


def _first_party_config(config, environment):
    if config.get('model_provider') not in (None, 'openai'):
        return False
    providers = config.get('model_providers') or {}
    if not isinstance(providers, dict):
        return False
    provider = providers.get('openai') or {}
    if not isinstance(provider, dict):
        return False
    for url in (provider.get('base_url'), environment.get('OPENAI_BASE_URL')):
        if url and (not isinstance(url, str) or urlparse(url).hostname != 'api.openai.com'):
            return False
    chat_url = config.get('chatgpt_base_url')
    return not chat_url or (isinstance(chat_url, str) and urlparse(chat_url).hostname in ('chatgpt.com', 'chat.openai.com'))


def normalize_limits(account, response):
    """Prefer distinct quota buckets; the legacy aggregate duplicates one bucket."""
    if not isinstance(response, dict):
        raise QuotaUnavailable()
    buckets = response.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        legacy = response.get('rateLimits')
        buckets = {'codex': legacy} if isinstance(legacy, dict) else {}
    windows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        name = _label(bucket.get('limitName'), 'Codex')
        for slot in ('primary', 'secondary'):
            raw = bucket.get(slot)
            if not isinstance(raw, dict):
                continue
            used = raw.get('usedPercent')
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                continue
            duration = _positive_integer(raw.get('windowDurationMins'))
            # Human-readable labels use actual duration, never assume 5h/7d.
            interval = (f'{duration // 1440} 天' if duration and duration % 1440 == 0 else
                        f'{duration // 60} 小时' if duration and duration % 60 == 0 else
                        f'{duration} 分钟' if duration else ('主要额度' if slot == 'primary' else '其他额度'))
            windows.append(QuotaWindow(f'{bucket_id}:{slot}', f'{name} · {interval}', used,
                                       duration, _positive_integer(raw.get('resetsAt'))))
    if not windows:
        raise QuotaUnavailable()
    current = account.get('account') or {}
    plan = current.get('planType')
    if not isinstance(plan, str) or not re.fullmatch(r'[A-Za-z0-9 _-]{1,40}', plan):
        plan = None
    routing = account.get('workspaceRouting')
    owner = routing.get('chatgptAccountId') if isinstance(routing, dict) else None
    key = hashlib.sha256(owner.encode()).hexdigest() if isinstance(owner, str) and owner else None
    return AccountQuota('OpenAI', native_account_label('ChatGPT', current.get('email')), tuple(windows), plan, key)


async def read_account_quota(driver):
    executable = native_path(driver)
    if not executable:
        raise QuotaUnavailable()
    environment = driver.agent_spec.build_env()
    # ACP-only provider overrides are not understood by the native app-server.
    # Do not read the default account and label it as this connection's quota.
    try:
        overrides = json.loads(environment.get('CODEX_CONFIG') or '{}')
    except (TypeError, ValueError):
        raise QuotaUnavailable('unsupported') from None
    if not isinstance(overrides, dict) or any(key in overrides for key in ('model_provider', 'model_providers', 'profile')):
        raise QuotaUnavailable('unsupported')
    # Avoid inheriting a Project's working directory or a local .codex config.
    # The original account/profile home stays in the inherited environment.
    with tempfile.TemporaryDirectory(prefix='kaus-quota-') as cwd:
        spec = AcpAgentSpec(command=(executable, 'app-server'), cwd=cwd,
                            env=environment, inherit_env=False)
        connection = await AcpConnection.spawn(spec)
        try:
            await connection.call('initialize', {'clientInfo': {'name': 'kaus_quota',
                'title': 'Kaus', 'version': '1.0.0'}, 'capabilities': {}}, timeout=8)
            connection.notify('initialized', {})
            config = await connection.call('config/read', {'includeLayers': False}, timeout=8)
            if not isinstance(config, dict) or not isinstance(config.get('config'), dict):
                raise QuotaUnavailable()
            if not _first_party_config(config['config'], environment):
                raise QuotaUnavailable('unsupported')
            account = await connection.call('account/read', {'refreshToken': False}, timeout=8)
            if not isinstance(account, dict):
                raise QuotaUnavailable()
            current = account.get('account')
            if current is None:
                raise QuotaUnavailable('unauthenticated' if account.get('requiresOpenaiAuth') else 'unsupported')
            if not isinstance(current, dict) or current.get('type') != 'chatgpt':
                raise QuotaUnavailable('unsupported')
            response = await connection.call('account/rateLimits/read', timeout=8)
            return normalize_limits(account, response)
        except AcpRpcError as exc:
            raise QuotaUnavailable('unsupported' if exc.code == -32601 else 'unavailable') from None
        finally:
            await connection.aclose()
