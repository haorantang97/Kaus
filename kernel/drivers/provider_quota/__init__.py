"""Read already configured provider accounts through exact first-party endpoints."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from urllib.parse import urlsplit

import httpx

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable
from .parsers import normalize
from .source_context import failure_issue, normalized_source_url, setup_issue_for, source_id_for, valid_key


@dataclass(frozen=True)
class ProviderSource:
    base_url: str
    api_key: str = field(repr=False)

    @property
    def source_id(self):
        return source_id_for(self.base_url, self.api_key)


@dataclass(frozen=True)
class Endpoint:
    provider: str
    path: str
    parser: str
    currency: str = 'USD'
    bearer: bool = True


HOSTS = {
    'api.deepseek.com': Endpoint('DeepSeek', '/user/balance', 'deepseek'),
    'api.moonshot.cn': Endpoint('Moonshot CN', '/v1/users/me/balance', 'moonshot', 'CNY'),
    'api.moonshot.ai': Endpoint('Moonshot', '/v1/users/me/balance', 'moonshot'),
    'openrouter.ai': Endpoint('OpenRouter', '/api/v1/credits', 'openrouter'),
    'api.siliconflow.cn': Endpoint('SiliconFlow CN', '/v1/user/info', 'siliconflow', 'CNY'),
    'api.siliconflow.com': Endpoint('SiliconFlow', '/v1/user/info', 'siliconflow'),
    'api.stepfun.com': Endpoint('StepFun CN', '/v1/accounts', 'stepfun', 'CNY'),
    'api.stepfun.ai': Endpoint('StepFun', '/v1/accounts', 'stepfun'),
    'api.commandcode.ai': Endpoint('Command Code', '/alpha/billing/credits', 'commandcode'),
    'ollama.com': Endpoint('Ollama Cloud', '/api/balance', 'ollama'),
    'aihubmix.com': Endpoint('AiHubMix', '/dashboard/billing/remain', 'aihubmix'),
    'open.bigmodel.cn': Endpoint('智谱', '/api/monitor/usage/quota/limit', 'zhipu', bearer=False),
    'api.z.ai': Endpoint('Z.ai', '/api/monitor/usage/quota/limit', 'zhipu', bearer=False),
    'api.minimaxi.com': Endpoint('MiniMax CN', '/v1/token_plan/remains', 'minimax'),
    'api.minimax.cn': Endpoint('MiniMax CN', '/v1/token_plan/remains', 'minimax'),
    'api.minimax.io': Endpoint('MiniMax', '/v1/token_plan/remains', 'minimax'),
}


def endpoint_for(base_url):
    if not isinstance(base_url, str):
        return None
    try:
        url = urlsplit(base_url)
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment:
            return None
        host = url.hostname
        if host in ('api.kimi.com', 'api.kimi.ai') and url.path.rstrip('/') in ('/coding', '/coding/v1'):
            result = Endpoint('Kimi Code', '/coding/v1/usages', 'kimi-code')
        elif host == 'opencode.ai' and url.path.rstrip('/') in ('/zen/go', '/zen/go/v1', '/inference/go/openai/v1', '/inference/go/anthropic/v1'):
            result = Endpoint('OpenCode Go', '/zen/go/v1/usage', 'opencode-go')
        else:
            result = HOSTS.get(host)
        return (f'https://{host}{result.path}', result) if result else None
    except ValueError:
        return None


async def _request(client, url, key, endpoint):
    headers = {'Accept': 'application/json', 'Authorization': ('Bearer ' if endpoint.bearer else '') + key}
    async with client.stream('GET', url, headers=headers) as response:
        if response.status_code == 403 and endpoint.parser == 'openrouter':
            raise QuotaUnavailable('unsupported', reason='management_key_required')
        if response.status_code == 401:
            raise QuotaUnavailable('unauthenticated', reason='credential_expired')
        if response.status_code == 403:
            raise QuotaUnavailable(reason='permission_required')
        if response.status_code == 410 and endpoint.parser == 'siliconflow':
            raise QuotaUnavailable('unsupported', reason='endpoint_retired')
        if response.status_code == 429:
            raise QuotaUnavailable(reason='request_failed')
        if response.status_code != 200:
            raise QuotaUnavailable(reason='request_failed')
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 1_000_000:
                raise QuotaUnavailable(reason='quota_not_returned')
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise QuotaUnavailable(reason='quota_not_returned') from None


async def read_sources(sources, *, transport=None, total_timeout=28):
    """Bounded concurrent reads, with failures confined to the failing provider."""
    jobs, source_ids, issues = [], set(), []
    for source in sources:
        if source.source_id is None or source.source_id in source_ids:
            continue
        source_ids.add(source.source_id)
        selected = endpoint_for(source.base_url)
        setup = setup_issue_for(source.base_url, source.api_key, supported=selected is not None)
        if setup is not None:
            issues.append(setup)
            continue
        if selected is None:
            continue
        url, endpoint = selected
        key = source.api_key
        if not valid_key(key):
            issues.append(failure_issue(source, endpoint, 'unauthenticated', 'credential_unavailable'))
            continue
        key = key.strip()
        identity = hashlib.sha256((urlsplit(url).hostname + '\0' + key).encode()).hexdigest()
        jobs.append((url, endpoint, key, identity, source))
    limiter = asyncio.Semaphore(4)
    # Redirects cannot forward a provider key to a changed origin. Test transports
    # exercise the same URL and header validation without network access.
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False, transport=transport) as client:
        async def one(job):
            url, endpoint, key, identity, source = job
            try:
                async with limiter:
                    parser = endpoint.parser
                    try:
                        payload = await _request(client, url, key, endpoint)
                    except QuotaUnavailable as exc:
                        if endpoint.parser != 'openrouter' or exc.reason != 'management_key_required':
                            raise
                        # A normal OpenRouter inference key can reveal its own
                        # explicit budget, not the account's total cash balance.
                        fallback = Endpoint('OpenRouter', '/api/v1/key', 'openrouter-key')
                        payload = await _request(client, 'https://openrouter.ai/api/v1/key', key, fallback)
                        parser = fallback.parser
                windows, balances, plan = normalize(parser, payload, currency=endpoint.currency)
                return AccountQuota(endpoint.provider, endpoint.provider + ' API', windows, plan,
                    'provider-key:' + identity, balances, 'plan' if windows else 'balance', source.source_id)
            except QuotaUnavailable as exc:
                return failure_issue(source, endpoint, exc.state, exc.reason)
            except (httpx.HTTPError, ValueError, TypeError):
                return failure_issue(source, endpoint, 'unavailable', 'request_failed')
        tasks = [asyncio.create_task(one(job)) for job in jobs]
        try:
            if tasks:
                done, _ = await asyncio.wait(tasks, timeout=total_timeout)
                replies = [task.result() if task in done else failure_issue(job[4], job[1], 'unavailable', 'request_failed')
                           for task, job in zip(tasks, jobs)]
            else:
                replies = []
        finally:
            # The outer service can also cancel a refresh. Never leave request
            # tasks running after its HTTP client has closed.
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    return QuotaReadResult(tuple(row for row in replies if isinstance(row, AccountQuota)),
                          tuple(issues) + tuple(row for row in replies if isinstance(row, QuotaIssue)))


def discover_provider_sources(driver):
    from .discovery import discover
    return discover(driver)


async def read_provider_quotas(driver):
    sources = await asyncio.to_thread(discover_provider_sources, driver)
    return await read_sources(sources)
