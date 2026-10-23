"""Dispatch a managed Codex server and the matching original remote TUI.

The allowlist is an immutable launcher argument, not a mutable shared file.
This standalone helper also runs outside the dashboard's Python import path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from drivers.codex.companions import matching_tui


def command_for(native, server, allowed, version, server_hash, args, env):
    if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in allowed)):
        raise ValueError('项目技能配置无效')
    env = dict(env)
    env.pop('KAUS_CODEX_SKILL_ALLOWLIST', None)
    env.pop('KAUS_CODEX_PERSIST_THREAD_START', None)
    if args[:1] == ['app-server']:
        # ACP uses only the verified server. A separately upgraded native TUI
        # must not disable this path; its version is checked below on handoff.
        path = Path(server)
        if path.is_symlink() or not path.is_file():
            raise ValueError('项目技能运行组件不可用')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != server_hash:
            raise ValueError('项目技能运行组件校验失败')
        env['KAUS_CODEX_PERSIST_THREAD_START'] = '1'
        args = [arg for arg in args[1:] if arg != '--stdio']
        if allowed is None:
            return [server, *args], env
        env['KAUS_CODEX_SKILL_ALLOWLIST'] = json.dumps(allowed)
        config = "skills.config=[" + ",".join(
            "{path=" + json.dumps(path) + ",enabled=true}" for path in allowed) + "]"
        return [server, '-c', config, *args], env
    if not (args[:1] == ['--remote'] or args in (['--help'], ['--version'])):
        raise ValueError('项目技能终端需要使用受管会话连接')
    native = matching_tui(server, version, native)
    try:
        result = subprocess.run([native, '--version'], env=env, check=True,
                                capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('原生 Codex 版本无法确认') from exc
    if result.stdout.strip() != 'codex-cli ' + version:
        raise ValueError('Codex 版本已变更，请更新项目技能运行组件')
    return [native, *args], env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', required=True)
    parser.add_argument('--server', required=True)
    parser.add_argument('--allowed', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--server-hash', required=True)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.args[1:] if options.args[:1] == ['--'] else options.args
    try:
        command, env = command_for(options.native, options.server, json.loads(options.allowed),
                                  options.version, options.server_hash, args, os.environ)
        os.execve(command[0], command, env)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
