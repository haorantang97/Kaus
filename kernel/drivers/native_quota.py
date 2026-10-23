"""Bounded, read-only transport and parsing for native account usage readers."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json as json_module
import math
import os
from pathlib import Path
import re

import httpx

from drivers.account_quota import QuotaUnavailable

LIMIT = 4 * 1024 * 1024


def driver_env(driver):
    spec = getattr(driver, 'agent_spec', None)
    return spec.build_env() if spec is not None else dict(os.environ)


def finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def reset_at(value):
    number = finite(value)
    if number is not None:
        return int(number / 1000 if number > 10_000_000_000 else number) if number > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return int(parsed.timestamp()) if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def account_key(host, credential):
    return hashlib.sha256((host + '\0' + credential).encode()).hexdigest()


def read_json(path, *, jsonc=False):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise QuotaUnavailable(reason='credential_unavailable')
        text = raw.decode('utf-8-sig')
        if jsonc:
            tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/|,\s*(?=[}\]])')
            text = tokens.sub(lambda match: match[0] if match[0].startswith('"') else '', text)
        value = json_module.loads(text)
        if not isinstance(value, dict):
            raise QuotaUnavailable(reason='credential_unavailable')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise QuotaUnavailable(reason='credential_unavailable') from None


async def request_json(method, url, *, headers=None, json=None, data=None):
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, url, headers=headers, json=json, data=data) as response:
                if response.status_code == 401:
                    raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
                if response.status_code == 403:
                    raise QuotaUnavailable(reason='permission_required')
                if not 200 <= response.status_code < 300:
                    raise QuotaUnavailable(reason='request_failed')
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > LIMIT:
                        raise QuotaUnavailable(reason='quota_not_returned')
        value = json_module.loads(chunks)
        if not isinstance(value, (dict, list)):
            raise QuotaUnavailable(reason='quota_not_returned')
        return value
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise QuotaUnavailable(reason='request_failed') from None
