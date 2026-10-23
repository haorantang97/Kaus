"""Native Skill paths and per-agent permission allowlists, verified before use."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported


async def inspect(executable, spec, cwd, *args):
    process = await asyncio.create_subprocess_exec(executable, 'debug', *args, cwd=cwd,
        env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生项目配置验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生引擎未提供项目配置查询')
    try:
        return json.loads(output)
    except ValueError:
        raise UnsupportedCapabilityError('原生项目配置返回格式不兼容')


def permitted(rules, name):
    outcome = 'ask'
    for rule in rules:
        if fnmatch.fnmatchcase('skill', rule.get('permission', '')) and fnmatch.fnmatchcase(name, rule.get('pattern', '')):
            outcome = rule.get('action')
    return outcome != 'deny'


def scope_matches(rules, allowed):
    relevant = [rule for rule in rules if fnmatch.fnmatchcase('skill', rule.get('permission', ''))]
    resets = [i for i, rule in enumerate(relevant) if rule.get('pattern') == '*' and rule.get('action') == 'deny']
    if not resets:
        return False
    return (all(rule.get('action') == 'deny' or rule.get('pattern') in allowed for rule in relevant[resets[-1] + 1:])
            and all(permitted(rules, name) for name in allowed))


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    executable = shutil.which(spec.command[0], path=spec.build_env().get('PATH'))
    if not executable:
        return unsupported(binding, '原生 CLI 不可用', dry_run=dry_run)
    cwd = workspace_root or spec.cwd
    try:
        loaded = await inspect(executable, spec, cwd, 'config')
    except UnsupportedCapabilityError as exc:
        return unsupported(binding, str(exc), dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    # Never copy credentials from the resolved configuration into the overlay.
    # Keep the original native config, replacing only Skill paths/permissions.
    original = spec.build_env().get('OPENCODE_CONFIG_CONTENT')
    config = json.loads(original) if original else {}
    overlay = {}
    patterns = {'*'}
    for item in [loaded, *loaded.get('agent', {}).values(), *loaded.get('mode', {}).values()]:
        permission = item.get('permission') if isinstance(item, dict) else None
        skills = permission.get('skill') if isinstance(permission, dict) else None
        if isinstance(skills, dict):
            patterns.update(skills)
    policy = {**{pattern: 'deny' for pattern in sorted(patterns)}, **{name: 'allow' for name in plan.names}}
    config['skills'] = {'paths': [str(plan.root / '.agents/skills')]}
    overlay['skills'] = config['skills']
    config['permission'] = {**(config.get('permission') if isinstance(config.get('permission'), dict) else {}), 'skill': policy}
    names = {'build', 'plan', 'general', 'explore', 'compaction', 'summary', 'title'}
    names.update(loaded.get('agent', {}))
    names.update(loaded.get('mode', {}))
    config.setdefault('agent', {})
    overlay['agent'] = {}
    for name in names:
        item = config['agent'].setdefault(name, {})
        item['permission'] = {**(item.get('permission') if isinstance(item.get('permission'), dict) else {}), 'skill': policy}
        overlay['agent'][name] = {'permission': {'skill': policy}}
    permissions = json.loads(spec.build_env().get('OPENCODE_PERMISSION') or '{}')
    permissions['skill'] = policy
    # Native config hooks may create or replace agents after static overlays
    # merge. The last hook reapplies the Skill policy without changing other
    # permissions; tool hooks also keep later mode changes within the selection.
    import yaml
    descriptions = []
    for path in plan.paths:
        content = path.read_text()
        head = yaml.safe_load(content[4:].split('\n---', 1)[0]) if content.startswith('---\n') else {}
        descriptions.append(head.get('description', '') if isinstance(head, dict) else '')
    data = {'root': str(plan.root / '.agents/skills'), 'paths': [str(path) for path in plan.paths],
            'names': list(plan.names), 'descriptions': descriptions, 'policy': policy}
    template = Path(__file__).with_name('opencode_guard.mjs').read_text()
    source = 'const scope=' + json.dumps(data, sort_keys=True) + ';\n' + template
    guard = plan.root / ('opencode-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    if guard.exists():
        if guard.is_symlink() or guard.read_text() != source:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(guard, source)
    plugins = list(config.get('plugin') or [])
    plugins.append(guard.as_uri())
    config['plugin'] = plugins
    plan.spec = spec.with_env(OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_PERMISSION=json.dumps(permissions))
    # The native permission set is authoritative, including custom modes and
    # plugin configuration. Fail if another config layer overrides the policy.
    for name in names:
        agent = await inspect(executable, plan.spec, cwd, 'agent', name)
        rules = agent.get('permission')
        if not isinstance(rules, list) or not scope_matches(rules, set(plan.names)):
            raise UnsupportedCapabilityError('原生 Agent 未执行项目技能范围')
    catalogue = await inspect(executable, plan.spec, cwd, 'skill')
    sources = {item.get('name'): item.get('location') for item in catalogue}
    if any(not sources.get(name) or Path(sources[name]).resolve() != path.resolve()
           for name, path in zip(plan.names, plan.paths)):
        raise UnsupportedCapabilityError('原生引擎未加载完整项目技能副本')
    from .opencode_commands import remember
    remember(plan, catalogue, loaded)
    encoded = json.dumps({'overlay': overlay, 'skill': policy, 'plugins': [guard.as_uri()]}, sort_keys=True)
    manifest = plan.root / ('terminal-overlay-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    if manifest.exists():
        if manifest.is_symlink() or manifest.read_text() != encoded:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(manifest, encoded)
    plan.spec = plan.spec.with_env(KAUS_SKILL_TERMINAL_OVERLAY=str(manifest))
    return plan


def cli_command(plan, command):
    return tuple(command)


def remember_session_options(spec, conversation, metadata):
    from .opencode_family_terminal import remember
    remember(spec, conversation, metadata)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .opencode_family_terminal import command as launch
    return launch(plan, command, conversation=conversation, binding=binding, spec=spec, engine='opencode')


async def validate_prompt(plan, prompt, cwd):
    from .opencode_commands import validate
    await validate(plan, prompt, cwd, inspect=inspect)


def filter_session_update(plan, update):
    from .opencode_commands import filter_update
    return filter_update(plan, update)


def inventory_roots(spec, projects, bindings):
    from .opencode_inventory import roots
    return roots(spec, projects, bindings, agent='opencode', label='OpenCode', prefix='OPENCODE')


async def refresh_inventory(spec):
    from .opencode_inventory import refresh
    await refresh(spec, agent='opencode', prefix='OPENCODE')


async def prepare_runtime_spec(spec, conversation, metadata):
    from .opencode_mcp import prepare
    return await prepare(spec, metadata, prefix='OPENCODE')


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    from .opencode_mcp import verify
    await verify(spec, metadata, session_id)


async def load_native_history(spec, binding, native_session_id):
    from .opencode_history import load
    return await load(spec, binding, native_session_id, engine='opencode')


async def load_local_native_history(spec, binding, native_session_id):
    return await load_native_history(spec, binding, native_session_id)
