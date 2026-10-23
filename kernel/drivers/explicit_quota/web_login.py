"""StepFun login boundary shared by the desktop child and quota reader.

Token claims select the expected identity only. The platform must accept the
session before it is saved; decoding a JWT is not authentication.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlsplit

SITES = {
    'ai': ('https://platform.stepfun.ai', '20700'),
    'com': ('https://platform.stepfun.com', '10300'),
}
TOKEN_LIMIT = 16384


class LoginInvalid(ValueError):
    def __init__(self, reason='sign_in_required'):
        self.reason = reason
        super().__init__(reason)


def site_details(site):
    if not isinstance(site, str) or site not in SITES:
        raise LoginInvalid('invalid_fields')
    return SITES[site]


def login_url(site):
    return site_details(site)[0] + '/step-plan'


def token_claims(token):
    if (not isinstance(token, str) or len(token) > TOKEN_LIMIT
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)):
        raise LoginInvalid()
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if not isinstance(claims, dict):
            raise ValueError()
        return claims
    except (ValueError, UnicodeError):
        raise LoginInvalid() from None


def _identity(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if (not isinstance(value, str) or not value or len(value) > 256
            or not re.fullmatch(r'[A-Za-z0-9._:-]+', value)):
        raise LoginInvalid()
    return value


def clean_session(captured):
    if not isinstance(captured, dict):
        raise LoginInvalid()
    access, refresh = captured.get('access'), captured.get('refresh')
    claims, grant = token_claims(access), token_claims(refresh)
    if claims.get('activated') is not True:
        raise LoginInvalid()
    account = _identity(claims.get('oasis_id'))
    if captured.get('account') and captured['account'] != account:
        raise LoginInvalid('credential_expired')
    grant_account = grant.get('oasis_id')
    if grant_account is not None and _identity(grant_account) != account:
        raise LoginInvalid('credential_expired')
    # The refresh grant's device is authoritative, not local storage or input.
    web_id = _identity(grant.get('device_id') or captured.get('webId'))
    return dict(access=access, refresh=refresh, webId=web_id, account=account)


def capture_session(site, page_url, cookies):
    """Read only cookies supplied by this newly created, isolated WebView."""
    origin, _ = site_details(site)
    try:
        page = urlsplit(page_url)
        if (page.scheme != 'https' or page.hostname != urlsplit(origin).hostname
                or page.port not in (None, 443) or page.username or page.password):
            return None
    except (ValueError, TypeError):
        return None
    domains = {'platform.stepfun.' + site, 'stepfun.' + site}
    for cookie in cookies:
        if (cookie.get('name') != 'Oasis-Token'
                or str(cookie.get('domain', '')).lstrip('.') not in domains
                or not cookie.get('secure') or cookie.get('path', '/') != '/'):
            continue
        raw = cookie.get('value', '')
        if not isinstance(raw, str) or len(raw) > TOKEN_LIMIT * 2 + 3:
            continue
        parts = raw.split('...')
        if len(parts) != 2:
            continue
        try:
            return clean_session(dict(access=parts[0], refresh=parts[1]))
        except LoginInvalid:
            continue
    return None
