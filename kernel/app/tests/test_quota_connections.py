import asyncio
import json
import stat
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.account_quotas import AccountQuotaService
from app.api.quota_connections import build_quota_connections_router
from app.api.session_auth import SessionAuthPolicy
from app.quota_connections import QuotaConnections
from drivers.account_quota import AccountQuota, QuotaBalance, QuotaUnavailable
from drivers.explicit_quota.catalog import ConnectionInvalid


SECRET = 'synthetic-management-secret'


def draft(**extra):
    return dict(provider='openrouter', config={}, secrets={'managementKey': SECRET}, **extra)


async def reader(provider, config, secrets):
    if secrets.get('managementKey') == 'bad':
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    return AccountQuota('OpenRouter', 'OpenRouter', (), account_key='proven-account',
                        balances=(QuotaBalance('credits', '余额', 12, 'USD'),), kind='balance')


def client_for(service):
    app = FastAPI()
    app.include_router(build_quota_connections_router(service, auth_policy=SessionAuthPolicy(lambda: 'local-token')))
    return TestClient(app)


AUTH = {'Authorization': 'Bearer local-token'}


def test_api_connection_roundtrip_is_write_only_and_auth_guarded(tmp_path):
    service = QuotaConnections(tmp_path, reader=reader)
    with client_for(service) as client:
        assert client.get('/api/quota-connections').status_code == 401
        assert client.get('/api/quota-connections/catalog').status_code == 401
        assert client.post('/api/quota-connections', json=draft(), headers=AUTH | {'Origin': 'https://evil.invalid'}).status_code == 403
        result = client.post('/api/quota-connections', json=draft(), headers=AUTH)
        assert result.status_code == 200
        row = result.json()['connection']
        assert row['secretFields'] == ['managementKey'] and SECRET not in result.text
        listed = client.get('/api/quota-connections', headers=AUTH)
        assert SECRET not in listed.text and 'secrets' not in listed.text
        identifier = row['id']
        edited = client.put('/api/quota-connections/' + identifier,
                            json={'label': 'Research', 'secrets': {'managementKey': ''}}, headers=AUTH)
        assert edited.status_code == 200 and edited.json()['connection']['label'] == 'Research'
        failed = client.put('/api/quota-connections/' + identifier,
                            json={'secrets': {'managementKey': 'bad'}}, headers=AUTH)
        assert failed.status_code == 400 and failed.json()['reason'] == 'credential_expired'
        assert service.store.get(identifier)['secrets']['managementKey'] == SECRET
        assert client.delete('/api/quota-connections/' + identifier, headers=AUTH).json() == {'ok': True}
        assert client.get('/api/quota-connections', headers=AUTH).json() == {'connections': []}
    assert SECRET.encode() not in (tmp_path / 'connections.enc').read_bytes()
    assert stat.S_IMODE((tmp_path / 'connections.enc').stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / 'key').stat().st_mode) == 0o600


def test_error_does_not_echo_body_or_native_exception(tmp_path):
    async def broken(*args): raise RuntimeError('/private/path ' + SECRET)
    with client_for(QuotaConnections(tmp_path, reader=broken)) as client:
        response = client.post('/api/quota-connections', json=draft(), headers=AUTH)
        assert response.status_code == 502
        assert response.json() == {'ok': False, 'reason': 'request_failed'}
        response = client.post('/api/quota-connections', json=draft(extraSecret=SECRET), headers=AUTH)
        assert SECRET not in response.text


async def test_invalid_config_and_unknown_secret_are_never_read(tmp_path):
    calls = []
    async def forbidden(*args): calls.append(args); raise AssertionError()
    service = QuotaConnections(tmp_path, reader=forbidden)
    for body in [dict(provider='unknown'), dict(provider='openrouter', config={'endpoint': 'https://evil.invalid'}, secrets={'managementKey': SECRET}),
                 dict(provider='openrouter', config={}, secrets={'managementKey': SECRET, 'other': SECRET})]:
        with pytest.raises(ConnectionInvalid): await service.save(body)
    assert not calls


async def test_storage_survives_restart_and_corruption_is_not_overwritten(tmp_path):
    service = QuotaConnections(tmp_path, reader=reader)
    row = (await service.save(draft()))['connection']
    restored = QuotaConnections(tmp_path, reader=reader)
    assert restored.store.get(row['id'])['secrets']['managementKey'] == SECRET
    (tmp_path / 'connections.enc').write_bytes(b'corrupt')
    with pytest.raises(ConnectionInvalid, match='storage_unavailable'): restored.list()
    assert (tmp_path / 'connections.enc').read_bytes() == b'corrupt'


async def test_explicit_accounts_merge_by_identity_and_cache_invalidates(tmp_path):
    connections = QuotaConnections(tmp_path, reader=reader)
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: []), connections=connections)
    assert not (await service.snapshot())['accounts']
    first = (await connections.save(draft(label='One')))['connection']['id']
    second = (await connections.save(draft(label='Two')))['connection']['id']
    result = await service.snapshot()
    assert len(result['accounts']) == 1
    assert set(result['accounts'][0]['connectionIds']) == {first, second}
    assert result['accounts'][0]['agentIds'] == []
    await connections.delete(first); await connections.delete(second)
    assert not (await service.snapshot())['accounts']


async def test_secret_refresh_is_saved_even_if_later_quota_fails(tmp_path):
    service = QuotaConnections(tmp_path, reader=reader)
    identifier = (await service.save(draft()))['connection']['id']
    async def rotate(*args):
        args[-1]['managementKey'] = 'synthetic-rotated'
        raise QuotaUnavailable(reason='request_failed')
    service.reader = rotate
    result = await service.read_accounts()
    assert result[0][2].issues[0].reason == 'request_failed'
    assert service.store.get(identifier)['secrets']['managementKey'] == 'synthetic-rotated'


async def test_draft_validation_does_not_replace_saved_credentials(tmp_path):
    service = QuotaConnections(tmp_path, reader=reader)
    identifier = (await service.save(draft()))['connection']['id']
    async def rotate(*args):
        args[-1]['managementKey'] = 'rotated-different-account'
        raise QuotaUnavailable(reason='request_failed')
    service.reader = rotate
    with pytest.raises(QuotaUnavailable):
        await service.test({'secrets': {'managementKey': 'another-account'}}, identifier)
    assert service.store.get(identifier)['secrets']['managementKey'] == SECRET


async def test_login_ticket_scope_claim_cancel_and_expiry(tmp_path):
    now = [0]
    service = QuotaConnections(tmp_path, reader=reader, clock=lambda: now[0])
    ticket = service.begin_login({'provider': 'stepfun-plan', 'config': {'site': 'com'}})['loginId']
    with pytest.raises(ConnectionInvalid, match='login_not_started'):
        await service.complete_login(ticket, {'secrets': {'access': 'access', 'refresh': 'refresh'}})
    claim = service.claim_login(ticket)
    assert claim == {'provider': 'stepfun-plan', 'config': {'site': 'com'}}
    assert service.cancel_login(ticket)['status'] == 'cancelled'
    assert (await service.complete_login(ticket, {'secrets': {'access': 'access', 'refresh': 'refresh'}}))['status'] == 'cancelled'
    assert not service.list()['connections']
    now[0] = 601
    with pytest.raises(ConnectionInvalid, match='login_expired'): service.login_state(ticket)


async def test_login_completion_validates_saves_and_is_idempotent(tmp_path):
    calls = []
    async def capture(provider, config, secrets):
        calls.append((provider, config))
        assert config['site'] == 'ai'
        secrets['refresh'] = 'rotated'
        return await reader(provider, config, secrets)
    service = QuotaConnections(tmp_path, reader=capture)
    ticket = service.begin_login({'provider': 'stepfun-plan', 'config': {'site': 'ai'}})['loginId']
    service.claim_login(ticket)
    result = await service.complete_login(ticket, {'secrets': {'access': 'access', 'refresh': 'refresh'}})
    assert result['status'] == 'connected'
    assert service.store.get(result['connectionId'])['secrets']['refresh'] == 'rotated'
    assert await service.complete_login(ticket, {}) == result
    assert len(calls) == 1


async def test_cancel_during_login_validation_does_not_create_connection(tmp_path):
    started, proceed = asyncio.Event(), asyncio.Event()
    async def paused(*args): started.set(); await proceed.wait(); return await reader(*args)
    service = QuotaConnections(tmp_path, reader=paused)
    ticket = service.begin_login({'provider': 'stepfun-plan', 'config': {'site': 'com'}})['loginId']
    service.claim_login(ticket)
    task = asyncio.create_task(service.complete_login(ticket, {'secrets': {'access': 'access', 'refresh': 'refresh'}}))
    await started.wait()
    service.cancel_login(ticket); proceed.set()
    assert (await task)['status'] == 'cancelled'
    assert not service.list()['connections']


async def test_one_connection_failure_keeps_other_account(tmp_path):
    service = QuotaConnections(tmp_path, reader=reader)
    await service.save(draft(label='Good'))
    bad = (await service.save(draft(label='Bad')))['connection']['id']
    record = service.store.get(bad); record['secrets']['managementKey'] = 'bad'; service.store.save(record)
    result = await AccountQuotaService(SimpleNamespace(list_drivers=lambda: []), connections=service).snapshot()
    assert len(result['accounts']) == 1
    assert result['unavailable'] == [{'connectionId': bad, 'label': 'Bad', 'state': 'unauthenticated', 'reason': 'credential_expired',
                                      'recovery': {'provider': 'openrouter', 'config': {}, 'fixedConfig': {}, 'action': 'credentials', 'connectionId': bad}}]


@pytest.mark.parametrize('change', ['delete', 'edit'])
async def test_other_process_changes_during_renewal_do_not_break_other_sources(tmp_path, change):
    service = QuotaConnections(tmp_path, reader=reader)
    victim = (await service.save(draft(label='Changing')))['connection']['id']
    good = (await service.save(draft(label='Good')))['connection']['id']
    separate = QuotaConnections(tmp_path, reader=reader)
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = 0
    async def rotating(provider, config, secrets):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()
            secrets['managementKey'] = 'rotated'
        return await reader(provider, config, secrets)
    service.reader = rotating
    task = asyncio.create_task(service.read_accounts())
    await entered.wait()
    if change == 'delete':
        await separate.delete(victim)
    else:
        record = separate.store.get(victim)
        record['secrets']['managementKey'] = 'replacement'
        separate.store.save(record, record['version'])
    resume.set()
    result = await task
    assert [row[0] for row in result] == [good]
    if change == 'delete':
        with pytest.raises(ConnectionInvalid): separate.store.get(victim)
    else:
        assert separate.store.get(victim)['secrets']['managementKey'] == 'replacement'


@pytest.mark.parametrize('operation', ['save', 'test'])
async def test_concurrent_disconnect_does_not_mask_original_query_failure(tmp_path, operation):
    service = QuotaConnections(tmp_path, reader=reader)
    identifier = (await service.save(draft()))['connection']['id']
    separate = QuotaConnections(tmp_path, reader=reader)
    async def failing(provider, config, secrets):
        secrets['managementKey'] = 'rotated'
        await separate.delete(identifier)
        raise QuotaUnavailable(reason='quota_not_returned')
    service.reader = failing
    with pytest.raises(QuotaUnavailable) as error:
        await getattr(service, operation)({}, identifier)
    assert error.value.reason == 'quota_not_returned'
    assert separate.list() == {'connections': []}
