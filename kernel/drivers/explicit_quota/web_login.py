"""StepFun session checks for the quota reader. The desktop shell captures the
sign-in cookie (desktop/chromium/daily.cjs); this module validates what it sends.

Token claims select the expected identity only. The platform must accept the
session before it is saved; decoding a JWT is not authentication.
"""
from __future__ import annotations

import base64
import json
import re

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
