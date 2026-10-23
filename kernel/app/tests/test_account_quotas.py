"""Quota facts stay separate from tokens, credentials, and conversation state."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.account_quotas import AccountQuotaService
from app.api.account_quotas import build_account_quota_router
from app.api.session_auth import SessionAuthPolicy
from drivers.account_quota import AccountQuota, QuotaBalance, QuotaIssue, QuotaReadResult, QuotaUnavailable, QuotaWindow, native_account_label, quota_reader
from drivers.codex.quota import normalize_limits, read_account_quota
from drivers.acp.client import AcpAgentSpec
from drivers.acp.presets import PRESETS


def account():
    return {'account': {'type': 'chatgpt', 'email': 'private@example.invalid', 'planType': 'pro'}}


def driver(key='codex'):
    return SimpleNamespace(backend_id=f'backend:{key}', driver_kind='acp',
                           preset=PRESETS.get(key), agent_spec=AcpAgentSpec(command=('unused',)))


def limits(used=25):
    return {'rateLimits': {'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': 1900000000}}}


def test_multi_bucket_limits_do_not_duplicate_legacy_or_invent_missing_values():
    data = limits(99)
    data['rateLimitsByLimitId'] = {
        'codex': {'limitName': 'Codex', 'primary': {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': 1900000000}},
        'fast': {'limitName': 'Fast', 'secondary': {'usedPercent': 105}, 'primary': {'usedPercent': None}},
    }
    result = normalize_limits(account(), data)
    assert [w.used_percent for w in result.windows] == [12, 105]
    assert result.windows[0].label == 'Codex · 5 小时'
    assert result.windows[1].to_wire()['remainingPercent'] == 0
    assert result.windows[1].resets_at is None
    assert result.plan == 'pro'
    assert result.account_key is None
    assert result.account_label == 'ChatGPT · p***@example.invalid'
    assert 'private' not in repr(result)


@pytest.mark.parametrize('value', [None, {}, {'rateLimits': None}, limits(None), limits(True), limits(float('nan')), limits(-1)])
def test_missing_or_invalid_limits_are_not_zero_percent(value):
    with pytest.raises(QuotaUnavailable):
        normalize_limits(account(), value)


def test_only_explicit_provider_account_identity_permits_grouping():
    a = account()
    a['workspaceRouting'] = {'chatgptAccountId': 'account-private'}
    result = normalize_limits(a, limits())
    assert result.account_key
    assert 'account-private' not in result.account_key


async def test_on_demand_cache_force_and_registry_changes():
    calls = []
    now = [1]
    drivers = [driver()]
    async def read():
        calls.append(1)
        return normalize_limits(account(), limits())
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers),
        reader_factory=lambda _: read, clock=lambda: now[0])
    assert calls == []
    first = await service.snapshot()
    now[0] += 1
    assert await service.snapshot() == first
    assert len(calls) == 1
    now[0] += 30
    await service.snapshot(force=True)
    assert len(calls) == 2
    now[0] += 61
    await service.snapshot()
    assert len(calls) == 3
    drivers.append(driver('custom'))
    await service.snapshot()
    assert len(calls) == 5


async def test_same_provider_without_account_identity_stays_separate():
    drivers = [driver('first'), driver('second')]
    async def read():
        return normalize_limits(account(), limits())
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers), reader_factory=lambda _: read)
    result = await service.snapshot()
    assert len(result['accounts']) == 2
    assert len({row['accountLabel'] for row in result['accounts']}) == 2
    assert [row['accountLabel'] for row in result['accounts']] == [
        'ChatGPT · p***@example.invalid · 账号 1', 'ChatGPT · p***@example.invalid · 账号 2']
    assert 'private@example' not in json.dumps(result)


@pytest.mark.parametrize(('email', 'label'), [
    ('someone@example.com', 'Claude · s***@example.com'),
    ('a@example.com', 'Claude · a***@example.com'),
    (None, 'Claude'), ('not-an-email', 'Claude'),
    ('name@example.com\n/private/config', 'Claude'),
    ('\x00@example.com', 'Claude'), ('name@/private/config.test', 'Claude'),
])
def test_native_display_identity_is_masked_and_invalid_values_are_ignored(email, label):
    assert native_account_label('Claude', email) == label


def test_native_organization_is_display_only():
    assert native_account_label('Claude', 'person@example.com', organization='Design Team') == 'Claude · p***@example.com · Design Team'
    assert native_account_label('Claude', None, organization='invalid\ntext') == 'Claude'


async def test_proven_shared_account_is_one_quota_not_two_totals():
    drivers = [driver('first'), driver('second')]
    async def read():
        return AccountQuota('Provider', 'Account', (QuotaWindow('one', 'Week', 30),), account_key='proven-id')
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers), reader_factory=lambda _: read).snapshot()
    assert len(result['accounts']) == 1
    assert result['accounts'][0]['agentIds'] == ['backend:first', 'backend:second']
    assert result['accounts'][0]['windows'][0]['usedPercent'] == 30


async def test_grouping_preserves_distinct_quota_buckets():
    drivers = [driver('first'), driver('second')]
    def reader(d):
        async def read():
            return AccountQuota('Provider', 'Account', (QuotaWindow(d.backend_id, 'Week', 30),), account_key='proven-id')
        return read
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers), reader_factory=reader).snapshot()
    assert len(result['accounts']) == 1
    assert len(result['accounts'][0]['windows']) == 2


async def test_force_has_cooldown_and_failed_refresh_drops_previous_account():
    now = [10]
    failures = [False]
    calls = []
    async def read():
        calls.append(1)
        if failures[0]:
            raise QuotaUnavailable('unauthenticated')
        return normalize_limits(account(), limits())
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: [driver_item]),
        reader_factory=lambda _: read, clock=lambda: now[0])
    driver_item = driver()
    assert len((await service.snapshot())['accounts']) == 1
    now[0] += 1
    await service.snapshot(force=True)
    assert len(calls) == 1
    now[0] += 31
    failures[0] = True
    result = await service.snapshot(force=True)
    assert result['accounts'] == []
    assert result['unavailable'][0]['state'] == 'unauthenticated'


async def test_one_failed_reader_does_not_hide_others_or_leak_error():
    drivers = [driver('good'), driver('bad'), driver('none')]
    async def read_good():
        return normalize_limits(account(), limits())
    async def read_bad():
        raise RuntimeError('/private/home secret-key')
    readers = {'backend:good': read_good, 'backend:bad': read_bad}
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers),
        reader_factory=lambda d: readers.get(d.backend_id)).snapshot()
    assert len(result['accounts']) == 1
    assert [row['state'] for row in result['unavailable']] == ['unavailable', 'unsupported']
    assert 'secret-key' not in json.dumps(result)


async def test_one_agent_can_report_separate_provider_accounts_and_partial_failure():
    async def read():
        return QuotaReadResult(accounts=(
            AccountQuota('Provider A', 'A', (), balances=(QuotaBalance('usd', 'Balance', 12.5, 'USD'),), kind='balance'),
            AccountQuota('Provider B', 'B', (QuotaWindow('month', 'Month', 30, remaining_amount=70, limit_amount=100, unit='requests'),), kind='plan'),
        ), issues=(QuotaIssue('unauthenticated', 'Provider C', 'credential_expired'),
                   QuotaIssue('unavailable', reason='/private/path-secret-key')))
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: [driver('custom')]), reader_factory=lambda _: read).snapshot()
    assert len(result['accounts']) == 2
    assert result['accounts'][0]['balances'] == [{'id': 'usd', 'label': 'Balance', 'remaining': 12.5, 'currency': 'USD'}]
    assert result['accounts'][1]['windows'][0]['remainingAmount'] == 70
    assert result['unavailable'][0]['reason'] == 'credential_expired'
    assert 'private' not in json.dumps(result)


async def test_same_verified_account_merges_balances_and_plan_without_duplicate_agents():
    async def read():
        return QuotaReadResult(accounts=(
            AccountQuota('Provider', 'Account', (), account_key='same', balances=(QuotaBalance('usd', 'Balance', 12, 'USD'),), kind='balance'),
            AccountQuota('Provider', 'Account', (QuotaWindow('requests', 'Requests', 0, unlimited=True),), account_key='same', kind='plan'),
        ))
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: [driver('custom')]), reader_factory=lambda _: read).snapshot()
    assert len(result['accounts']) == 1
    account = result['accounts'][0]
    assert account['agentIds'] == ['backend:custom']
    assert account['kind'] == 'mixed'
    assert account['windows'][0]['unlimited'] is True
    assert account['balances'][0]['remaining'] == 12


async def test_invalid_native_reader_preserves_successful_provider_balance(monkeypatch):
    async def native():
        return None
    async def providers(_):
        return QuotaReadResult((AccountQuota('Provider', 'Account', (), balances=(QuotaBalance('usd', 'Balance', 12, 'USD'),), kind='balance'),))
    monkeypatch.setattr('drivers.provider_quota.read_provider_quotas', providers)
    item = driver('custom')
    item.read_account_quota = native
    result = await quota_reader(item)()
    assert result.accounts[0].balances[0].remaining == 12
    assert result.issues == (QuotaIssue('unavailable', reason='quota_not_returned'),)


def test_api_requires_local_session_auth():
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: []))
    app = FastAPI()
    app.include_router(build_account_quota_router(service, auth_policy=SessionAuthPolicy(lambda: 'test-token-quota')))
    with TestClient(app) as client:
        assert client.get('/api/account-quotas').status_code == 401
        response = client.get('/api/account-quotas?force=1', headers={'Authorization': 'Bearer test-token-quota'})
        assert response.status_code == 200
        assert response.json()['accounts'] == []


class FakeConnection:
    def __init__(self, current=None):
        self.calls = []
        self.current = current or account()
        self.closed = False

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == 'account/read':
            return self.current
        if method == 'config/read':
            return {'config': {}}
        if method == 'account/rateLimits/read':
            return limits()
        return {}

    def notify(self, method, params):
        self.calls.append((method, params))

    async def aclose(self):
        self.closed = True


async def test_native_reader_uses_only_read_methods_and_closes(monkeypatch):
    connection = FakeConnection()
    spawned = []
    async def spawn(spec):
        spawned.append(spec)
        return connection
    monkeypatch.setattr('drivers.codex.quota.native_path', lambda _: '/fixture/codex')
    monkeypatch.setattr('drivers.codex.quota.AcpConnection.spawn', spawn)
    item = driver()
    item.agent_spec = AcpAgentSpec(command=('unused',), env={'CODEX_HOME': '/fixture/account'}, inherit_env=False)
    result = await read_account_quota(item)
    assert [x[0] for x in connection.calls] == ['initialize', 'initialized', 'config/read', 'account/read', 'account/rateLimits/read']
    assert connection.calls[3][1] == {'refreshToken': False}
    assert spawned[0].command == ('/fixture/codex', 'app-server')
    assert spawned[0].env['CODEX_HOME'] == '/fixture/account'
    assert connection.closed
    assert result.windows[0].used_percent == 25


@pytest.mark.parametrize(('current', 'state'), [({'account': {'type': 'apiKey'}}, 'unsupported'),
    ({'account': None, 'requiresOpenaiAuth': True}, 'unauthenticated')])
async def test_api_keys_and_signed_out_accounts_do_not_request_subscription_limits(monkeypatch, current, state):
    connection = FakeConnection(current)
    async def spawn(_): return connection
    monkeypatch.setattr('drivers.codex.quota.native_path', lambda _: '/fixture/codex')
    monkeypatch.setattr('drivers.codex.quota.AcpConnection.spawn', spawn)
    with pytest.raises(QuotaUnavailable) as exc:
        await read_account_quota(driver())
    assert exc.value.state == state
    assert 'account/rateLimits/read' not in [x[0] for x in connection.calls]
    assert connection.closed


async def test_cancellation_closes_native_process(monkeypatch):
    connection = FakeConnection()
    async def cancelled(*args, **kwargs): raise asyncio.CancelledError()
    connection.call = cancelled
    async def spawn(_): return connection
    monkeypatch.setattr('drivers.codex.quota.native_path', lambda _: '/fixture/codex')
    monkeypatch.setattr('drivers.codex.quota.AcpConnection.spawn', spawn)
    with pytest.raises(asyncio.CancelledError):
        await read_account_quota(driver())
    assert connection.closed
