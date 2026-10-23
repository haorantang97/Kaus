"""Subscription quota from Claude Code's native, non-interactive /usage command.

Only a positively identified local command is launched. Older installations
without that command remain unsupported rather than sending /usage as a prompt.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import lru_cache
import json
import mmap
import os
from pathlib import Path
import re
import signal
import tempfile
from zoneinfo import ZoneInfo

from drivers.account_quota import AccountQuota, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.acp.native_cli import native_path


@lru_cache(maxsize=16)
def _has_local_usage(path, modified, size):
    # This reads installed program code only. The same literal is present in
    # native Bun distributions and the npm CLI script. Unknown wrappers stay off.
    try:
        with open(path, 'rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as code:
            for literal in (b'name:"usage"', b'name: "usage"'):
                offset = 0
                while (offset := code.find(literal, offset)) >= 0:
                    block = code[max(0, offset - 40):offset + 450]
                    if (re.search(rb'type:\s*"local"', block)
                            and re.search(rb'supportsNonInteractive:\s*(?:!0|true)', block)):
                        return True
                    offset += len(literal)
    except (OSError, ValueError):
        pass
    return False


def supports_local_usage(executable):
    path = Path(executable).resolve()
    try:
        stat = path.stat()
    except OSError:
        return False
    return _has_local_usage(str(path), stat.st_mtime_ns, stat.st_size)


async def _command(executable, args, *, cwd, env):
    process = await asyncio.create_subprocess_exec(executable, *args, cwd=cwd, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name == 'posix')
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 20)
        if len(stdout) > 1_000_000:
            raise QuotaUnavailable()
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise QuotaUnavailable()
        return value
    except (ValueError, asyncio.TimeoutError):
        raise QuotaUnavailable() from None
    finally:
        if process.returncode is None:
            if os.name == 'posix':
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                if os.name == 'posix':
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                else:
                    process.kill()
                await process.wait()


def _reset_at(text, now=None):
    if not text:
        return None
    match = re.fullmatch(r'(?:(?P<month>[A-Za-z]{3}) (?P<day>\d{1,2})(?:,| at)? )?'
        r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm) \((?P<zone>[^()]+)\)', text.strip())
    if not match:
        return None
    try:
        zone = ZoneInfo(match['zone'])
        current = now.astimezone(zone) if now else datetime.now(zone)
        hour = int(match['hour'])
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match['ampm'] == 'pm' else 0)
        target = current.replace(hour=hour, minute=int(match['minute'] or 0), second=0, microsecond=0)
        if match['month']:
            month = datetime.strptime(match['month'], '%b').month
            target = target.replace(month=month, day=int(match['day']))
            if target < current - timedelta(minutes=1):
                target = target.replace(year=target.year + 1)
        elif target < current - timedelta(minutes=1):
            target += timedelta(days=1)
        return int(target.timestamp())
    except (ValueError, KeyError):
        return None


def normalize_usage(auth, payload):
    text = payload.get('result')
    if payload.get('is_error') or not isinstance(text, str):
        raise QuotaUnavailable()
    windows = []
    for line in text.splitlines():
        match = re.fullmatch(r'Current (session|week(?: \(([^)]+)\))?):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets (.+))?', line.strip())
        if not match:
            continue
        period, scope, percent, reset = match.groups()
        used = float(percent)
        if period == 'session':
            label, duration, key = '当前周期', 300, 'session'
        else:
            label = '每周 · 全部模型' if scope in (None, 'all models') else f'每周 · {scope}'
            duration, key = 10080, 'week:' + (scope or 'all models')
        windows.append(QuotaWindow(key, label, used, duration, _reset_at(reset)))
    if not windows:
        raise QuotaUnavailable()
    plan = auth.get('subscriptionType')
    if not isinstance(plan, str) or not re.fullmatch(r'[A-Za-z0-9 _-]{1,40}', plan):
        plan = None
    # auth status provides orgId but not a stable subscription member id. An
    # org alone cannot prove two logins share the same personal quota pool.
    return AccountQuota('Anthropic', native_account_label('Claude', auth.get('email'),
        organization=auth.get('orgName')), tuple(windows), plan)


async def read_account_quota(driver):
    executable = native_path(driver)
    if not executable:
        raise QuotaUnavailable()
    if not await asyncio.to_thread(supports_local_usage, executable):
        raise QuotaUnavailable('unsupported')
    env = driver.agent_spec.build_env()
    # Native auth status, not profile email or credential-file parsing, decides
    # whether this connection is a first-party subscription.
    env.pop('CLAUDECODE', None)
    env.pop('CLAUDE_CODE_ENTRYPOINT', None)
    env.update(DISABLE_AUTOUPDATER='1', DISABLE_TELEMETRY='1')
    with tempfile.TemporaryDirectory(prefix='kaus-quota-') as cwd:
        auth = await _command(executable, ['auth', 'status', '--json'], cwd=cwd, env=env)
        if not auth.get('loggedIn'):
            raise QuotaUnavailable('unauthenticated')
        if auth.get('apiProvider') != 'firstParty' or auth.get('authMethod') != 'claude.ai':
            raise QuotaUnavailable('unsupported')
        # Keep user configuration so native provider routing remains consistent.
        # Disable all hooks and MCP in this process, with no workspace sources.
        payload = await _command(executable, ['-p', '/usage', '--output-format', 'json',
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', 'user', '--settings', '{"disableAllHooks":true}',
            '--no-session-persistence'], cwd=cwd, env=env)
        return normalize_usage(auth, payload)
