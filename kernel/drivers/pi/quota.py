"""Account allowances belonging to Pi's own persisted OAuth grants."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable
from drivers.native_quota import driver_env, read_json
from . import vendor_quota as vendor

BRANDS = {'openai-codex': 'OpenAI', 'anthropic': 'Anthropic', 'github-copilot': 'GitHub Copilot',
          'google-gemini-cli': 'Gemini CLI', 'google-antigravity': 'Antigravity',
          'openrouter': 'OpenRouter', 'kimi-coding': 'Kimi Code', 'xai': 'xAI',
          'openai-chatgpt': 'OpenAI', 'meta': 'Meta', 'radius': 'Radius'}
HOSTS = {'openai-codex': {'chatgpt.com'}, 'anthropic': {'api.anthropic.com'},
         'github-copilot': {'api.individual.githubcopilot.com', 'api.business.githubcopilot.com', 'api.enterprise.githubcopilot.com'},
         'google-gemini-cli': {'cloudcode-pa.googleapis.com'},
         'google-antigravity': {'cloudcode-pa.googleapis.com', 'daily-cloudcode-pa.googleapis.com'},
         'openrouter': {'openrouter.ai'}, 'kimi-coding': {'api.kimi.com'},
         'xai': {'api.x.ai', 'cli-chat-proxy.grok.com'}}


def check_route(provider, config):
    value = config.get('baseUrl')
    if value is None:
        return
    try:
        route = urlsplit(value)
        if (route.scheme == 'https' and not route.username and not route.password
                and route.port in (None, 443) and route.hostname in HOSTS.get(provider, set())
                and not route.query and not route.fragment):
            return
    except (ValueError, TypeError, AttributeError):
        pass
    raise QuotaUnavailable('unsupported', reason='unsupported_provider')


async def read_account_quota(driver, *, total_timeout=28):
    env = driver_env(driver)
    home = Path(env.get('PI_CODING_AGENT_DIR') or Path(env.get('HOME') or Path.home()) / '.pi/agent').expanduser()
    credentials = read_json(home / 'auth.json')
    config = read_json(home / 'models.json').get('providers') or {}
    config = config if isinstance(config, dict) else {}
    limiter = asyncio.Semaphore(4)

    async def read(provider, credential):
        brand = BRANDS.get(provider, 'Pi OAuth')
        try:
            async with limiter:
                value = config.get(provider) or {}
                check_route(provider, value if isinstance(value, dict) else {})
                if provider == 'openai-codex': result = await vendor.read_codex(credential)
                elif provider == 'anthropic': result = await vendor.read_anthropic(credential)
                elif provider == 'github-copilot': result = await vendor.read_copilot(credential)
                elif provider in ('google-gemini-cli', 'google-antigravity'):
                    result = await vendor.read_google(credential, antigravity=provider == 'google-antigravity')
                elif provider == 'openrouter':
                    result = await vendor.read_api_oauth(credential, 'https://openrouter.ai/api/v1')
                elif provider == 'kimi-coding':
                    result = await vendor.read_api_oauth(credential, 'https://api.kimi.com/coding/v1')
                elif provider == 'xai': result = await vendor.read_xai(credential)
                else:
                    return QuotaReadResult(issues=(QuotaIssue('unsupported', brand, 'unsupported_provider'),))
                if isinstance(result, AccountQuota):
                    return QuotaReadResult((result,))
                if isinstance(result, QuotaReadResult):
                    return result
                raise QuotaUnavailable(reason='quota_not_returned')
        except QuotaUnavailable as error:
            return QuotaReadResult(issues=(QuotaIssue(error.state, brand, error.reason),))
        except (ValueError, TypeError, AttributeError, KeyError):
            return QuotaReadResult(issues=(QuotaIssue('unavailable', brand, 'quota_not_returned'),))
        except Exception:
            return QuotaReadResult(issues=(QuotaIssue('unavailable', brand, 'request_failed'),))

    entries = [(name, value) for name, value in credentials.items()
               if isinstance(value, dict) and value.get('type') == 'oauth']
    tasks = [asyncio.create_task(read(name, value)) for name, value in entries]
    try:
        done, _ = await asyncio.wait(tasks, timeout=total_timeout) if tasks else (set(), set())
        results = [task.result() if task in done else QuotaReadResult(issues=(
            QuotaIssue('unavailable', BRANDS.get(entry[0], 'Pi OAuth'), 'request_failed'),))
            for task, entry in zip(tasks, entries)]
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return QuotaReadResult(tuple(item for result in results for item in result.accounts),
                          tuple(item for result in results for item in result.issues))
