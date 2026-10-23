"""Quota recovery belongs to an existing source, never an inferred vendor account."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app.account_quotas import AccountQuotaService
from app.quota_connections import QuotaConnections
from drivers.account_quota import AccountQuota, QuotaBalance, QuotaIssue, QuotaReadResult, QuotaUnavailable
from drivers.explicit_quota.catalog import ConnectionInvalid


def missing(source='source-a', provider='openrouter', config=None, **extra):
    return QuotaIssue('unauthenticated', 'OpenRouter', 'management_key_required', source_id=source,
                      setup_provider=provider, setup_config=config or {}, **extra)


def quota(key='native-key-a', source='source-a'):
    return AccountQuota('OpenRouter', 'OpenRouter', (), account_key=key, source_id=source,
                        balances=(QuotaBalance('credits', '余额', 88.5, 'USD'),), kind='balance')


async def explicit_reader(provider, config, secrets):
    if secrets.get('managementKey') == 'expired':
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    return replace(quota(key='management-key-account', source=None), provider=provider)


def setup(tmp_path, result):
    connections = QuotaConnections(tmp_path, reader=explicit_reader)
    drivers = [SimpleNamespace(backend_id='backend:first', driver_kind='fixture', product_id='codex')]
    state = [result]
    async def read(): return state[0]
    service = AccountQuotaService(SimpleNamespace(list_drivers=lambda: drivers), reader_factory=lambda _: read,
                                  connections=connections, ttl=0, refresh_interval=0)
    return connections, service, state


def draft(source='source-a', key='management-synthetic'):
    return dict(sourceId=source, provider='openrouter', config={}, secrets={'managementKey': key})


async def test_automatic_quota_has_no_setup_or_connection(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult((quota(),)))
    result = await service.snapshot()
    assert len(result['accounts']) == 1 and not result['unavailable']
    assert not result['accounts'][0].get('connectionIds')
    assert not connections.list()['connections']


async def test_supplement_replaces_only_selected_source_preserves_verified_account_identity(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult((quota('other-native-key', 'source-b'),), (missing(),)))
    initial = await service.snapshot()
    recovery = initial['unavailable'][0]['recovery']
    assert recovery == {'sourceId': 'source-a', 'provider': 'openrouter', 'config': {}, 'fixedConfig': {}, 'action': 'credentials'}
    saved = (await connections.save(draft()))['connection']
    result = await service.snapshot()
    assert len(result['accounts']) == 2 and not result['unavailable']
    supplemented = next(a for a in result['accounts'] if a.get('connectionIds'))
    assert supplemented['connectionIds'] == [saved['id']]
    assert supplemented['agentIds'] == ['backend:first']
    assert supplemented['id'] != initial['accounts'][0]['id']
    await connections.delete(saved['id'])
    again = await service.snapshot()
    assert len(again['accounts']) == 1 and len(again['unavailable']) == 1


async def test_same_vendor_sources_each_have_own_recovery_and_success_suppresses_it(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing('a'), missing('b'))))
    before = await service.snapshot()
    assert {r['sourceId'] for r in before['unavailable']} == {'a', 'b'}
    await connections.save(draft('a'))
    after = await service.snapshot()
    assert [r['sourceId'] for r in after['unavailable']] == ['b']


async def test_repeated_and_concurrent_supplement_saves_do_not_duplicate(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(),)))
    await service.snapshot()
    results = await asyncio.gather(connections.save(draft()), connections.save(draft()))
    assert results[0]['connection']['id'] == results[1]['connection']['id']
    assert len(connections.list()['connections']) == 1


async def test_binding_is_validated_and_fixed_site_cannot_be_changed(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(provider='stepfun-plan', config={'site': 'ai'}),)))
    await service.snapshot()
    for body in [draft('unknown'), draft(), dict(sourceId='source-a', provider='stepfun-plan', config={'site': 'com'})]:
        with pytest.raises(ConnectionInvalid): connections.begin_login(body)
    ticket = connections.begin_login({'sourceId': 'source-a', 'provider': 'stepfun-plan', 'config': {'site': 'ai'}})['loginId']
    assert connections.claim_login(ticket)['config']['site'] == 'ai'
    await connections.complete_login(ticket, {'secrets': {'access': 'synthetic-access', 'refresh': 'synthetic-refresh'}})
    row = connections.list()['connections'][0]
    assert row['sourceId'] == 'source-a'
    with pytest.raises(ConnectionInvalid):
        await connections.save({'config': {'site': 'com'}}, row['id'])


async def test_native_credential_change_does_not_inherit_old_supplement(tmp_path):
    connections, service, state = setup(tmp_path, QuotaReadResult(issues=(missing(),)))
    await service.snapshot()
    saved = (await connections.save(draft()))['connection']
    state[0] = QuotaReadResult(issues=(missing('new-key-source'),))
    changed = await service.snapshot()
    assert changed['unavailable'][0]['sourceId'] == 'new-key-source'
    assert 'connectionId' not in changed['unavailable'][0]['recovery']
    assert changed['accounts'][0]['connectionIds'] == [saved['id']]
    assert changed['accounts'][0]['agentIds'] == []
    with pytest.raises(ConnectionInvalid, match='source_changed'):
        await connections.save(draft())


async def test_expired_supplement_edits_same_connection_instead_of_reconnecting(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(),)))
    await service.snapshot()
    saved = (await connections.save(draft()))['connection']
    async def expired(*args): raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    connections.reader = expired
    result = await service.snapshot()
    assert len(result['unavailable']) == 1
    assert result['unavailable'][0]['recovery']['connectionId'] == saved['id']
    assert result['unavailable'][0]['sourceId'] == 'source-a'


async def test_later_native_success_uses_automatic_quota_without_duplicate_but_keeps_management(tmp_path):
    connections, service, state = setup(tmp_path, QuotaReadResult(issues=(missing(),)))
    await service.snapshot()
    saved = (await connections.save(draft()))['connection']
    state[0] = QuotaReadResult((quota(),))
    result = await service.snapshot()
    assert len(result['accounts']) == 1 and not result['unavailable']
    assert result['accounts'][0]['connectionIds'] == [saved['id']]


async def test_transient_failure_does_not_offer_credential_setup(tmp_path):
    _, service, _ = setup(tmp_path, QuotaReadResult(issues=(QuotaIssue('unavailable', 'OpenRouter', 'request_failed', source_id='a'),)))
    result = await service.snapshot()
    assert 'recovery' not in result['unavailable'][0]


async def test_shared_source_across_agents_has_single_recovery_and_all_agent_links(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(),)))
    service.registry.list_drivers = lambda: [SimpleNamespace(backend_id=x, driver_kind='fixture', product_id='codex') for x in ('a', 'b')]
    assert len((await service.snapshot())['unavailable']) == 1
    await connections.save(draft())
    result = await service.snapshot()
    assert len(result['accounts']) == 1
    assert result['accounts'][0]['agentIds'] == ['a', 'b']


async def test_existing_custom_key_reused_only_for_same_origin_never_serialized(tmp_path):
    secret = 'synthetic-native-api-key'
    issue = missing(provider='custom', setup_secrets={'apiKey': secret}, setup_origin='https://quota.example')
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(issue,)))
    wire = await service.snapshot()
    assert secret not in json.dumps(wire)
    assert wire['unavailable'][0]['recovery']['hasExistingCredential'] is True
    calls = []
    async def capture(provider, config, secrets):
        calls.append(dict(secrets))
        return await explicit_reader(provider, config, secrets)
    connections.reader = capture
    body = {'sourceId': 'source-a', 'provider': 'custom', 'config': {'url': 'https://other.example/balance'}, 'secrets': {}}
    with pytest.raises(ConnectionInvalid, match='credential_required'): await connections.save(body)
    assert not calls
    body['config']['url'] = 'https://quota.example/balance'
    saved = (await connections.save(body))['connection']
    assert calls == [{'apiKey': secret}] and secret not in json.dumps(saved)
    with pytest.raises(ConnectionInvalid, match='credential_required'):
        await connections.save({'config': {'url': 'https://other.example/balance'}}, saved['id'])
    await connections.save({'config': {'url': 'https://other.example/balance'}, 'secrets': {'apiKey': 'explicit-other-key'}}, saved['id'])
    assert calls[-1] == {'apiKey': 'explicit-other-key'}


async def test_waiting_login_cannot_overwrite_edit_or_changed_source(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(provider='stepfun-plan', config={'site': 'ai'}),)))
    await service.snapshot()
    body = {'sourceId': 'source-a', 'provider': 'stepfun-plan', 'config': {'site': 'ai'}}
    ticket = connections.begin_login(body)['loginId']
    connections.claim_login(ticket)
    await connections.complete_login(ticket, {'secrets': {'access': 'a', 'refresh': 'r'}})
    row = connections.list()['connections'][0]
    ticket = connections.begin_login(body | {'connectionId': row['id']})['loginId']
    connections.claim_login(ticket)
    await connections.save({'label': 'Edited while signing in'}, row['id'])
    result = await connections.complete_login(ticket, {'secrets': {'access': 'old-login', 'refresh': 'old-refresh'}})
    assert result == {'status': 'failed', 'reason': 'connection_changed'}
    assert connections.store.get(row['id'])['secrets']['access'] == 'a'


async def test_legacy_connection_stays_manageable_without_separate_list(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult())
    saved = (await connections.save({k: v for k, v in draft().items() if k != 'sourceId'}))['connection']
    async def expired(*args): raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    connections.reader = expired
    wire = await service.snapshot()
    assert wire['unavailable'][0]['recovery']['connectionId'] == saved['id']
    assert 'sourceId' not in wire['unavailable'][0]['recovery']


async def test_two_waiting_logins_cannot_replace_each_others_saved_credentials(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult(issues=(missing(provider='stepfun-plan', config={'site': 'ai'}),)))
    await service.snapshot()
    body = {'sourceId': 'source-a', 'provider': 'stepfun-plan', 'config': {'site': 'ai'}}
    first, second = [connections.begin_login(body)['loginId'] for _ in range(2)]
    connections.claim_login(first)
    connections.claim_login(second)
    assert (await connections.complete_login(first, {'secrets': {'access': 'first-access', 'refresh': 'r'}}))['status'] == 'connected'
    assert (await connections.complete_login(second, {'secrets': {'access': 'late-access', 'refresh': 'r'}})) == {
        'status': 'failed', 'reason': 'connection_changed'}
    assert len(connections.list()['connections']) == 1
    assert connections.store.all()[0]['secrets']['access'] == 'first-access'


async def test_snapshot_discards_account_read_before_a_concurrent_credential_edit(tmp_path):
    connections, service, _ = setup(tmp_path, QuotaReadResult())
    saved = (await connections.save({k: v for k, v in draft().items() if k != 'sourceId'}))['connection']
    native_started, release_native = asyncio.Event(), asyncio.Event()
    async def slow_native():
        native_started.set()
        await release_native.wait()
        return QuotaReadResult()
    service.reader_factory = lambda _: slow_native
    explicit_finished = asyncio.Event()
    original_read = connections.read_accounts
    async def read_with_marker(**kwargs):
        result = await original_read(**kwargs)
        explicit_finished.set()
        return result
    connections.read_accounts = read_with_marker
    pending = asyncio.create_task(service.snapshot())
    await native_started.wait()
    await explicit_finished.wait()
    await connections.save({'secrets': {'managementKey': 'new-account-key'}}, saved['id'])
    release_native.set()
    result = await pending
    assert not result['accounts']
    assert result['unavailable'][0]['recovery']['connectionId'] == saved['id']
    assert len((await service.snapshot())['accounts']) == 1
