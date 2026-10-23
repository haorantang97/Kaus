"""Kilo's native Skill permission policy, plus process-local plugin enforcement."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported
from .opencode import inspect, scope_matches

VERIFIED_VERSIONS = {'7.8.1'}


async def agent_list(executable, spec, cwd):
    process = await asyncio.create_subprocess_exec(executable, 'agent', 'list', cwd=cwd,
        env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生 Agent 配置验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生 Agent 配置不可读取')
    text = output.decode()
    result = {}
    for match in re.finditer(r'^([^\s]+) \(([^)]+)\)\s*', text, re.M):
        try:
            rules, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
        except ValueError:
            raise UnsupportedCapabilityError('原生 Agent 配置返回格式不兼容')
        if not isinstance(rules, list):
            raise UnsupportedCapabilityError('原生 Agent 权限格式不兼容')
        result[match[1]] = rules
    if not result:
        raise UnsupportedCapabilityError('原生 Agent 配置为空')
    return result


def _write(path, content):
    if path.exists():
        if path.is_symlink() or path.read_text() != content:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(path, content)


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    executable = shutil.which(spec.command[0], path=spec.build_env().get('PATH'))
    if not executable:
        return unsupported(binding, '原生 CLI 不可用', dry_run=dry_run)
    version_proc = await asyncio.create_subprocess_exec(executable, '--version', env=spec.build_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(version_proc.communicate(), 10)
    except asyncio.TimeoutError:
        version_proc.kill()
        await version_proc.wait()
        return unsupported(binding, '原生引擎版本检测超时', dry_run=dry_run)
    version = re.search(r'\b(\d+\.\d+\.\d+)\b', output.decode(errors='replace'))
    if version_proc.returncode or not version or version[1] not in VERIFIED_VERSIONS:
        return unsupported(binding, '当前 Kilo 技能加载版本尚未验证', dry_run=dry_run)
    cwd = workspace_root or spec.cwd
    loaded = await inspect(executable, spec, cwd, 'config')
    agents = await agent_list(executable, spec, cwd)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    patterns = {'*'}
    for item in [loaded, *loaded.get('agent', {}).values(), *loaded.get('mode', {}).values()]:
        permission = item.get('permission') if isinstance(item, dict) else None
        skills = permission.get('skill') if isinstance(permission, dict) else None
        if isinstance(skills, dict):
            patterns.update(skills)
    policy = {**{pattern: 'deny' for pattern in sorted(patterns)}, **{name: 'allow' for name in plan.names}}
    original = spec.build_env().get('KILO_CONFIG_CONTENT')
    config = json.loads(original) if original else {}
    config['skills'] = {'paths': [str(plan.root / '.agents/skills')]}
    config['permission'] = {**(config.get('permission') if isinstance(config.get('permission'), dict) else {}), 'skill': policy}
    names = set(agents) | set(loaded.get('agent', {})) | set(loaded.get('mode', {}))
    config.setdefault('agent', {})
    for name in names:
        item = config['agent'].setdefault(name, {})
        item['permission'] = {**(item.get('permission') if isinstance(item.get('permission'), dict) else {}), 'skill': policy}
    # A native plugin keeps late-created modes and tool calls within the same
    # set. User plugins, authentication and non-Skill permissions remain intact.
    import yaml
    descriptions = []
    for path in plan.paths:
        content = path.read_text()
        head = yaml.safe_load(content[4:].split('\n---', 1)[0]) if content.startswith('---\n') else {}
        descriptions.append(head.get('description', '') if isinstance(head, dict) else '')
    data = {'descriptions': descriptions, 'root': str(plan.root / '.agents/skills'), 'paths': [str(path) for path in plan.paths],
            'names': list(plan.names), 'policy': policy}
    template = Path(__file__).with_name('kilo_guard.mjs').read_text()
    source = 'const scope=' + json.dumps(data, sort_keys=True) + ';\n' + template
    guard = plan.root / ('kilo-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    _write(guard, source)
    plugins = list(config.get('plugin') or [])
    plugins.append(guard.as_uri())
    config['plugin'] = plugins
    plan.spec = spec.with_env(KILO_CONFIG_CONTENT=json.dumps(config))
    final = await agent_list(executable, plan.spec, cwd)
    for name, rules in final.items():
        # Utility agents may have a native deny-all fence after our policy.
        # They must never gain Skill access merely because a project selected it.
        relevant = [rule for rule in rules if rule.get('permission') in {'*', 'skill'}]
        if not scope_matches(rules, set(plan.names)):
            if not relevant or relevant[-1].get('pattern') != '*' or relevant[-1].get('action') != 'deny':
                raise UnsupportedCapabilityError('原生 Agent 未执行项目技能范围')
    catalog = await inspect(executable, plan.spec, cwd, 'skill')
    sources = {item.get('name'): item.get('location') for item in catalog}
    if any(not sources.get(name) or Path(sources[name]).resolve() != path.resolve()
           for name, path in zip(plan.names, plan.paths)):
        raise UnsupportedCapabilityError('原生引擎未加载完整项目技能副本')
    from .opencode_commands import remember
    remember(plan, catalog, loaded, aliases=True)
    return plan


def cli_command(plan, command):
    return tuple(command)


def remember_session_options(spec, conversation, metadata):
    from .opencode_family_terminal import remember
    remember(spec, conversation, metadata)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .opencode_family_terminal import command as launch
    return launch(plan, command, conversation=conversation, binding=binding, spec=spec, engine='kilo')


async def validate_prompt(plan, prompt, cwd):
    from .opencode_commands import validate
    await validate(plan, prompt, cwd, inspect=inspect)


def filter_session_update(plan, update):
    from .opencode_commands import filter_update
    return filter_update(plan, update)


def inventory_roots(spec, projects, bindings):
    from .opencode_inventory import roots
    return roots(spec, projects, bindings, agent='kilo', label='Kilo', prefix='KILO')


async def refresh_inventory(spec):
    from .opencode_inventory import refresh
    await refresh(spec, agent='kilo', prefix='KILO')


async def prepare_runtime_spec(spec, conversation, metadata):
    from .opencode_mcp import prepare
    return await prepare(spec, metadata, prefix='KILO')


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    from .opencode_mcp import verify
    await verify(spec, metadata, session_id)


async def load_native_history(spec, binding, native_session_id):
    from .opencode_history import load
    return await load(spec, binding, native_session_id, engine='kilo')


async def load_local_native_history(spec, binding, native_session_id):
    return await load_native_history(spec, binding, native_session_id)
