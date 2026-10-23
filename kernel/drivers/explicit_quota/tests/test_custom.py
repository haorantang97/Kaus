import httpx
import pytest
from drivers.account_quota import QuotaUnavailable
from drivers.explicit_quota.custom import read_connection
from drivers.explicit_quota.catalog import ConnectionInvalid, catalog, validate


def config(**extra):
    return dict(url='https://billing.example.com/balance', remainingPath='data.quota', currency='CNY', authScheme='bearer', multiplier='0.000002') | extra


async def test_explicit_endpoint_units_auth_and_json_path():
    async def handle(request):
        assert str(request.url) == 'https://billing.example.com/balance'
        assert request.headers['Authorization'] == 'Bearer synthetic'
        return httpx.Response(200, json={'data': {'quota': 5000000}})
    result = await read_connection('custom', config(), {'apiKey': 'synthetic'}, transport=httpx.MockTransport(handle))
    assert result.balances[0].remaining == 10 and result.balances[0].currency == 'CNY'


@pytest.mark.parametrize('payload', [{}, {'data': {'quota': None}}, {'data': {'quota': True}}, {'data': {'quota': 'nan'}}])
async def test_missing_amount_is_not_zero(payload):
    with pytest.raises(QuotaUnavailable, match='unavailable'):
        await read_connection('custom', config(), {'apiKey': 'synthetic'}, transport=httpx.MockTransport(lambda r: httpx.Response(200,json=payload)))


@pytest.mark.parametrize('url', ['http://example.com/balance', 'https://user:secret@example.com/balance', 'https://example.com/balance?key=secret'])
async def test_endpoint_refuses_embedded_secret_or_cleartext(url):
    with pytest.raises(ConnectionInvalid):
        await read_connection('custom', config(url=url), {'apiKey': 'synthetic'})


async def test_private_destination_and_redirect_are_rejected():
    with pytest.raises(QuotaUnavailable) as error:
        await read_connection('custom', config(url='https://127.0.0.1/balance'), {'apiKey': 'synthetic'})
    assert error.value.reason == 'invalid_endpoint'
    calls=[]
    def redirect(request): calls.append(request); return httpx.Response(302,headers={'Location':'https://another.example/balance'})
    with pytest.raises(QuotaUnavailable):
        await read_connection('custom',config(),{'apiKey':'synthetic'},transport=httpx.MockTransport(redirect))
    assert len(calls) == 1


@pytest.mark.parametrize('change', [dict(remainingPath='data.gift'), dict(currency='USD'),
    dict(multiplier='0.000004'), dict(userId='2'), dict(method='POST'), dict(authScheme='raw')])
async def test_distinct_query_scopes_are_not_merged(change):
    handler = httpx.MockTransport(lambda _: httpx.Response(200, json={'data': {'quota': 500000, 'gift': 500000}}))
    first = await read_connection('custom', config(), {'apiKey': 'synthetic'}, transport=handler)
    other = await read_connection('custom', config(**change), {'apiKey': 'synthetic'}, transport=handler)
    assert first.account_key != other.account_key


async def test_normalized_equivalent_config_and_same_key_share_identity():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={'data': {'quota': 1}}))
    original = config(userId='0012', multiplier='1.0000', url='https://BILLING.example.com:443/balance')
    equivalent = dict(reversed(list(config(userId='12', multiplier='1', method='GET').items())))
    one = await read_connection('custom', original, {'apiKey': ' synthetic '}, transport=transport)
    two = await read_connection('custom', equivalent, {'apiKey': 'synthetic'}, transport=transport)
    assert one.account_key == two.account_key


async def test_different_credentials_never_share_identity():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={'data': {'quota': 1}}))
    one = await read_connection('custom', config(), {'apiKey': 'account-one'}, transport=transport)
    two = await read_connection('custom', config(), {'apiKey': 'account-two'}, transport=transport)
    assert one.account_key != two.account_key


async def test_http200_business_refusal_never_displays_balance_or_message():
    transport = httpx.MockTransport(lambda _: httpx.Response(200,
        json={'success': False, 'message': 'private diagnostic', 'data': {'quota': 500000}}))
    with pytest.raises(QuotaUnavailable) as error:
        await read_connection('custom', config(), {'apiKey': 'synthetic'}, transport=transport)
    assert error.value.reason == 'request_failed'
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_explicit_new_api_user_and_query_method(method):
    def handler(request):
        assert request.method == method
        assert request.headers['New-Api-User'] == '12345'
        assert request.headers['Authorization'] == 'access-token'
        assert request.content == b''
        return httpx.Response(200, json={'success': True, 'data': {'quota': 500000}})
    result = await read_connection('custom', config(method=method, userId='12345', authScheme='raw', currency='USD'),
        {'apiKey': 'access-token'}, transport=httpx.MockTransport(handler))
    assert result.balances[0].remaining == 1 and result.balances[0].currency == 'USD'


async def test_default_query_is_get_without_user_header():
    def handler(request):
        assert request.method == 'GET' and 'New-Api-User' not in request.headers
        return httpx.Response(200, json={'data': {'quota': 0}})
    result = await read_connection('custom', config(), {'apiKey': 'synthetic'}, transport=httpx.MockTransport(handler))
    assert result.balances[0].remaining == 0


@pytest.mark.parametrize('user_id', ['-1', '1.2', 'abc', '１２', '1\r\nAuthorization:test', '1' * 21])
async def test_invalid_user_id_cannot_be_sent(user_id):
    def handler(_): pytest.fail('unexpected request')
    with pytest.raises(ConnectionInvalid):
        await read_connection('custom', config(userId=user_id), {'apiKey': 'synthetic'}, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('method', ['PUT', 'DELETE', 'PATCH', 'post', 'POST\nGET'])
async def test_only_explicit_get_and_post_are_allowed(method):
    def handler(_): pytest.fail('unexpected request')
    with pytest.raises(ConnectionInvalid):
        await read_connection('custom', config(method=method), {'apiKey': 'synthetic'}, transport=httpx.MockTransport(handler))


def test_catalog_groups_advanced_fields_and_links_manage_key_settings():
    providers = {p['id']: p for p in catalog()['providers']}
    fields = {field['key']: field for field in providers['custom']['fields']}
    assert all(fields[key]['advanced'] for key in ('multiplier', 'authScheme', 'userId', 'method'))
    assert not fields['url'].get('advanced') and not fields['remainingPath'].get('advanced')
    assert providers['aihubmix']['docsUrl'] == 'https://console.aihubmix.com/setting'
    clean = validate('custom', config(), {'apiKey': 'synthetic'})
    assert clean['method'] == 'GET' and clean['userId'] == ''
