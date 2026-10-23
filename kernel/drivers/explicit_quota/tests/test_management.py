import asyncio

import httpx
import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.explicit_quota.management import read_connection
from drivers.provider_quota import ProviderSource, read_sources


def run(provider, secrets, handler, **kwargs):
    return asyncio.run(read_connection(provider, {}, secrets, transport=httpx.MockTransport(handler), **kwargs))


def test_aihubmix_official_manage_key_protocol_and_usd_conversion():
    def handler(request):
        assert request.url == httpx.URL('https://aihubmix.com/api/user/self')
        assert request.method == 'GET'
        assert request.headers['Authorization'] == 'fd-synthetic'
        return httpx.Response(200, json={'success': True, 'data': {'id': 42, 'quota': 1500000,
            'used_quota': 999999999, 'email': 'reader@example.test'}})
    account = run('aihubmix', {'accessToken': 'fd-synthetic'}, handler)
    assert account.balances[0].remaining == 3 and account.balances[0].currency == 'USD'
    assert not account.windows and account.kind == 'balance'
    assert account.account_label == 'AiHubMix · r***@example.test'
    assert 'fd-synthetic' not in repr(account)


@pytest.mark.parametrize('amount', [0, -500000])
def test_aihubmix_zero_and_debt_are_not_missing(amount):
    account = run('aihubmix', {'accessToken': 'fd-synthetic'}, lambda _: httpx.Response(200,
        json={'success': True, 'data': {'quota': amount}}))
    assert account.balances[0].remaining == amount / 500000


@pytest.mark.parametrize('payload', [{}, {'success': False, 'data': {'quota': 1}},
    {'success': True, 'data': {}}, {'success': True, 'data': {'quota': None}},
    {'success': True, 'data': {'quota': True}}, {'success': True, 'data': {'quota': 'nan'}}])
def test_aihubmix_unknown_fields_never_become_zero(payload):
    with pytest.raises(QuotaUnavailable) as caught:
        run('aihubmix', {'accessToken': 'fd-synthetic'}, lambda _: httpx.Response(200, json=payload))
    assert caught.value.reason == 'quota_not_returned'


def test_aihubmix_provider_account_deduplicates_rotated_manage_keys():
    handler = lambda _: httpx.Response(200, json={'success': True, 'data': {'id': 123, 'quota': 1}})
    first = run('aihubmix', {'accessToken': 'old'}, handler)
    second = run('aihubmix', {'accessToken': 'new'}, handler)
    assert first.account_key == second.account_key


def test_openrouter_balance_and_existing_management_key_share_identity():
    def handler(request):
        assert request.url == httpx.URL('https://openrouter.ai/api/v1/credits')
        assert request.headers['Authorization'] == 'Bearer management-test'
        return httpx.Response(200, json={'data': {'total_credits': 20, 'total_usage': 3.5}})
    account = run('openrouter', {'managementKey': 'management-test'}, handler)
    existing = asyncio.run(read_sources([ProviderSource('https://openrouter.ai/api/v1', 'management-test')],
        transport=httpx.MockTransport(handler)))
    assert account.account_key == existing.accounts[0].account_key
    assert account.balances == existing.accounts[0].balances
    assert account.balances[0].remaining == 16.5


def test_openrouter_management_403_does_not_fall_back_to_another_budget():
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(403, json={'error': {'message': 'sensitive diagnostics'}})
    with pytest.raises(QuotaUnavailable) as caught:
        run('openrouter', {'managementKey': 'test'}, handler)
    assert urls == ['https://openrouter.ai/api/v1/credits']
    assert caught.value.reason == 'management_key_required'
    assert 'sensitive' not in str(caught.value)


@pytest.mark.parametrize('data', [{}, {'total_credits': 10}, {'total_usage': 1}, {'total_credits': True, 'total_usage': 0}])
def test_openrouter_partial_balance_is_not_zero(data):
    with pytest.raises(QuotaUnavailable):
        run('openrouter', {'managementKey': 'test'}, lambda _: httpx.Response(200, json={'data': data}))


@pytest.mark.parametrize('provider,key', [('aihubmix', 'accessToken'), ('openrouter', 'managementKey')])
@pytest.mark.parametrize('value', ['', None, 'token\r\nInjected:value', 'x' * 16385, '不合法'])
def test_invalid_management_credentials_do_not_leave_host(provider, key, value):
    def handler(_):
        pytest.fail('unexpected network request')
    with pytest.raises(QuotaUnavailable):
        run(provider, {key: value}, handler)


@pytest.mark.parametrize('status,reason', [(401, 'credential_expired'), (403, 'permission_required'),
    (302, 'request_failed'), (429, 'request_failed'), (500, 'request_failed')])
def test_management_http_errors_have_no_raw_diagnostics(status, reason):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://another.invalid'}, content=b'private')
    with pytest.raises(QuotaUnavailable) as caught:
        run('aihubmix', {'accessToken': 'test'}, handler)
    assert caught.value.reason == reason and len(calls) == 1
    assert 'private' not in str(caught.value)


def test_management_timeout_cancels_request():
    cancelled = []
    async def handler(_):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    with pytest.raises(QuotaUnavailable) as caught:
        run('aihubmix', {'accessToken': 'test'}, handler, total_timeout=0.02)
    assert caught.value.reason == 'request_failed' and cancelled == [True]


def test_management_outer_cancel_propagates_and_stops_request():
    async def check():
        started, stopped = asyncio.Event(), []
        async def handler(_):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                stopped.append(True)
                raise
        task = asyncio.create_task(read_connection('openrouter', {}, {'managementKey': 'test'},
            transport=httpx.MockTransport(handler)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [True]
    asyncio.run(check())


@pytest.mark.parametrize('content', [b'invalid', b'[]', b'x' * 1000001])
def test_management_rejects_malformed_and_oversized_responses(content):
    with pytest.raises(QuotaUnavailable) as caught:
        run('aihubmix', {'accessToken': 'test'}, lambda _: httpx.Response(200, content=content))
    assert caught.value.reason == 'quota_not_returned'
