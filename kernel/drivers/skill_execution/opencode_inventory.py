"""Native OpenCode-family sources and locally extracted embedded Skill text."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import yaml

from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from ._source_inventory import read, expand, global_project
from .common import state_root
from .inventory import project_workspaces
from .opencode import inspect

_locks = {}


def cache_root(spec, agent):
    native = shutil.which(spec.command[0], path=spec.build_env().get('PATH'))
    if not native:
        return None
    path = Path(native).resolve(); info = path.stat()
    from .node_package import locate
    package = locate(spec, '@kilocode/cli' if agent == 'kilo' else 'opencode-ai')
    manifest = (package / 'package.json').read_bytes() if package else b''
    key = hashlib.sha256(f'{path}\0{info.st_mtime_ns}\0{info.st_size}'.encode() + manifest).hexdigest()[:24]
    return state_root(spec) / 'native-inventory' / agent / key


async def refresh(spec, *, agent, prefix):
    destination = cache_root(spec, agent)
    if destination is None:
        raise ValueError('原生引擎尚未安装')
    async with _locks.setdefault(str(destination), asyncio.Lock()):
        if (destination / 'source.json').is_file():
            return
        with tempfile.TemporaryDirectory(prefix='kaus-native-skills-') as temporary:
            home = Path(temporary)
            workspace = home / 'workspace'; workspace.mkdir()
            # Query bundled resources in an empty config home. No account,
            # session, user plugin, remote URL or user configuration is loaded.
            env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'), 'XDG_CACHE_HOME': str(home / 'cache'),
                   prefix + '_CONFIG_DIR': str(home / 'config' / agent),
                   prefix + '_CONFIG_CONTENT': json.dumps({'plugin': [], 'skills': {'paths': [], 'urls': []}}),
                   prefix + '_DISABLE_AUTOUPDATE': 'true', prefix + '_DISABLE_CLAUDE_CODE': 'true',
                   prefix + '_DISABLE_EXTERNAL_SKILLS': 'true'}
            clean = spec.model_copy(update={'env': {'PATH': spec.build_env().get('PATH', ''), **env}, 'inherit_env': False})
            rows = await inspect(spec.command[0], clean, str(workspace), 'skill')
            if not isinstance(rows, list):
                raise ValueError('内置技能格式不兼容')
            sources = []
            for row in rows:
                if not isinstance(row, dict) or row.get('location') != 'builtin':
                    continue
                if not isinstance(row.get('content'), str) or not isinstance(row.get('name'), str):
                    raise ValueError('内置技能格式不兼容')
                body = row['content']
                if len(body.encode()) > 1024 * 1024:
                    raise ValueError('内置技能超过读取限制')
                digest = hashlib.sha256((row['name'] + '\0' + body).encode()).hexdigest()
                folder = destination / digest
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                header = yaml.safe_dump({'name': row['name'], 'description': row.get('description', '')}, allow_unicode=True, sort_keys=False)
                atomic_write_text(folder / 'SKILL.md', '---\n' + header + '---\n' + body)
                sources.append({'name': row['name'], 'sha256': digest})
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_text(destination / 'source.json', json.dumps({'sources': sources}, sort_keys=True))


def roots(spec, projects, bindings, *, agent, label, prefix):
    projects = tuple(projects); owner = global_project(projects)
    if not owner:
        return ()
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home()); cwd = Path(spec.cwd or home)
    config = expand(env.get('XDG_CONFIG_HOME') or '~/.config', home, cwd)
    own = expand(env.get(prefix + '_CONFIG_DIR') or str(config / agent), home, cwd)
    directories = ['.opencode'] if agent == 'opencode' else ['.kilo', '.kilocode']
    external = [] if env.get(prefix + '_DISABLE_EXTERNAL_SKILLS', '').lower() in {'true', '1'} else ['.agents']
    if env.get(prefix + '_DISABLE_CLAUDE_CODE', '').lower() not in {'true', '1'} and env.get(prefix + '_DISABLE_CLAUDE_CODE_SKILLS', '').lower() not in {'true', '1'}:
        external.append('.claude')
    result = []
    def add(path, project):
        result.append(NativeSkillRoot(path, agent, label, project, nested_skills=True))
    for base in [own, *[home / folder for folder in external + directories]]:
        for child in ['skill', 'skills'] if base == own or base.name in directories else ['skills']:
            add(base / child, owner)
    def configured(path, project, workspace):
        data = read(path)
        add_config(data, project, workspace)
    def add_config(data, project, workspace):
        value = data.get('skills') if isinstance(data, dict) else None
        paths = value.get('paths', []) if isinstance(value, dict) else []
        for raw in paths if isinstance(paths, list) else []:
            resolved = expand(raw, home, workspace)
            if resolved:
                add(resolved, project)
    filenames = [agent + '.json', agent + '.jsonc']
    for filename in filenames:
        configured(own / filename, owner, cwd)
    explicit = expand(env.get(prefix + '_CONFIG'), home, cwd)
    if explicit:
        configured(explicit, owner, cwd)
    try:
        add_config(json.loads(env.get(prefix + '_CONFIG_CONTENT') or '{}'), owner, cwd)
    except ValueError:
        pass
    for project, workspace in project_workspaces(projects, bindings, agent):
        for folder in directories + external:
            for child in ['skill', 'skills'] if folder in directories else ['skills']:
                add(workspace / folder / child, project)
            for filename in filenames:
                configured(workspace / folder / filename, project, workspace)
        for filename in filenames:
            configured(workspace / filename, project, workspace)
    cache = cache_root(spec, agent)
    if cache and (cache / 'source.json').is_file():
        add(cache, owner)
    return tuple(result)
