"""Use OMP's non-inference usage command outside the workspace."""
import asyncio
import json
import mmap
import os
from pathlib import Path
import re
import signal
import tempfile

from drivers.account_quota import AccountQuota, QuotaBalance, QuotaIssue, QuotaReadResult, QuotaUnavailable, QuotaWindow, native_account_label
from drivers.acp.native_cli import native_path
from drivers.native_quota import LIMIT, account_key, driver_env, finite, reset_at


def supports_usage(executable):
    path = Path(executable).resolve()
    try:
        if path.stat().st_size < 8192:
            script = path.read_text()
            # Kaus's installation wrapper names a fixed binary; never evaluate shell text.
            if '# Kaus local Agent installation ' in script:
                match = re.search(r'^exec (/[A-Za-z0-9_./-]+) "\$@"$', script, re.M)
                if match:
                    path = Path(match[1]).resolve()
        with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as code:
            return code.find(b'accountsWithoutUsage') >= 0 and any(code.find(marker) >= 0
                for marker in (b'handleUsageCommand', b'runUsageCommand'))
    except (OSError, ValueError, UnicodeError):
        return False


def normalize_usage(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('reports'), list):
        raise QuotaUnavailable(reason='quota_not_returned')
    accounts, issues = [], []
    for report in payload['reports']:
        if not isinstance(report, dict) or not isinstance(report.get('provider'), str):
            continue
        provider = report['provider']; windows, balances = [], []
        metadata = report.get('metadata') if isinstance(report.get('metadata'), dict) else {}
        for row in report.get('limits', []) if isinstance(report.get('limits'), list) else []:
            if not isinstance(row, dict) or not isinstance(row.get('amount'), dict):
                continue
            amount = row['amount']; period = row.get('window') if isinstance(row.get('window'), dict) else {}
            used, total, left = (finite(amount.get(field)) for field in ('used', 'limit', 'remaining'))
            fraction = finite(amount.get('usedFraction'))
            remaining_fraction = finite(amount.get('remainingFraction'))
            percent = fraction * 100 if fraction is not None else None
            if percent is None and total is not None and total > 0:
                if used is not None: percent = used / total * 100
                elif left is not None: percent = (total - left) / total * 100
            if percent is None and amount.get('unit') == 'percent' and used is not None: percent = used
            if percent is None and remaining_fraction is not None: percent = (1 - remaining_fraction) * 100
            ident = str(row.get('id') or len(windows)); label = str(row.get('label') or period.get('label') or 'Allowance')
            if percent is None:
                if amount.get('unit') == 'usd' and left is not None:
                    balances.append(QuotaBalance(ident, label, left, 'USD'))
                continue
            duration = finite(period.get('durationMs'))
            windows.append(QuotaWindow(ident, label, max(0, percent), int(duration / 60000) if duration and duration > 0 else None,
                reset_at(period.get('resetsAt')), left, total, amount.get('unit')))
        if not windows and not balances:
            issues.append(QuotaIssue('unavailable', provider, 'quota_not_returned')); continue
        identity = metadata.get('accountId')
        scope = metadata.get('orgId') or metadata.get('projectId') or ''
        key = account_key('omp:' + provider, str(identity) + '\0' + str(scope)) if identity else None
        plan = metadata.get('plan') or metadata.get('planType')
        accounts.append(AccountQuota(provider, native_account_label(provider, metadata.get('email')), tuple(windows),
            plan if isinstance(plan, str) else None, key, tuple(balances), 'subscription' if windows else 'balance'))
    for row in payload.get('accountsWithoutUsage', []) if isinstance(payload.get('accountsWithoutUsage'), list) else []:
        if isinstance(row, dict) and isinstance(row.get('provider'), str):
            issues.append(QuotaIssue('unavailable', row['provider'], 'quota_not_returned'))
    if not accounts and not issues:
        raise QuotaUnavailable('unauthenticated', reason='sign_in_required')
    return QuotaReadResult(tuple(accounts), tuple(issues))


async def command(executable, args, env, cwd, *, timeout=30, limit=LIMIT):
    try:
        process = await asyncio.create_subprocess_exec(executable, *args,
            cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == 'posix')
    except OSError:
        raise QuotaUnavailable('unsupported', reason='native_version_unsupported') from None
    try:
        async def read():
            chunks = bytearray()
            while block := await process.stdout.read(65536):
                chunks.extend(block)
                if len(chunks) > limit: raise QuotaUnavailable(reason='quota_not_returned')
            await process.wait()
            return bytes(chunks)
        payload = await asyncio.wait_for(read(), timeout)
        return process.returncode, payload
    except asyncio.TimeoutError:
        raise QuotaUnavailable(reason='request_failed') from None
    finally:
        if process.returncode is None:
            if os.name == 'posix':
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else: process.kill()
            await process.wait()


async def read_account_quota(driver):
    executable = native_path(driver)
    if not executable or not await asyncio.to_thread(supports_usage, executable):
        raise QuotaUnavailable('unsupported', reason='native_version_unsupported')
    with tempfile.TemporaryDirectory(prefix='kaus-omp-usage-') as cwd:
        env = driver_env(driver)
        # Probe the actual usage contract; global noExtensions symbols alone
        # do not establish whether this subcommand accepts the flag.
        status, help_text = await command(executable, ('usage', '--help'), env, cwd, timeout=5, limit=65536)
        if status or not all(value in help_text for value in (b'usage [', b'--json')):
            raise QuotaUnavailable('unsupported', reason='native_version_unsupported')
        args = ('usage', '--json', '--no-extensions')
        if b'--no-extensions' not in help_text:
            # v18.3.2's usage branch predates extension discovery entirely:
            # Settings.loadReadOnly -> native auth -> static ModelRegistry ->
            # usage.reports. Verified official commit 7853b4e49993; no need
            # to require a later flag for that already non-inference command.
            version_status, version = await command(executable, ('--version',), env, cwd, timeout=5, limit=1024)
            if version_status or version.strip() != b'omp/18.3.2':
                raise QuotaUnavailable('unsupported', reason='native_version_unsupported')
            args = ('usage', '--json')
        status, payload = await command(executable, args, env, cwd)
        if status:
            raise QuotaUnavailable(reason='request_failed')
        try:
            return normalize_usage(json.loads(payload))
        except (ValueError, UnicodeError):
            raise QuotaUnavailable(reason='quota_not_returned') from None
