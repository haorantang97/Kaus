"""Identify an existing inference source without exposing its credential.

Plan recognition is exact host + documented route. Custom origins are metadata
only: discovery never guesses a billing URL or sends their key to another host.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from drivers.account_quota import QuotaIssue


def normalized_source_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname.lower()
        # Domain names are public routing metadata; reject malformed names and
        # IPv6 literals here instead of allowing URL parser discrepancies.
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
            return None
        return urlunsplit(('https', host, parsed.path.rstrip('/'), '', ''))
    except ValueError:
        return None


def source_id_for(base_url, api_key):
    normalized = normalized_source_url(base_url)
    if normalized is None:
        return None
    key = api_key.strip() if isinstance(api_key, str) else ''
    encoded = json.dumps([_identity_scope(normalized), key], ensure_ascii=True, separators=(',', ':')).encode()
    return 'provider-source:' + hashlib.sha256(encoded).hexdigest()


def valid_key(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 16384
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _route(path, prefix):
    return path == prefix or path.startswith(prefix + '/')


def _identity_scope(normalized):
    """Equivalent API aliases identify one source; explicit plan pools do not."""
    parsed = urlsplit(normalized)
    host, path = parsed.hostname, parsed.path
    origin = 'https://' + host
    if re.fullmatch(r'ark\.([a-z0-9]+(?:-[a-z0-9]+)*)\.volces\.com', host):
        for prefix in ('/api/coding', '/api/plan'):
            if _route(path, prefix):
                return origin + prefix
    if host in ('tokenhub.tencentmaas.com', 'tokenhub-intl.tencentmaas.com') and _route(path, '/plan'):
        return origin + '/plan'
    if host in ('api.stepfun.com', 'api.stepfun.ai') and _route(path, '/step_plan'):
        return origin + '/step_plan'
    # Imported at call time after the provider module has initialized; endpoint
    # classification remains centralized and does not cause network access.
    from . import endpoint_for
    selected = endpoint_for(normalized)
    if selected:
        query, endpoint = selected
        if endpoint.parser == 'zhipu' and _route(path, '/api/coding'):
            return query + '#coding-plan'
        return query
    return normalized


def setup_issue_for(base_url, api_key, *, supported=False):
    """Return the extra credential needed by this source, without probing it."""
    normalized = normalized_source_url(base_url)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    host, path = parsed.hostname, parsed.path
    source_id = source_id_for(normalized, api_key)
    origin = 'https://' + host
    volc = re.fullmatch(r'ark\.([a-z0-9]+(?:-[a-z0-9]+)*)\.volces\.com', host)
    if volc and (_route(path, '/api/coding') or _route(path, '/api/plan')):
        return QuotaIssue('unauthenticated', '火山方舟', 'cloud_credentials_required', source_id,
                          'volcengine', {'region': volc[1], 'plan': 'coding' if _route(path, '/api/coding') else 'agent'},
                          setup_origin=origin)
    if host in ('tokenhub.tencentmaas.com', 'tokenhub-intl.tencentmaas.com') and _route(path, '/plan'):
        region = 'ap-guangzhou' if host == 'tokenhub.tencentmaas.com' else 'ap-singapore'
        return QuotaIssue('unauthenticated', '腾讯 TokenHub', 'cloud_credentials_required', source_id,
                          'tencent', {'region': region}, setup_origin=origin)
    if host in ('api.stepfun.com', 'api.stepfun.ai') and _route(path, '/step_plan'):
        return QuotaIssue('unauthenticated', 'StepFun Plan', 'web_session_required', source_id,
                          'stepfun-plan', {'site': 'com' if host.endswith('.com') else 'ai'}, setup_origin=origin)
    if supported:
        return None
    # Known first-party routes with no supported billing API are not custom
    # relays and must not ask users to invent a balance endpoint.
    if (volc or host in {'tokenhub.tencentmaas.com', 'tokenhub-intl.tencentmaas.com',
                        'api.lkeap.cloud.tencent.com', 'api.openai.com', 'api.anthropic.com',
                        'api.kimi.com', 'api.kimi.ai', 'opencode.ai', 'generativelanguage.googleapis.com'}):
        return QuotaIssue('unsupported', reason='no_usage_api', source_id=source_id)
    return QuotaIssue('unsupported', '自定义服务', 'unsupported_provider', source_id, 'custom', {},
                      {'apiKey': api_key.strip()} if valid_key(api_key) else None, origin)


def failure_issue(source, endpoint, state, reason):
    """Ordinary login/network failures never turn into another credential form."""
    setup = endpoint.parser if reason == 'management_key_required' and endpoint.parser in ('openrouter', 'aihubmix') else None
    normalized = normalized_source_url(source.base_url)
    return QuotaIssue(state, endpoint.provider, reason, source.source_id,
                      setup, {} if setup else None,
                      setup_origin='https://' + urlsplit(normalized).hostname if setup and normalized else None)
