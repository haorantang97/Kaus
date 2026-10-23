"""OMP's native, process-local configuration overlay and final Skill allowlist."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported

VERIFIED_VERSIONS = {'18.3.2'}


def scoped_command(command):
    """Preserve existing overlays in order; put the managed overlay last."""
    result, files = [command[0]], []
    index = 1
    while index < len(command):
        arg = command[index]
        if arg in {'--config', '--skills'}:
            index += 1
            if index >= len(command):
                raise UnsupportedCapabilityError('原生启动参数缺少配置值')
            if arg == '--config':
                files.append(command[index])
        elif arg.startswith('--config='):
            files.append(arg.split('=', 1)[1])
        elif arg != '--no-skills' and not arg.startswith('--skills='):
            result.append(arg)
        index += 1
    return tuple(result), files


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    executable = shutil.which(spec.command[0], path=spec.build_env().get('PATH'))
    if not executable:
        return unsupported(binding, '原生 CLI 不可用', dry_run=dry_run)
    process = await asyncio.create_subprocess_exec(executable, '--version', env=spec.build_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return unsupported(binding, '原生引擎版本检测超时', dry_run=dry_run)
    version = re.search(r'\b(\d+\.\d+\.\d+)\b', output.decode(errors='replace'))
    if process.returncode or not version or version[1] not in VERIFIED_VERSIONS:
        return unsupported(binding, '当前 OMP 技能加载版本尚未验证', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    scope = {'skills': {'enabled': bool(plan.names), 'customDirectories': [str(plan.root / '.agents/skills')],
        'includeSkills': list(plan.names), 'ignoredSkills': [],
        'enableCodexUser': False, 'enableClaudeUser': False, 'enableClaudeProject': False,
        'enablePiUser': False, 'enablePiProject': False, 'enableAgentsUser': False, 'enableAgentsProject': False}}
    # JSON is a YAML subset, and the native CLI loads this read-only layer after
    # all user/project settings. Other resources and auth remain in their home.
    encoded = json.dumps(scope, sort_keys=True)
    overlay = plan.root / ('omp-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    if overlay.exists():
        if overlay.is_symlink() or overlay.read_text() != encoded:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(overlay, encoded)
    command, files = scoped_command(spec.command)
    original = spec.build_env().get('PI_CONFIG_FILES', '')
    paths = [path for path in original.split(os.pathsep) if path] + files + [str(overlay)]
    plan.spec = spec.model_copy(update={'command': command}).with_env(PI_CONFIG_FILES=os.pathsep.join(paths))
    return plan


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    if not plan.spec or not plan.spec.env.get('PI_CONFIG_FILES'):
        raise UnsupportedCapabilityError('终端技能配置缺失')
    if not command:
        return ()
    script = '#!/bin/sh\nexec env ' + shlex.quote('PI_CONFIG_FILES=' + plan.spec.env['PI_CONFIG_FILES']) + ' ' + shlex.quote(command[0]) + ' "$@"\n'
    launcher = plan.root / ('omp-terminal-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    if launcher.exists() and (launcher.is_symlink() or launcher.read_text() != script):
        raise UnsupportedCapabilityError('终端技能配置不一致')
    if not launcher.exists():
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    return (str(launcher), *command[1:])


def inventory_roots(spec, projects, bindings):
    from .omp_inventory import roots
    return roots(spec, projects, bindings)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, selected
    mode = selected(conversation, 'execution_mode', 'mode')
    if mode not in (None, 'default'):
        raise UnsupportedCapabilityError('当前运行模式不支持终端续接，请切回默认模式')
    permission = choice(approval(conversation), {'host:ask': 'always-ask', 'ask': 'always-ask',
        'auto': 'write', 'bypass': 'yolo', 'always-ask': 'always-ask', 'write': 'write', 'yolo': 'yolo'}, '审批模式')
    effort = choice(selected(conversation, 'reasoning_mode', 'thinking'),
        {'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'}, '思考强度')
    command = arguments(command, ('--model', selected(conversation, 'model_id', 'model')),
        ('--thinking', effort), ('--approval-mode', permission))
    return cli_command(plan, command) if plan is not None else command


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from .history_io import canonical_id, read_bytes, tree_history
    from .omp_inventory import flags
    ident = canonical_id(native_session_id)
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    base = home / (env.get('PI_CONFIG_DIR') or '.omp')
    profile = next(iter(flags(spec.command, {'--profile'})), None) or env.get('OMP_PROFILE') or env.get('PI_PROFILE')
    root = base / 'profiles' / profile / 'agent' if profile and profile != 'default' else Path(env.get('PI_CODING_AGENT_DIR') or base / 'agent')
    if not cwd:
        raise UnsupportedCapabilityError('原生会话工作目录缺失')
    folder = Path('sessions') / ('--' + re.sub(r'[/\\:]', '-', re.sub(r'^[/\\]', '', cwd)) + '--')
    try:
        matches = [p for p in (root / folder).iterdir() if p.name.endswith('_' + ident + '.jsonl')]
        if len(matches) != 1:
            raise ValueError('ambiguous native transcript')
        rows = [json.loads(line) for line in read_bytes(root, folder / matches[0].name).splitlines() if line.strip()]
        # OMP reserves its first line for the mutable title, outside the branch.
        if rows and rows[0].get('type') == 'title':
            rows = rows[1:]
        raw = b'\n'.join(json.dumps(row).encode() for row in rows)
        return tree_history(raw, ident=ident, cwd=cwd)
    except (OSError, ValueError, TypeError):
        raise RuntimeError('原生会话历史暂不可读') from None
