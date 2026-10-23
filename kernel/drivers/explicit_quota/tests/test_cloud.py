import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.explicit_quota.cloud import read_connection, tencent_headers, volc_headers

NOW = datetime(2026, 10, 9, 8, 30, 15, tzinfo=timezone.utc)
VOLC = {'accessKeyId': 'AKLTexample', 'secretAccessKey': 'c2VjcmV0LWV4YW1wbGU='}
TC = {'secretId': 'example-id', 'secretKey': 'example-secret'}


def run(provider, config, secrets, handler, **kwargs):
    return asyncio.run(read_connection(provider, config, secrets,
        transport=httpx.MockTransport(handler), now=lambda: NOW, **kwargs))


def coding(percent=0):
    return {'Result': {'QuotaUsage': [{'Level': 'session', 'Percent': percent, 'ResetTimestamp': 1791540000}]}}


def team(team_id='team-one', **extra):
    return {'TeamId': team_id, 'Name': '设计团队', 'Uin': '123456789', 'ProductType': 'enterprise',
        'PackageInfo': {'CycleQuota': '1000', 'TotalUsed': '250'},
        'TokenSummary': {'CycleStartTime': '2026-10-01T00:00:00+08:00', 'CycleEndTime': '2026-11-01T00:00:00+08:00'},
        **extra}


@pytest.mark.parametrize('region,action,signature', [
    ('cn-beijing', 'GetCodingPlanUsage', 'deb80c0cd8bd77768ca40f6d47c7204621c0994428f6ab77e23b76e15dc4c047'),
    ('cn-shanghai', 'GetAFPUsage', '8a241be0996c93de4dfcd9e8b1db37494bf63170804fcaebe8380ff7dca2bb23'),
])
def test_volc_sdk_signature_vectors(region, action, signature):
    # Public Volcengine SDK algorithm, fixed synthetic AK/SK and request bytes.
    query, headers = volc_headers(VOLC['accessKeyId'], VOLC['secretAccessKey'], region, action, b'{}', NOW)
    assert query == f'Action={action}&Version=2024-01-01'
    assert headers['Authorization'].endswith('Signature=' + signature)
    assert headers['X-Content-Sha256'] == '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
    assert headers['X-Date'] == '20261009T083015Z'


def test_tencent_official_tc3_example_vector():
    # Tencent's public api/213/30654 example, not an account credential.
    payload = b'{"Limit": 1, "Filters": [{"Values": ["\\u672a\\u547d\\u540d"], "Name": "instance-name"}]}'
    headers = tencent_headers('AKID' + '*' * 32, '*' * 32, 'ap-guangzhou', 'DescribeInstances', payload,
        datetime.fromtimestamp(1551113065, timezone.utc), host='cvm.tencentcloudapi.com', service='cvm')
    assert headers['Authorization'].endswith('Signature=10b1a37a7301a02ca19a647ad722d5e43b4b3cff309d421d85b46093f6ab6c4f')


def test_volc_posts_only_fixed_control_plane_and_preserves_zero():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'POST' and request.url.host == 'open.volcengineapi.com'
        assert request.content == b'{}'
        assert request.headers['authorization'].startswith('HMAC-SHA256 ')
        return httpx.Response(200, json=coding())
    result = run('volcengine', {'plan': 'coding', 'url': 'https://evil.invalid'}, VOLC, handler)
    window = result.accounts[0].windows[0]
    assert window.used_percent == 0 and window.resets_at == 1791540000
    assert window.remaining_amount is None and window.limit_amount is None
    assert len(requests) == 1 and not result.issues
    assert VOLC['accessKeyId'] not in repr(result)


def test_volc_agent_preserves_real_daily_window_and_millisecond_reset():
    result = run('volcengine', {'plan': 'agent'}, VOLC, lambda _: httpx.Response(200, json={'Result': {
        'PlanType': 'Max', 'AFPFiveHour': {'Quota': '200', 'Used': '30', 'ResetTime': 1791540000000},
        'AFPDaily': {'Quota': '400', 'Used': '0'}, 'AFPWeekly': {'Quota': '0', 'Used': '0'},
        'AFPMonthly': {'Percent': '50', 'ResetTimestamp': 1791540000}}}))
    account = result.accounts[0]
    assert account.plan == 'Agent Plan · Max'
    assert [w.id for w in account.windows] == ['AFPFiveHour', 'AFPDaily', 'AFPMonthly']
    assert account.windows[0].remaining_amount == 170
    assert account.windows[0].resets_at == 1791540000
    assert account.windows[1].used_percent == 0
    assert account.windows[2].limit_amount is None


def test_volc_both_plans_are_independent_and_keep_partial_success():
    def handler(request):
        if request.url.params['Action'] == 'GetCodingPlanUsage':
            return httpx.Response(200, json=coding(12))
        return httpx.Response(200, json={'ResponseMetadata': {'Error': {'Code': 'AccessDenied', 'Message': 'secret must not leak'}}})
    result = run('volcengine', {}, VOLC, handler)
    assert len(result.accounts) == len(result.issues) == 1
    assert result.accounts[0].plan == 'Coding Plan'
    assert result.issues[0].reason == 'permission_required'
    assert 'secret must' not in repr(result)


def test_volc_http400_retains_structured_signature_error():
    result = run('volcengine', {'plan': 'coding'}, VOLC, lambda _: httpx.Response(400,
        json={'ResponseMetadata': {'Error': {'Code': 'SignatureDoesNotMatch', 'Message': 'private'}}}))
    assert not result.accounts
    assert result.issues[0].state == 'unauthenticated'
    assert result.issues[0].reason == 'credential_unavailable'


@pytest.mark.parametrize('bad', [None, True, '', 'nan', 'inf', -1])
def test_volc_missing_or_invalid_percent_never_becomes_zero(bad):
    result = run('volcengine', {'plan': 'coding'}, VOLC, lambda _: httpx.Response(200, json=coding(bad)))
    assert not result.accounts and result.issues[0].reason == 'quota_not_returned'


@pytest.mark.parametrize('error,state,reason', [
    ('AuthFailure.SignatureFailure', 'unauthenticated', 'credential_unavailable'),
    ('AuthFailure.SecretIdNotFound', 'unauthenticated', 'credential_unavailable'),
    ('AuthFailure.TokenExpired', 'unauthenticated', 'credential_expired'),
    ('UnauthorizedOperation', 'unavailable', 'permission_required'),
    ('ResourceNotFound', 'unavailable', 'quota_not_returned'),
    ('InternalError', 'unavailable', 'request_failed'),
])
def test_cloud_business_errors_are_classified_and_redacted(error, state, reason):
    with pytest.raises(QuotaUnavailable) as caught:
        run('tencent', {}, TC, lambda _: httpx.Response(200, json={'Response': {'Error': {'Code': error, 'Message': TC['secretKey']}}}))
    assert (caught.value.state, caught.value.reason) == (state, reason)
    assert TC['secretKey'] not in str(caught.value)


def test_tencent_explicit_team_skips_enumeration_and_preserves_cycle():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url == httpx.URL('https://tokenhub.tencentcloudapi.com/')
        assert request.headers['X-TC-Action'] == 'DescribeTokenPlan'
        assert request.headers['X-TC-Region'] == 'ap-singapore'
        assert request.headers['X-TC-Version'] == '2026-03-22'
        assert json.loads(request.content) == {'TeamId': 'team-one'}
        return httpx.Response(200, json={'Response': team(ProductType='enterprise-auto')})
    result = run('tencent', {'teamId': 'team-one', 'region': 'ap-singapore'}, TC, handler)
    window = result.accounts[0].windows[0]
    assert window.unit == 'tokens' and window.used_percent == 25
    assert window.limit_amount == 1000 and window.remaining_amount == 750
    assert window.window_duration_mins == 31 * 24 * 60
    assert window.resets_at == 1793462400
    assert result.accounts[0].account_label == '腾讯 TokenHub · 设计团队'
    assert len(requests) == 1


def test_tencent_pagination_and_per_team_identity():
    offsets, details = [], []
    def handler(request):
        params = json.loads(request.content)
        if request.headers['X-TC-Action'] == 'DescribeTokenPlanList':
            offsets.append(params['Offset'])
            item = team('one' if params['Offset'] == 0 else 'two')
            return httpx.Response(200, json={'Response': {'TokenPlanSet': [item], 'TotalCount': 2}})
        details.append(params['TeamId'])
        return httpx.Response(200, json={'Response': team(params['TeamId'])})
    result = run('tencent', {}, TC, handler)
    assert offsets == [0, 1] and sorted(details) == ['one', 'two']
    assert len(result.accounts) == 2 and len({a.account_key for a in result.accounts}) == 2
    assert not result.issues


def test_tencent_deduplicates_list_pages_and_preserves_available_team():
    def handler(request):
        if request.headers['X-TC-Action'] == 'DescribeTokenPlanList':
            return httpx.Response(200, json={'Response': {'TokenPlanSet': [team()], 'TotalCount': 2}})
        return httpx.Response(200, json={'Response': team()})
    result = run('tencent', {}, TC, handler)
    assert len(result.accounts) == 1 and len(result.issues) == 1


def test_tencent_detail_failure_uses_list_values_without_inventing_reset():
    def handler(request):
        if request.headers['X-TC-Action'] == 'DescribeTokenPlanList':
            return httpx.Response(200, json={'Response': {'TokenPlanSet': [team(TokenSummary=None)], 'TotalCount': 1}})
        return httpx.Response(503)
    result = run('tencent', {}, TC, handler)
    window = result.accounts[0].windows[0]
    assert window.used_percent == 25 and window.resets_at is None and window.window_duration_mins is None


def test_tencent_does_not_merge_response_from_another_team():
    result = run('tencent', {'teamId': 'one'}, TC, lambda _: httpx.Response(200, json={'Response': team('another')}))
    assert not result.accounts and result.issues[0].reason == 'quota_not_returned'


@pytest.mark.parametrize('package', [None, {}, {'TotalQuota': 100}, {'TotalQuota': 0, 'TotalUsed': 0},
    {'TotalQuota': 100, 'TotalUsed': None}, {'TotalQuota': True, 'TotalUsed': 0}, {'TotalQuota': 100, 'TotalUsed': -2}])
def test_tencent_incomplete_quota_is_not_zero(package):
    result = run('tencent', {'teamId': 'team-one'}, TC, lambda _: httpx.Response(200, json={'Response': team(PackageInfo=package)}))
    assert not result.accounts and result.issues[0].reason == 'quota_not_returned'


def test_tencent_zero_usage_and_stopped_plan_are_provider_facts():
    result = run('tencent', {'teamId': 'team-one'}, TC, lambda _: httpx.Response(200, json={'Response': team(
        PackageInfo={'TotalQuota': '1000', 'TotalUsed': '0'}, Status='disable', StopReason='FROZEN')}))
    assert result.accounts[0].windows[0].used_percent == 0
    assert result.accounts[0].plan.endswith('已冻结')


def test_tencent_separate_keys_for_same_provider_issued_team_share_identity():
    handler = lambda _: httpx.Response(200, json={'Response': team()})
    one = run('tencent', {'teamId': 'team-one'}, TC, handler)
    two = run('tencent', {'teamId': 'team-one'}, {**TC, 'secretId': 'different-key'}, handler)
    assert one.accounts[0].account_key == two.accounts[0].account_key


def test_tencent_detail_permission_failure_preserves_reason_without_list_values():
    result = run('tencent', {'teamId': 'team-one'}, TC, lambda _: httpx.Response(403))
    assert result.issues[0].reason == 'permission_required'


@pytest.mark.parametrize('status,state,reason', [(401, 'unauthenticated', 'credential_expired'),
    (403, 'unavailable', 'permission_required'), (429, 'unavailable', 'request_failed'),
    (500, 'unavailable', 'request_failed'), (302, 'unavailable', 'request_failed')])
def test_cloud_http_errors_and_redirects(status, state, reason):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://evil.invalid/'})
    result = run('volcengine', {'plan': 'coding'}, VOLC, handler)
    assert len(calls) == 1 and not result.accounts
    assert (result.issues[0].state, result.issues[0].reason) == (state, reason)


@pytest.mark.parametrize('body', [b'{}', b'{invalid json', b'[]', b'x' * 1000001])
def test_cloud_malformed_or_oversized_response(body):
    result = run('volcengine', {'plan': 'coding'}, VOLC, lambda _: httpx.Response(200, content=body))
    assert not result.accounts and result.issues[0].reason == 'quota_not_returned'


@pytest.mark.parametrize('provider,config,secrets', [
    ('volcengine', {}, {}), ('tencent', {}, {}),
    ('volcengine', {}, {**VOLC, 'accessKeyId': 'bad\nheader'}),
    ('volcengine', {'region': 'cn-beijing\r\nHost:evil'}, VOLC),
    ('tencent', {'teamId': '../bad'}, TC), ('tencent', {'region': 'invalid'}, TC),
])
def test_cloud_invalid_inputs_never_send_credentials(provider, config, secrets):
    def handler(_):
        pytest.fail('unexpected request')
    with pytest.raises(QuotaUnavailable):
        run(provider, config, secrets, handler)


def test_total_deadline_cancels_pending_and_keeps_completed_plan():
    cancelled = []
    async def handler(request):
        if request.url.params['Action'] == 'GetCodingPlanUsage':
            return httpx.Response(200, json=coding(10))
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    result = run('volcengine', {}, VOLC, handler, total_timeout=0.03)
    assert len(result.accounts) == len(result.issues) == 1 and cancelled == [True]


def test_outer_cancellation_leaves_no_requests_alive():
    async def check():
        started, cancelled = asyncio.Event(), []
        async def handler(_):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        task = asyncio.create_task(read_connection('volcengine', {'plan': 'coding'}, VOLC,
            transport=httpx.MockTransport(handler)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True]
    asyncio.run(check())
