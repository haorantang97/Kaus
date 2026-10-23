"""DSH registry scope, shared by native profiles and bridges using that registry."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported
from .node_package import locate

VERIFIED_MODULES = {
    'dsh-skill': '41137835ad018bf1974ef781c14d2a7974db4c502f484fc88d0b0608a26de88e',
    'dsh-skill-filesystem': '1aea87781ba5b4d44c7cfd6184d933a1c171a3fd2791456b65ebfc8b255f1221',
}


def dependency(package, name):
    for parent in (package, *package.parents):
        path = parent / 'node_modules' / name / 'lib/index.js'
        if path.is_file():
            return path.resolve()
    return None


def _write(path, content):
    if path.exists():
        if path.is_symlink() or path.read_text() != content:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(path, content)


async def project_for_package(spec, binding, effective, workspace_root, package_name, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    package = locate(spec, package_name)
    modules = {name: dependency(package, '@deepseek-ai/' + name) for name in VERIFIED_MODULES} if package else {}
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if not node or not modules or any(not path or hashlib.sha256(path.read_bytes()).hexdigest() != VERIFIED_MODULES[name]
                                     for name, path in modules.items()):
        return unsupported(binding, '当前 DSH 技能加载版本尚未验证', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    source = Path(__file__).with_name('dsh_guard.mjs').read_text()
    guard = plan.root / ('dsh-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    _write(guard, source)
    data = {'package': str(package.resolve()), 'root': str(plan.root / '.agents/skills'),
            'paths': [str(path) for path in plan.paths], 'hashes': VERIFIED_MODULES}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('dsh-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    _write(manifest, encoded)
    options = spec.build_env().get('NODE_OPTIONS', '')
    plan.spec = spec.with_env(KAUS_DSH_SKILL_SCOPE=str(manifest),
        NODE_OPTIONS=(options + ' --import=' + guard.as_uri()).strip())
    # Exercise the installed registry/parser, without booting a profile, reading
    # authentication, or contacting a model. Runtime hooks also cover profile-
    # local copies of these packages and reject an unverified implementation.
    cordis = dependency(package, '@deepseek-ai/cordis')
    script = f'''const {{Context}}=await import({json.dumps(cordis.as_uri())});
const {{SkillRegistry}}=await import({json.dumps(modules['dsh-skill'].as_uri())});
const {{FileSystemSkillProvider}}=await import({json.dumps(modules['dsh-skill-filesystem'].as_uri())});
const ctx=new Context();ctx.provide('logger',{{warn(){{}}}});const registry=new SkillRegistry(ctx);
let provider;registry.registerProvider(control=>provider=new FileSystemSkillProvider(ctx,control,{{watch:false}}));
const list=await registry.list({{cwd:process.cwd()}});
if(list.length!=={len(plan.paths)})throw Error('incomplete');
for(const item of list)if(!(await registry.get(item.name)))throw Error('unreadable');
await provider.dispose();'''
    process = await asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
        env={**plan.spec.build_env(), 'KAUS_DSH_SCOPE_PROBE': '1'}, cwd=workspace_root or spec.cwd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), 20)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生技能加载验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生 DSH 技能加载验证失败')
    return plan


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    return await project_for_package(spec, binding, effective, workspace_root, '@deepseek-ai/dsh',
                                     dry_run=dry_run, runtime_info=runtime_info)


def cli_command(plan, command):
    raise UnsupportedCapabilityError('当前引擎尚未提供原生终端续接入口')


def inventory_roots_for_package(spec, projects, bindings, package_name, agent_key='dsh', label='DeepSeek Harness'):
    from .dsh_inventory import roots
    return roots(spec, projects, bindings, package_name, agent_key, label)


def inventory_roots(spec, projects, bindings):
    return inventory_roots_for_package(spec, projects, bindings, '@deepseek-ai/dsh')


async def refresh_inventory_for_package(spec, package_name):
    from .dsh_inventory import refresh
    await refresh(spec, package_name)


async def refresh_inventory(spec):
    await refresh_inventory_for_package(spec, '@deepseek-ai/dsh')
