"""DSH's declared filesystem sources; no dynamic plugin execution."""
from pathlib import Path
from ._source_inventory import read, expand, global_project, direct_roots
from .inventory import project_workspaces
from .node_package import locate


def roots(spec, projects, bindings, package_name, agent_key, label):
    projects = tuple(projects)
    owner = global_project(projects)
    if not owner:
        return ()
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    cwd = Path(spec.cwd or home)
    native = expand(env.get('DSH_HOME') or '~/.dsh', home, cwd)
    agents = expand(env.get('DSH_AGENTS_HOME') or '~/.agents', home, cwd)
    command = spec.command
    profile = command[command.index('--profile') + 1] if '--profile' in command and command.index('--profile') + 1 < len(command) else 'acp'
    patches = [native / 'profiles' / profile / 'cordis.patch.yml', native / 'cordis.patch.yml']
    patches += [expand(command[i + 1], home, cwd) for i, value in enumerate(command[:-1]) if value == '--patch']
    config, enabled, badge_enabled = {}, True, package_name == '@deepseek-ai/dsh'
    for path in patches:
        rows = read(path)
        if not isinstance(rows, list):
            continue
        pending = list(rows)
        while pending:
            row = pending.pop(0)
            if not isinstance(row, dict):
                continue
            if isinstance(row.get('insert'), list):
                pending.extend(row['insert'])
            if row.get('id') == 'skill-badge' or row.get('name') == '@deepseek-ai/dsh-skill-badge':
                badge_enabled = not row.get('disabled', False)
            if row.get('id') == 'skill-filesystem' or row.get('name') == '@deepseek-ai/dsh-skill-filesystem':
                if 'disabled' in row:
                    enabled = not row['disabled']
                if isinstance(row.get('config'), dict):
                    config = row['config']
    result = []
    cache, _ = badge_cache(spec, package_name)
    if badge_enabled and cache and (cache / 'source.json').is_file():
        result.extend(direct_roots(cache, agent_key, label, owner))
    if enabled and config.get('includeDefaultRoots', True):
        for path in [expand(config.get('dshHome'), home, cwd) or native, expand(config.get('agentsHome'), home, cwd) or agents]:
            result.extend(direct_roots(path / 'skills', agent_key, label, owner))
        for project, workspace in project_workspaces(projects, bindings, agent_key):
            for folder in ['.dsh/skills', '.agents/skills']:
                result.extend(direct_roots(workspace / folder, agent_key, label, project))
    for value in config.get('customSkillDirs', []) if enabled and isinstance(config.get('customSkillDirs', []), list) else []:
        path = expand(value, home, cwd)
        if path:
            result.extend(direct_roots(path, agent_key, label, owner))
    bundled = config.get('bundledSkillDir') or (env.get('DSH_BUNDLED_SKILL_DIR') if config.get('includeDefaultRoots', True) else None)
    path = expand(bundled, home, cwd)
    if enabled and path:
        result.extend(direct_roots(path, agent_key, label, owner))
    return tuple(result)


def badge_cache(spec, package_name):
    import hashlib
    from .common import state_root
    from .dsh import dependency
    package = locate(spec, package_name)
    module = dependency(package, '@deepseek-ai/dsh-skill-badge') if package else None
    if not module:
        return None, None
    hasher = hashlib.sha256(module.read_bytes())
    for path in sorted((module.parent.parent / 'assets').rglob('*')):
        if path.is_file() and not path.is_symlink():
            hasher.update(str(path.relative_to(module.parent.parent)).encode())
            hasher.update(path.read_bytes())
    digest = hasher.hexdigest()[:24]
    return state_root(spec) / 'native-inventory/dsh-badge' / digest, module


_locks = {}


async def refresh(spec, package_name):
    import asyncio
    destination, _ = badge_cache(spec, package_name)
    if destination is None:
        return
    async with _locks.setdefault(str(destination), asyncio.Lock()):
        await _refresh(spec, package_name)


async def _refresh(spec, package_name):
    import asyncio
    import json
    import shutil
    import yaml
    from drivers.projection_store import atomic_write_text
    destination, module = badge_cache(spec, package_name)
    if destination is None or (destination / 'source.json').is_file():
        return
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    script = f'''const {{apply}}=await import({json.dumps(module.as_uri())});let provider;
apply({{skills:{{registerProvider:create=>provider=create()}}}});
const output=[];for(const item of await provider.list())output.push(await provider.get(item));console.log(JSON.stringify(output));'''
    proc = await asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
        env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait(); raise ValueError('内置技能读取超时')
    if proc.returncode or len(out) > 2 * 1024 * 1024:
        raise ValueError('内置技能读取失败')
    rows = json.loads(out)
    for row in rows:
        if row.get('name') != 'dsh-badge' or not isinstance(row.get('content'), str):
            raise ValueError('内置技能格式不兼容')
        assets = Path(row['resourceBase']['path']).resolve()
        if assets != module.parent.parent / 'assets':
            raise ValueError('内置技能资源位置不兼容')
        folder = destination / 'dsh-badge'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for source in assets.iterdir():
            if source.is_file() and not source.is_symlink() and source.stat().st_size <= 5 * 1024 * 1024:
                atomic_write_text(folder / source.name, source.read_text()) if source.suffix == '.md' else shutil.copyfile(source, folder / source.name)
        metadata = yaml.safe_dump({'name': row['name'], 'description': row['description']}, allow_unicode=True, sort_keys=False)
        atomic_write_text(folder / 'SKILL.md', '---\n' + metadata + '---\n' + row['content'])
    atomic_write_text(destination / 'source.json', json.dumps({'source': str(module), 'names': [r['name'] for r in rows]}))
