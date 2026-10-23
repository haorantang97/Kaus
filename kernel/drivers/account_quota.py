"""Account quota capability. Readers return provider facts, never token estimates."""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
from importlib import import_module
import re
from typing import Literal


class QuotaUnavailable(Exception):
    def __init__(self, state: Literal['unsupported', 'unavailable', 'unauthenticated'] = 'unavailable', *, reason: str | None = None):
        self.state = state
        self.reason = reason
        super().__init__(state)


def native_account_label(brand, email=None, *, organization=None):
    """Display identity from a native response, never from credential files.

    The masked address is a label only: it cannot establish that two connections
    share the same subscription or quota pool.
    """
    parts = [brand]
    if (isinstance(email, str) and len(email) <= 254
            and re.fullmatch(r'[^@\s\x00-\x1f\x7f]+@[A-Za-z0-9.-]+\.[A-Za-z0-9-]+', email)):
        local, domain = email.rsplit('@', 1)
        parts.append(f'{local[0]}***@{domain}')
    if (isinstance(organization, str) and organization.strip() and len(organization) <= 100
            and not re.search(r'[\x00-\x1f\x7f]', organization)):
        parts.append(organization.strip())
    return ' · '.join(parts)


@dataclass(frozen=True)
class QuotaWindow:
    id: str
    label: str
    used_percent: float
    window_duration_mins: int | None = None
    resets_at: int | None = None
    remaining_amount: float | None = None
    limit_amount: float | None = None
    unit: str | None = None
    unlimited: bool = False

    def to_wire(self):
        result = {'id': self.id, 'label': self.label, 'usedPercent': self.used_percent,
                'remainingPercent': max(0, min(100, 100 - self.used_percent)),
                'windowDurationMins': self.window_duration_mins, 'resetsAt': self.resets_at}
        if self.remaining_amount is not None: result['remainingAmount'] = self.remaining_amount
        if self.limit_amount is not None: result['limitAmount'] = self.limit_amount
        if self.unit: result['unit'] = self.unit
        if self.unlimited: result['unlimited'] = True
        return result


@dataclass(frozen=True)
class QuotaBalance:
    id: str
    label: str
    remaining: float
    currency: str

    def to_wire(self):
        return {'id': self.id, 'label': self.label, 'remaining': self.remaining, 'currency': self.currency}


@dataclass(frozen=True)
class AccountQuota:
    provider: str
    account_label: str
    windows: tuple[QuotaWindow, ...]
    plan: str | None = None
    # A provider-issued identity, hashed by the adapter. Never infer from email,
    # model name, or provider name: one person may have several workspaces.
    account_key: str | None = None
    balances: tuple[QuotaBalance, ...] = ()
    kind: Literal['subscription', 'plan', 'balance'] = 'subscription'
    source_id: str | None = None


@dataclass(frozen=True)
class QuotaIssue:
    state: Literal['unsupported', 'unavailable', 'unauthenticated']
    provider: str | None = None
    reason: str | None = None
    source_id: str | None = None
    setup_provider: str | None = None
    setup_config: dict | None = None
    # Internal defaults only. Public DTOs must never serialize this field.
    setup_secrets: dict | None = field(default=None, repr=False)
    setup_origin: str | None = None


@dataclass(frozen=True)
class QuotaReadResult:
    accounts: tuple[AccountQuota, ...] = ()
    issues: tuple[QuotaIssue, ...] = ()


def quota_reader(driver):
    custom = getattr(driver, 'read_account_quota', None)
    module = getattr(getattr(driver, 'preset', None), 'account_quota_adapter', None)
    native = custom if callable(custom) else None
    if native is None and module:
        adapter = import_module(module)
        async def native():
            return await adapter.read_account_quota(driver)

    async def read():
        from drivers.provider_quota import read_provider_quotas
        async def invoke(reader):
            try:
                result = await asyncio.wait_for(reader(), timeout=35)
                if isinstance(result, AccountQuota): return QuotaReadResult((result,))
                if isinstance(result, QuotaReadResult): return result
                return QuotaReadResult(issues=(QuotaIssue('unavailable', reason='quota_not_returned'),))
            except QuotaUnavailable as exc:
                return QuotaReadResult(issues=(QuotaIssue(exc.state, reason=exc.reason),))
            except Exception:
                return QuotaReadResult(issues=(QuotaIssue('unavailable', reason='request_failed'),))
        async def providers():
            return await read_provider_quotas(driver)
        readers = [providers] + ([native] if native is not None else [])
        results = await asyncio.gather(*(invoke(reader) for reader in readers))
        accounts = tuple(account for result in results for account in result.accounts)
        issues = tuple(issue for result in results for issue in result.issues)
        # A native subscription may not apply when the Agent uses an API provider.
        if accounts:
            issues = tuple(issue for issue in issues if issue.provider or issue.state != 'unsupported')
        if not accounts and not issues:
            issues = (QuotaIssue('unsupported', reason='provider_not_configured'),)
        return QuotaReadResult(accounts, issues)
    return read
