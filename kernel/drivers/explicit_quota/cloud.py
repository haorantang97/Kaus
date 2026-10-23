"""Read-only Ark and TokenHub billing queries with vendor request signatures.

Protocols: Volcengine SignerV4, Tencent Cloud TC3 (api/213/30654), and
TokenHub DescribeTokenPlan{List} (api/1823/132269, 132270). No SDK, login
store, inference endpoint or inherited proxy participates in these requests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from urllib.parse import urlencode, quote

import httpx

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.native_quota import account_key, finite, reset_at

VOLC_HOST = 'open.volcengineapi.com'
TC_HOST = 'tokenhub.tencentcloudapi.com'
TC_CONTENT_TYPE = 'application/json; charset=utf-8'
RESPONSE_LIMIT = 1_000_000


def _secret(secrets, name, *, reason='cloud_credentials_required'):
    value = secrets.get(name) if isinstance(secrets, dict) else None
    if (not isinstance(value, str) or not value.strip() or len(value) > 16384
            or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise QuotaUnavailable('unauthenticated', reason=reason)
    return value


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _mac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def volc_headers(access_key, secret_key, region, action, body, now):
    """Sign exactly the bytes subsequently sent to the Ark control plane."""
    stamp = now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    query = urlencode(sorted({'Action': action, 'Version': '2024-01-01'}.items()), quote_via=quote)
    signed = 'content-type;host;x-content-sha256;x-date'
    digest = _sha(body)
    canonical = '\n'.join(('POST', '/', query,
        f'content-type:application/json\nhost:{VOLC_HOST}\nx-content-sha256:{digest}\nx-date:{stamp}\n', signed, digest))
    scope = f'{stamp[:8]}/{region}/ark/request'
    key = _mac(secret_key.encode(), stamp[:8])
    for part in (region, 'ark', 'request'):
        key = _mac(key, part)
    signature = _mac(key, f'HMAC-SHA256\n{stamp}\n{scope}\n{_sha(canonical.encode())}').hex()
    return query, {'Content-Type': 'application/json', 'Host': VOLC_HOST,
        'X-Date': stamp, 'X-Content-Sha256': digest, 'Accept': 'application/json',
        'Authorization': f'HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'}


def tencent_headers(secret_id, secret_key, region, action, body, now, *, host=TC_HOST, service='tokenhub'):
    """TC3's public signing procedure; host/service parameters support SDK vectors."""
    stamp = str(int(now.timestamp()))
    date = now.astimezone(timezone.utc).strftime('%Y-%m-%d')
    signed = 'content-type;host;x-tc-action'
    canonical = '\n'.join(('POST', '/', '',
        f'content-type:{TC_CONTENT_TYPE}\nhost:{host}\nx-tc-action:{action.lower()}\n', signed, _sha(body)))
    scope = f'{date}/{service}/tc3_request'
    key = _mac(('TC3' + secret_key).encode(), date)
    for part in (service, 'tc3_request'):
        key = _mac(key, part)
    signature = _mac(key, f'TC3-HMAC-SHA256\n{stamp}\n{scope}\n{_sha(canonical.encode())}').hex()
    return {'Content-Type': TC_CONTENT_TYPE, 'Host': host, 'Accept': 'application/json',
        'X-TC-Action': action, 'X-TC-Version': '2026-03-22', 'X-TC-Region': region,
        'X-TC-Timestamp': stamp,
        'Authorization': f'TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed}, Signature={signature}'}


async def _request_json(client, method, url, *, headers, body=None, management=False, cloud_errors=False):
    try:
        async with client.stream(method, url, headers=headers, content=body) as response:
            if response.status_code == 401:
                raise QuotaUnavailable('unauthenticated', reason='credential_expired')
            if response.status_code == 403:
                raise QuotaUnavailable(reason='management_key_required' if management else 'permission_required')
            if response.status_code != 200 and not (cloud_errors and response.status_code == 400):
                raise QuotaUnavailable(reason='request_failed')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > RESPONSE_LIMIT:
                    raise QuotaUnavailable(reason='quota_not_returned')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise QuotaUnavailable(reason='quota_not_returned')
        if response.status_code != 200:
            _business_error(_object(value.get('ResponseMetadata')).get('Error')
                            or _object(value.get('Response')).get('Error'))
            raise QuotaUnavailable(reason='request_failed')
        return value
    except (httpx.HTTPError, UnicodeError):
        raise QuotaUnavailable(reason='request_failed') from None
    except ValueError:
        raise QuotaUnavailable(reason='quota_not_returned') from None


def _object(value):
    return value if isinstance(value, dict) else {}


def _business_error(value):
    if not isinstance(value, dict) or not value:
        return
    code = str(value.get('Code', '')).lower()
    # The provider's Message can contain request data; never surface it.
    if any(part in code for part in ('unauthorized', 'accessdenied', 'forbidden', 'permission')):
        raise QuotaUnavailable(reason='permission_required')
    if 'expired' in code:
        raise QuotaUnavailable('unauthenticated', reason='credential_expired')
    if any(part in code for part in ('authfailure', 'signature', 'accesskey', 'credential', 'invalidtoken')):
        raise QuotaUnavailable('unauthenticated', reason='credential_unavailable')
    if any(part in code for part in ('notfound', 'notsubscribed', 'notpurchased')):
        raise QuotaUnavailable(reason='quota_not_returned')
    raise QuotaUnavailable(reason='request_failed')


def _percent(value):
    value = finite(value)
    return min(100, value) if value is not None and value >= 0 else None


def _volc_account(result, plan, region, access_key):
    windows = []
    if plan == 'coding':
        rows = result.get('QuotaUsage')
        known = {'session': ('5 小时', 300), 'weekly': ('每周', 10080), 'monthly': ('每月', None)}
        for row in rows if isinstance(rows, list) else ():
            row = _object(row)
            level = row.get('Level')
            percent = _percent(row.get('Percent'))
            if level in known and percent is not None:
                label, span = known[level]
                windows.append(QuotaWindow(level, label, percent, span, reset_at(row.get('ResetTimestamp'))))
        title = 'Coding Plan'
    else:
        for key, label, span in (('AFPFiveHour', '5 小时', 300), ('AFPDaily', '每日', 1440),
                                  ('AFPWeekly', '每周', 10080), ('AFPMonthly', '每月', None)):
            row = _object(result.get(key))
            limit = finite(row.get('Quota', row.get('Total')))
            used = finite(row.get('Used'))
            percent = _percent(row.get('Percent'))
            valid_amounts = limit is not None and limit > 0 and used is not None and used >= 0
            if percent is None and valid_amounts:
                percent = min(100, used / limit * 100)
            if percent is None or (limit is not None and limit <= 0):
                continue
            windows.append(QuotaWindow(key, label, percent, span,
                reset_at(row.get('ResetTime', row.get('ResetTimestamp'))),
                max(0, limit - used) if valid_amounts else None,
                limit if valid_amounts else None, 'AFP' if valid_amounts else None))
        title = 'Agent Plan'
        tier = result.get('PlanType', result.get('Tier'))
        if tier in ('Small', 'Medium', 'Large', 'Max', 'Lite', 'Pro'):
            title += f' · {tier}'
    if not windows:
        raise QuotaUnavailable(reason='quota_not_returned')
    return AccountQuota('火山方舟', '火山方舟', tuple(windows), title,
        'cloud-plan:' + account_key(VOLC_HOST, f'{access_key}\0{region}\0{plan}'), kind='plan')


async def _volc_one(client, region, plan, access_key, secret_key, now):
    action = 'GetCodingPlanUsage' if plan == 'coding' else 'GetAFPUsage'
    body = b'{}'
    query, headers = volc_headers(access_key, secret_key, region, action, body, now())
    value = await _request_json(client, 'POST', f'https://{VOLC_HOST}/?{query}', headers=headers, body=body, cloud_errors=True)
    _business_error(_object(value.get('ResponseMetadata')).get('Error'))
    return _volc_account(_object(value.get('Result')), plan, region, access_key)


async def _tencent_call(client, region, action, params, secret_id, secret_key, now):
    body = json.dumps(params, ensure_ascii=False, separators=(',', ':')).encode()
    headers = tencent_headers(secret_id, secret_key, region, action, body, now())
    value = await _request_json(client, 'POST', f'https://{TC_HOST}/', headers=headers, body=body, cloud_errors=True)
    result = _object(value.get('Response'))
    _business_error(result.get('Error'))
    return result


def _tencent_account(plan, region, secret_id):
    package = _object(plan.get('PackageInfo'))
    limit = finite(package.get('CycleQuota'))
    if limit is None or limit <= 0:
        limit = finite(package.get('TotalQuota'))
    used = finite(package.get('TotalUsed'))
    team_id = plan.get('TeamId')
    if not isinstance(team_id, str) or not team_id or limit is None or limit <= 0 or used is None or used < 0:
        raise QuotaUnavailable(reason='quota_not_returned')
    summary = _object(plan.get('TokenSummary'))
    start, end = reset_at(summary.get('CycleStartTime')), reset_at(summary.get('CycleEndTime'))
    span = int((end - start) / 60) if start and end and end > start else None
    # Do not derive a future reset from an expired package's start date.
    unit = {'enterprise': 'credits', 'enterprise-auto': 'tokens'}.get(plan.get('ProductType'))
    window = QuotaWindow('cycle', '当前周期', min(100, used / limit * 100), span, end,
        max(0, limit - used), limit, unit)
    title = 'Token Plan Enterprise'
    if plan.get('Status') == 'disable':
        title += ' · ' + {'ISOLATED': '欠费停用', 'FROZEN': '已冻结', 'EXHAUSTED': '额度用尽',
                         'DESTROYED': '已销毁'}.get(plan.get('StopReason'), '已停用')
    # The provider-issued UIN plus team scopes a quota pool; labels never dedupe.
    uin = plan.get('Uin')
    identity = f'uin:{uin}' if isinstance(uin, str) and re.fullmatch(r'[0-9]{1,128}', uin) else f'key:{secret_id}'
    return AccountQuota('腾讯 TokenHub', native_account_label('腾讯 TokenHub', organization=plan.get('Name')),
        (window,), title, 'cloud-plan:' + account_key(TC_HOST, f'{identity}\0{region}\0{team_id}'), kind='plan')


async def _collect(jobs, timeout):
    limiter = asyncio.Semaphore(4)
    async def run(label, operation):
        try:
            async with limiter:
                return await operation()
        except QuotaUnavailable as exc:
            return QuotaIssue(exc.state, label, exc.reason)
        except (httpx.HTTPError, ValueError, TypeError, OverflowError):
            return QuotaIssue('unavailable', label, 'request_failed')
    tasks = [asyncio.create_task(run(label, operation)) for label, operation in jobs]
    try:
        done, _ = await asyncio.wait(tasks, timeout=max(0, timeout)) if tasks else (set(), set())
        rows = [task.result() if task in done else QuotaIssue('unavailable', job[0], 'request_failed')
                for task, job in zip(tasks, jobs)]
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return QuotaReadResult(tuple(row for row in rows if isinstance(row, AccountQuota)),
                           tuple(row for row in rows if isinstance(row, QuotaIssue)))


async def _tencent(client, config, secrets, now, total_timeout):
    region = config.get('region', 'ap-guangzhou')
    if region not in ('ap-guangzhou', 'ap-singapore'):
        raise QuotaUnavailable(reason='request_failed')
    secret_id, secret_key = _secret(secrets, 'secretId'), _secret(secrets, 'secretKey')
    team = config.get('teamId', '')
    if not isinstance(team, str) or (team and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', team)):
        raise QuotaUnavailable(reason='request_failed')
    deadline = asyncio.get_running_loop().time() + total_timeout
    plans, issues = [], []
    if team:
        plans = [{'TeamId': team}]
    else:
        seen, offset = set(), 0
        while True:
            try:
                reply = await asyncio.wait_for(_tencent_call(client, region, 'DescribeTokenPlanList',
                    {'Offset': offset, 'Limit': 100}, secret_id, secret_key, now),
                    timeout=max(0, deadline - asyncio.get_running_loop().time()))
                batch = reply.get('TokenPlanSet')
                if not isinstance(batch, list):
                    raise QuotaUnavailable(reason='quota_not_returned')
                total = finite(reply.get('TotalCount'))
                fresh = 0
                for item in batch:
                    item = _object(item)
                    item_id = item.get('TeamId')
                    if isinstance(item_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item_id) and item_id not in seen:
                        seen.add(item_id)
                        plans.append(item)
                        fresh += 1
                offset += len(batch)
                if (total is not None and offset >= total) or (total is None and len(batch) < 100):
                    if total is not None and len(seen) < total:
                        raise QuotaUnavailable(reason='quota_not_returned')
                    break
                if not fresh or offset >= 10000:
                    raise QuotaUnavailable(reason='quota_not_returned')
            except (QuotaUnavailable, TimeoutError) as exc:
                if not plans:
                    if isinstance(exc, QuotaUnavailable):
                        raise
                    raise QuotaUnavailable(reason='request_failed') from None
                issues.append(QuotaIssue(getattr(exc, 'state', 'unavailable'), '腾讯 TokenHub',
                    getattr(exc, 'reason', None) or 'request_failed'))
                break
    if not plans:
        raise QuotaUnavailable(reason='quota_not_returned')

    async def detail(plan):
        try:
            reply = await _tencent_call(client, region, 'DescribeTokenPlan', {'TeamId': plan['TeamId']}, secret_id, secret_key, now)
            if reply.get('TeamId') not in (None, plan['TeamId']):
                raise QuotaUnavailable(reason='quota_not_returned')
            combined = {**plan, **{key: value for key, value in reply.items() if value is not None}}
            return _tencent_account(combined, region, secret_id)
        except QuotaUnavailable as original:
            # The list also carries current quota values. Keep those actual
            # values when detail/reset metadata is temporarily unavailable.
            try:
                return _tencent_account(plan, region, secret_id)
            except QuotaUnavailable:
                raise original
    result = await _collect([('腾讯 TokenHub', lambda plan=plan: detail(plan)) for plan in plans],
        deadline - asyncio.get_running_loop().time())
    return QuotaReadResult(result.accounts, tuple(issues) + result.issues)


async def read_connection(provider: str, config: dict, secrets: dict, *, transport=None, now=None, total_timeout=28):
    """Query explicit cloud credentials; no local account discovery or writes."""
    now = now or (lambda: datetime.now(timezone.utc))
    if not isinstance(config, dict) or not isinstance(secrets, dict):
        raise QuotaUnavailable('unauthenticated', reason='cloud_credentials_required')
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        if provider == 'tencent':
            return await _tencent(client, config, secrets, now, total_timeout)
        if provider != 'volcengine':
            raise QuotaUnavailable('unsupported', reason='unsupported_provider')
        region, plan = config.get('region', 'cn-beijing'), config.get('plan', 'both')
        if not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}-[a-z0-9-]{1,36}', region) or plan not in ('coding', 'agent', 'both'):
            raise QuotaUnavailable(reason='request_failed')
        access_key, secret_key = _secret(secrets, 'accessKeyId'), _secret(secrets, 'secretAccessKey')
        plans = ('coding', 'agent') if plan == 'both' else (plan,)
        return await _collect([('火山方舟', lambda plan=plan: _volc_one(client, region, plan, access_key, secret_key, now))
                               for plan in plans], total_timeout)
