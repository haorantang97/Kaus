"""Read OAuth application metadata from the selected native installation.

Never execute a package to inspect it, scan another account, or persist the
values. Native access/refresh credentials remain owned by their existing store.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
import mmap
from pathlib import Path
import re


def client_pair(client_id, client_secret):
    values = (client_id, client_secret)
    if all(isinstance(v, str) and 0 < len(v) <= 512 and not any(c.isspace() for c in v)
           for v in values):
        return values
    return None


def _literal_pair(source, prefix):
    def literal(name):
        pattern = rb'\b' + re.escape(name.encode()) + rb'\s*=\s*\(?\s*[\'\"]([^\'\"\r\n\\]{1,512})[\'\"]'
        values = set(re.findall(pattern, source))
        return next(iter(values)).decode('ascii') if len(values) == 1 else None
    try:
        return client_pair(literal(prefix + 'CLIENT_ID'), literal(prefix + 'CLIENT_SECRET'))
    except UnicodeError:
        return None


@lru_cache(maxsize=32)
def _installed_pair(path, stamp, prefix):
    """Inspect literal native defaults in a bounded file; cache by file identity."""
    source = Path(path)
    try:
        if not 0 < stamp[2] <= 1024 * 1024 * 1024:
            return None
        with source.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            marker, start, pairs = (prefix + 'CLIENT_ID').encode(), 0, set()
            for _ in range(128):
                offset = data.find(marker, start)
                if offset < 0:
                    break
                pair = _literal_pair(data[offset:offset + 4096], prefix)
                if pair:
                    pairs.add(pair)
                start = offset + len(marker)
            else:
                return None
        if _stamp(source) != stamp or len(pairs) != 1:
            return None
        return next(iter(pairs))
    except (OSError, ValueError):
        return None


def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_native(spec, *, antigravity):
    if spec is None:
        return None
    if antigravity:
        from drivers.skill_execution.antigravity import native_paths
        executable, _, _ = native_paths(spec)
        paths = [executable] if executable else []
        prefix = '_DEFAULT_'
    else:
        from drivers.skill_execution.node_package import locate
        package = locate(spec, '@google/gemini-cli')
        if package is None:
            return None
        paths = [package / 'node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js',
                 package.parent / 'gemini-cli-core/dist/src/code_assist/oauth2.js']
        paths.extend(sorted((package / 'bundle').glob('*.js'))[:64])
        prefix = 'OAUTH_'
    pairs = set()
    for path in paths:
        try:
            pair = _installed_pair(str(path.resolve()), _stamp(path), prefix)
        except OSError:
            continue
        if pair:
            pairs.add(pair)
    return next(iter(pairs)) if len(pairs) == 1 else None


async def native_client(driver, *, antigravity=False, env=None):
    """Use connection-local overrides, then the exact selected native runtime."""
    from drivers.native_quota import driver_env
    env = driver_env(driver) if env is None else env
    prefix = 'KAUS_ANTIGRAVITY_OAUTH_' if antigravity else 'KAUS_GEMINI_OAUTH_'
    if env.get(prefix + 'CLIENT_ID') or env.get(prefix + 'CLIENT_SECRET'):
        return client_pair(env.get(prefix + 'CLIENT_ID'), env.get(prefix + 'CLIENT_SECRET'))
    return await asyncio.to_thread(_read_native, getattr(driver, 'agent_spec', None), antigravity=antigravity)
