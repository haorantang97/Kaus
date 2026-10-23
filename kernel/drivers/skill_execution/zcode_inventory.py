"""Read-only configured sources plus a versioned native built-in catalogue."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from .common import state_root
from .inventory import _read, project_workspaces

_locks = {}


def cache_path(spec):
    from .zcode import native_command, bundle_path
    executable = native_command(spec)
    module = bundle_path(executable) if executable else None
    if not module:
        return None
    try:
        stat = module.stat()
    except OSError:
        return None
    key = hashlib.sha256(f'{module}\0{stat.st_mtime_ns}\0{stat.st_size}'.encode()).hexdigest()[:24]
    return state_root(spec) / 'native-inventory/zcode' / (key + '.json')


async def plugin_catalog(spec, executable, cwd):
    from drivers.zcode_bridge.native import Native
    async def deny_request(method, params):
        if method == 'session/requestRuntimePreferences':
            return {'nativeSearchEnhancementsEnabled': False, 'memoryEnabled': False,
                    'askUserQuestionAutoResolutionEnabled': False, 'modelContextBudgetStrategy': 'preflight-v1'}
        raise ValueError('来源扫描不能执行工具')
    native = Native((executable, 'app-server', '--stdio'), lambda *args: None,
        deny_request, lambda *args: None, env=spec.build_env(), cwd=str(cwd))
    try:
        await native.start()
        workspace = {'workspacePath': str(cwd), 'workspaceKey': str(cwd)}
        snapshot = await native.call('session/create', {'workspace': workspace,
            'mcpServers': [], 'mode': 'build', 'persistence': 'immediate', 'titleGenerationEnabled': False}, timeout=20)
        data = await native.call('skills/referenceCatalog',
            {'sessionId': snapshot['session']['sessionId'], 'workspace': workspace}, timeout=20)
        return data.get('skills', [])
    finally:
        await native.close()


async def refresh(spec):
    from .zcode import native_command, native_catalog
    target = cache_path(spec)
    if not target:
        raise ValueError('原生技能入口不可用')
    async with _locks.setdefault(str(target), asyncio.Lock()):
        if target.is_file():
            return
        # Native discovery can migrate its own config; use a synthetic home so
        # catalogue enumeration never mutates the user's files or starts plugins.
        home = target.parent / target.stem
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        isolated = spec.with_env(HOME=str(home), ZCODE_DATA_BASE_DIR=str(home),
            ZCODE_STORAGE_DIR=str(home / 'storage'), ZCODE_SESSION_DB_PATH=str(home / 'sessions.sqlite'),
            XDG_CONFIG_HOME=str(home / 'config'), XDG_DATA_HOME=str(home / 'data'))
        executable = native_command(spec)
        rows = await native_catalog(executable, isolated, home)
        rows += await plugin_catalog(isolated, executable, home)
        result = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                raise ValueError('原生技能目录格式不兼容')
            path = Path(row['path'])
            if path.name != 'SKILL.md' or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError('原生技能来源不可读取')
            result.append({**row, 'sourceHash': hashlib.sha256(path.read_bytes()).hexdigest()})
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = list({row['path']: row for row in result}.values())
        atomic_write_text(target, json.dumps({'skills': result}, sort_keys=True))


def _path(value, base, home):
    if not isinstance(value, str) or not value:
        return None
    path = home / value[2:] if value.startswith('~/') else Path(value)
    try:
        return (path if path.is_absolute() else base / path).resolve()
    except (OSError, RuntimeError):
        return None


def _project_bases(workspace):
    values = [workspace, *workspace.parents]
    stop = next((i for i, path in enumerate(values) if (path / '.git').exists()), None)
    return values[:stop + 1] if stop is not None else [workspace]


def _enabled(config):
    plugins = config.get('plugins')
    value = plugins.get('enabledPlugins') if isinstance(plugins, dict) else None
    return value if isinstance(value, dict) else {}


def roots(spec, projects, bindings):
    projects = tuple(projects)
    owner = next((p.id for p in projects if p.slug == 'default'), None)
    owner = owner or next((p.id for p in projects if not p.parent_project_id), None)
    if owner is None:
        return ()
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home()).expanduser().resolve()
    user = _read(home / '.zcode/cli/config.json')
    workspaces = project_workspaces(projects, bindings, 'zcode')
    result = [NativeSkillRoot(home / relative, 'zcode', 'ZCode', owner)
              for relative in ('.zcode/skills', '.agents/skills')]
    overrides = {}
    for project, workspace in workspaces:
        current = {}
        for base in reversed(_project_bases(workspace)):
            for relative in ('.zcode/skills', '.agents/skills'):
                result.append(NativeSkillRoot(base / relative, 'zcode', 'ZCode', project))
            config = _read(base / '.zcode/config.json')
            for key in ('skills', 'plugins'):
                value = config.get(key)
                if isinstance(value, dict):
                    current[key] = {**current.get(key, {}), **value}
        overrides[project, workspace] = current

    def configured(config, project, base, inherited=None):
        skills = config.get('skills', {})
        for raw in skills.get('roots', []) if isinstance(skills, dict) and isinstance(skills.get('roots', []), list) else []:
            path = _path(raw, base, home)
            if path:
                result.append(NativeSkillRoot(path, 'zcode', 'ZCode', project))
        plugins = config.get('plugins', {})
        if not isinstance(plugins, dict):
            return
        local_enabled = plugins.get('enabledPlugins')
        enabled = {**(inherited or {}), **(local_enabled if isinstance(local_enabled, dict) else {})}
        for raw in plugins.get('dirs', []) if isinstance(plugins.get('dirs', []), list) else []:
            package = _path(raw, base, home)
            if package:
                add_plugin(package, project, enabled, default=True)

    def add_plugin(package, project, enabled, *, default=False, identity=None, blocked=()):
        manifest = _read(package / '.zcode-plugin/plugin.json')
        name = manifest.get('name')
        if not isinstance(name, str) or not name:
            return
        key = identity or name + '@inline'
        if enabled.get(key, default) is not True:
            return
        # Native plugin convention plus explicitly declared relative skill dirs.
        raw = manifest.get('skills', [])
        extra = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for relative in ['skills', *extra]:
            path = _path(relative, package, home)
            if path and path.is_relative_to(package) and path.is_dir():
                result.append(NativeSkillRoot(path, 'zcode', 'ZCode', project,
                    name_prefix=name + ':', blocked_project_ids=tuple(blocked)))

    plugins = user.get('plugins') if isinstance(user.get('plugins'), dict) else {}
    enabled = plugins.get('enabledPlugins') if isinstance(plugins.get('enabledPlugins'), dict) else {}
    configured(user, owner, Path(spec.cwd or home))
    for (project, workspace), config in overrides.items():
        configured(config, project, workspace, enabled)
    storage_config = user.get('storage') if isinstance(user.get('storage'), dict) else {}
    storage = _path(env.get('ZCODE_STORAGE_DIR') or storage_config.get('dir') or '~/.zcode', home, home)
    storage = storage or home / '.zcode'
    storage = storage if storage.name == 'cli' else storage / 'cli'
    records = _read(storage / 'plugins/installed_plugins.json').get('plugins', [])
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        identity = record.get('id')
        package = _path(record.get('installPath'), storage, home)
        if not isinstance(identity, str) or package is None:
            continue
        if record.get('scope', 'user') == 'user':
            blocked = [pid for (pid, _), value in overrides.items()
                       if _enabled(value).get(identity) is False]
            add_plugin(package, owner, enabled, identity=identity, blocked=blocked)
        else:
            for (pid, workspace), value in overrides.items():
                selection = {**enabled, **_enabled(value)}
                # Workspace-scoped records lack a trustworthy project path in
                # some versions; explicit workspace enablement is required.
                if _enabled(value).get(identity) is True:
                    add_plugin(package, pid, selection, identity=identity)
    cache = cache_path(spec)
    for row in _read(cache).get('skills', []) if cache else []:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            continue
        path = Path(row['path'])
        plugin = row.get('pluginId')
        if not plugin and isinstance(row.get('plugin'), dict):
            plugin = row['plugin'].get('id')
        # Current catalogue returns pluginName rather than pluginId.
        name = row.get('pluginName')
        identity = plugin or (name + '@zcode-plugins-official' if isinstance(name, str) else None)
        suppressed = plugins.get('suppressedBuiltins')
        suppressed = suppressed if isinstance(suppressed, list) else []
        if identity and (enabled.get(identity) is False or identity in suppressed):
            continue
        blocked = [pid for (pid, _), value in overrides.items() if identity and
                   _enabled(value).get(identity) is False]
        if path.name == 'SKILL.md' and path.is_file():
            result.append(NativeSkillRoot(path.parent, 'zcode', 'ZCode', owner,
                blocked_project_ids=tuple(blocked)))
    return tuple(result)
