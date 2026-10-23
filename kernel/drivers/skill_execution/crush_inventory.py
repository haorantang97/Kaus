"""Native built-ins are copied locally from the installed loader, never bundled."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from drivers.crush_bridge.skills import inspect
from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from .common import state_root

_locks = {}


def executable(spec):
    command = spec.command
    value = command[command.index('--crush-command') + 1] if '--crush-command' in command else 'crush'
    found = shutil.which(value, path=spec.build_env().get('PATH'))
    return Path(found).resolve() if found else None


def cache_root(spec):
    native = executable(spec)
    if not native:
        return None
    try:
        info = native.stat()
    except OSError:
        return None
    key = hashlib.sha256(f'{native}\0{info.st_mtime_ns}\0{info.st_size}'.encode()).hexdigest()[:24]
    return state_root(spec) / 'native-inventory/crush' / key


async def refresh(spec):
    destination = cache_root(spec)
    if destination is None:
        raise ValueError('原生引擎尚未安装')
    lock = _locks.setdefault(str(destination), asyncio.Lock())
    async with lock:
        if (destination / 'source.json').is_file():
            return
        native = executable(spec)
        with tempfile.TemporaryDirectory(prefix='kaus-crush-catalog-') as temporary:
            root = Path(temporary)
            for folder in ['config', 'data', 'skills', 'workspace']:
                (root / folder).mkdir()
            # No agent initialization or prompt. A loopback placeholder avoids
            # provider discovery and native account/configuration reads entirely.
            config = {'providers': {'local-inventory': {'id': 'local-inventory', 'type': 'openai-compat',
                'name': 'Local inventory', 'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'inventory-only',
                'discover_models': False, 'models': [{'id': 'inventory', 'name': 'inventory', 'context_window': 4096,
                    'default_max_tokens': 512}]}}, 'options': {'disable_default_providers': True, 'disable_provider_auto_update': True}}
            (root / 'config/crush.json').write_text(json.dumps(config))
            (root / 'data/crush.json').write_text(json.dumps({'providers': {'copilot': {'disable': True, 'api_key': 'inventory-only'}}}))
            env = {**spec.build_env(), 'CRUSH_GLOBAL_CONFIG': str(root / 'config'),
                'CRUSH_GLOBAL_DATA': str(root / 'data'), 'CRUSH_SKILLS_DIR': str(root / 'skills'),
                'CRUSH_CACHE_DIR': str(root / 'cache'), 'CRUSH_DISABLE_METRICS': 'true',
                'CRUSH_DISABLE_PROVIDER_AUTO_UPDATE': 'true', 'CRUSH_DISABLE_DEFAULT_PROVIDERS': 'true'}
            rows = await inspect(str(native), root / 'workspace', env, [], include_builtin_content=True)
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            sources = []
            for row in rows:
                if not row['id'].startswith('crush://'):
                    continue
                data = base64.b64decode(row['content'], validate=True)
                if len(data) > 1024 * 1024:
                    raise ValueError('内置技能超过读取限制')
                # No upstream binary/source is redistributed with Kaus. This
                # content-addressed copy remains inside this user's state root.
                digest = hashlib.sha256(data).hexdigest()
                folder = destination / digest
                folder.mkdir(exist_ok=True)
                atomic_write_text(folder / 'SKILL.md', data.decode('utf-8'))
                sources.append({'uri': row['id'], 'sha256': digest, 'name': row['name']})
            atomic_write_text(destination / 'source.json', json.dumps({'native': str(native),
                'binaryHash': hashlib.sha256(native.read_bytes()).hexdigest(), 'sources': sources}, sort_keys=True))


def roots(spec, projects, bindings):
    from .inventory import project_workspaces, _read
    projects = tuple(projects)
    owner = next((p.id for p in projects if p.slug == 'default'), None) or next((p.id for p in projects if not p.parent_project_id), None)
    if not owner:
        return ()
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home()).expanduser()
    config = Path(env.get('XDG_CONFIG_HOME') or home / '.config')
    global_paths = [Path(env['CRUSH_SKILLS_DIR'])] if env.get('CRUSH_SKILLS_DIR') else [
        config / 'crush/skills', config / 'agents/skills', home / '.agents/skills', home / '.claude/skills']
    result = [NativeSkillRoot(path, 'crush', 'Crush', owner) for path in global_paths]
    def extra(path, project, base):
        options = _read(path).get('options')
        values = options.get('skills_paths') if isinstance(options, dict) else None
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, str) or not value:
                continue
            source = home / value[2:] if value.startswith('~/') else Path(value)
            source = source if source.is_absolute() else base / source
            result.append(NativeSkillRoot(source.resolve(), 'crush', 'Crush', project))
    global_config = Path(env.get('CRUSH_GLOBAL_CONFIG') or config / 'crush')
    global_data = Path(env.get('CRUSH_GLOBAL_DATA') or Path(env.get('XDG_DATA_HOME') or home / '.local/share') / 'crush')
    for path in [global_config / 'crush.json', global_data / 'crush.json']:
        extra(path, owner, Path(spec.cwd or home))
    cache = cache_root(spec)
    if cache and (cache / 'source.json').is_file():
        result.append(NativeSkillRoot(cache, 'crush', 'Crush', owner))
    for project, workspace in project_workspaces(projects, bindings, 'crush'):
        for config_name in ['crush.json', '.crush.json', '.crush/crush.json']:
            extra(workspace / config_name, project, workspace)
        for relative in ['.agents/skills', '.crush/skills', '.claude/skills', '.cursor/skills']:
            result.append(NativeSkillRoot(workspace / relative, 'crush', 'Crush', project))
    return tuple(result)
