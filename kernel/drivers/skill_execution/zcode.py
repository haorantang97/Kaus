"""Constrain the native bundled SkillPort, including fresh plugin discoveries.

The preload changes only the running process; selection is immutable for its
lifetime. Native mode keeps the original CLI. Unknown bundle layouts fail closed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
from collections import Counter

import yaml

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported

ANCHORS = (
    'const roots = request.roots ?? await resolveDefaultSkillRoots(workingDirectory, this.options);',
    'const skillPaths = await this.skillFilesUnderRoot(root3, diagnostics);',
    'const parsed = await this.parseSkill(path6, root3, diagnostics);',
)


def native_command(spec):
    args = spec.command
    if '--command' in args:
        index = args.index('--command')
        if index + 1 < len(args):
            return shutil.which(args[index + 1], path=spec.build_env().get('PATH'))
    return shutil.which('zcode', path=spec.build_env().get('PATH'))


def bundle_path(executable):
    """Resolve known installation layouts without evaluating a shell wrapper."""
    candidates, visited = [Path(executable)], set()
    while candidates:
        path = candidates.pop().resolve()
        if path in visited:
            continue
        visited.add(path)
        for candidate in (path, path.parent.parent / 'lib/zcode.cjs'):
            if candidate.name == 'zcode.cjs' and candidate.is_file():
                return candidate.resolve()
        try:
            if path.stat().st_size > 65536:
                continue
            source = path.read_text()
            for match in re.finditer(r'^exec\s+([^\n]+)', source, re.M):
                args = shlex.split(match.group(1))
                if args and Path(args[0]).is_absolute():
                    candidates.append(Path(args[0]))
        except (OSError, UnicodeError, ValueError):
            pass
    return None


def immutable(path, content):
    if path.exists():
        if path.is_symlink() or path.read_text() != content:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(path, content)


async def native_catalog(executable, spec, cwd):
    process = await asyncio.create_subprocess_exec(executable, 'skills', 'list', '--json',
        cwd=cwd, env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 20)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if isinstance(exc, asyncio.TimeoutError):
            raise UnsupportedCapabilityError('原生技能加载验证超时') from None
        raise
    if process.returncode or len(output) > 8 * 1024 * 1024:
        raise UnsupportedCapabilityError('原生技能加载验证失败')
    try:
        result = json.loads(output)
        return result['skills']
    except (ValueError, TypeError, KeyError):
        raise UnsupportedCapabilityError('原生技能目录格式不兼容') from None


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        return materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    executable = native_command(spec)
    module = bundle_path(executable) if executable else None
    if not module or os.name == 'nt':
        return unsupported(binding, '当前安装未提供可验证的原生技能加载入口', dry_run=dry_run)
    source = module.read_text()
    if any(source.count(anchor) != 1 for anchor in ANCHORS):
        return unsupported(binding, '当前引擎版本的技能加载接口需要适配', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if dry_run:
        return plan
    # Some native tools require a canonical Skill name (e.g. workflow gates).
    # Preserve unique source names while retaining immutable, disambiguated paths.
    manifest_rows = json.loads((plan.root / '.kaus-skill-selection.json').read_text())['skills']
    original_names = []
    for row in manifest_rows:
        text = Path(row['source']).read_text()
        front = yaml.safe_load(text[4:].split('\n---', 1)[0]) if text.startswith('---\n') else {}
        name = front.get('name') if isinstance(front, dict) else None
        original_names.append(name if isinstance(name, str) and name else row['name'])
    counts = Counter(original_names)
    native_counts = Counter(name for name, row in zip(original_names, manifest_rows) if row['type'] != 'skills')
    plan.names = tuple(name if counts[name] == 1 or (row['type'] != 'skills' and native_counts[name] == 1) else fallback
                       for name, fallback, row in zip(original_names, plan.names, manifest_rows))
    data = {'module': str(module), 'moduleHash': hashlib.sha256(source.encode()).hexdigest(),
            'paths': [str(path) for path in plan.paths],
            'names': {str(path): name for path, name in zip(plan.paths, plan.names)},
            'roots': [{'path': str(path.parent), 'source': 'plugin', 'scope': 'project', 'priority': i}
                      for i, path in enumerate(plan.paths)]}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('zcode-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    guard_text = Path(__file__).with_name('zcode_guard.cjs').read_text()
    guard = plan.root / ('zcode-guard-' + hashlib.sha256(guard_text.encode()).hexdigest()[:24] + '.cjs')
    immutable(manifest, encoded)
    immutable(guard, guard_text)
    # NODE_OPTIONS is parsed by Node, not by a shell; quote spaces explicitly.
    options = (spec.build_env().get('NODE_OPTIONS', '') + ' --require=' + json.dumps(str(guard), ensure_ascii=False)).strip()
    plan.spec = spec.with_env(KAUS_ZCODE_SKILL_SCOPE=str(manifest), NODE_OPTIONS=options)
    rows = await native_catalog(executable, plan.spec, workspace_root or spec.cwd)
    if {row.get('path') for row in rows} != {str(path) for path in plan.paths}:
        raise UnsupportedCapabilityError('原生技能加载集合与项目配置不一致')
    return plan


def cli_command(plan, command):
    if plan and plan.root and not plan.spec.env.get('KAUS_ZCODE_SKILL_SCOPE'):
        raise UnsupportedCapabilityError('项目技能终端配置不可用')
    return tuple(command)


def remember_session_options(spec, conversation, metadata):
    from .bridge_terminal import remember
    remember(spec, conversation, metadata)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .bridge_terminal import manifest, record
    from .terminal_settings import approval, choice, environment, selected, immutable as frozen
    cli_command(plan, command)
    module = bundle_path(command[0])
    anchor = 'titleGeneration: TUI_TITLE_GENERATION_CONFIG,'
    if not module or module.read_text().count(anchor) != 1:
        raise UnsupportedCapabilityError('当前引擎的终端配置接口需要适配')
    source = module.read_text()
    if 'import("@zcode/tui")' in source and not any(
            (parent / 'node_modules/@zcode/tui/dist/index.js').is_file() for parent in module.parents):
        raise UnsupportedCapabilityError('当前安装缺少原生 TUI 组件，请安装完整的 ZCode 版本')
    value = record(spec, conversation, binding, 'zcode-bridge')
    from drivers.zcode_bridge.shapes import mcp_servers
    value['terminalMcpServers'] = mcp_servers(value['mcpServers'])
    value['nativeArgs'] = [spec.command[index + 1] for index, arg in enumerate(spec.command[:-1]) if arg == '--native-arg']
    value.update(executable=command[0], module=str(module),
                 moduleHash=hashlib.sha256(module.read_bytes()).hexdigest(),
                 model=selected(conversation, 'model_id', 'model'),
                 reasoning=selected(conversation, 'reasoning_mode', 'thought_level'),
                 permission=choice(approval(conversation), {
                     'ask': 'build', 'host:ask': 'build', 'auto': 'edit', 'bypass': 'yolo',
                     'build': 'build', 'edit': 'edit', 'yolo': 'yolo', 'plan': 'plan'}, '审批方式'))
    path = manifest(spec, conversation, value)
    guard = frozen(spec, conversation, Path(__file__).with_name('zcode_guard.cjs').read_text(), suffix='.cjs')
    active = plan.spec if plan else spec
    env = active.build_env()
    overrides = {'KAUS_ZCODE_TERMINAL_SCOPE': str(path),
                 'NODE_OPTIONS': (env.get('NODE_OPTIONS', '') + ' --require=' + json.dumps(str(guard), ensure_ascii=False)).strip()}
    if env.get('KAUS_ZCODE_SKILL_SCOPE'):
        overrides['KAUS_ZCODE_SKILL_SCOPE'] = env['KAUS_ZCODE_SKILL_SCOPE']
    return environment(spec, conversation,
        (sys.executable, str(Path(__file__).with_name('zcode_terminal.py')), str(path)), overrides)


async def refresh_inventory(spec):
    from .zcode_inventory import refresh
    await refresh(spec)


def inventory_roots(spec, projects, bindings):
    from .zcode_inventory import roots
    return roots(spec, projects, bindings)


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from .zcode_history import read
    return await asyncio.to_thread(read, spec, binding, native_session_id)
