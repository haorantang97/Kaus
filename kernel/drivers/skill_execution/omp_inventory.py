"""Read OMP's native declarations and enabled plugin registries without starting it."""
from __future__ import annotations

from dataclasses import replace
import fnmatch
import os
import re
from pathlib import Path

from ._source_inventory import read as read_value, expand, global_project, direct_roots
from .inventory import project_workspaces



def read(path):
    value = read_value(path)
    return value if isinstance(value, dict) else {}


def merge(left, right):
    result = dict(left)
    for key, value in right.items() if isinstance(right, dict) else ():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def flags(command, names):
    values = []
    for i, arg in enumerate(command):
        if arg in names and i + 1 < len(command):
            values.append(command[i + 1])
        elif any(arg.startswith(name + '=') for name in names):
            values.append(arg.split('=', 1)[1])
    return values


def native_settings(base):
    for filename in ('config.yml', 'config.yaml'):
        if (base / filename).exists():
            data = read(base / filename)
            return data if isinstance(data, dict) else {}
    data = read(base / 'settings.json')
    return data if isinstance(data, dict) else {}


def plugins(base):
    """Only dependency/link registrations with an enabled native manifest."""
    package = read(base / 'package.json'); runtime = read(base / 'omp-plugins.lock.json')
    deps = package.get('dependencies', {}) if isinstance(package, dict) else {}
    states = runtime.get('plugins', {}) if isinstance(runtime, dict) else {}
    deps = deps if isinstance(deps, dict) else {}; states = states if isinstance(states, dict) else {}
    found = {}
    for name in [*deps, *states]:
        if not isinstance(name, str) or '..' in name.split('/') or name.startswith('/'):
            continue
        path = base / 'node_modules' / name
        if (base / 'package.json').is_file() and name not in deps and not path.is_symlink():
            continue
        state = states.get(name)
        if isinstance(state, dict) and not state.get('enabled'):
            continue
        meta = read(path / 'package.json')
        if isinstance(meta, dict) and (isinstance(meta.get('omp'), dict) or isinstance(meta.get('pi'), dict)):
            found[name] = path
    return found



def ancestors(workspace, home):
    result = []
    for folder in (workspace, *workspace.parents):
        if folder == home or folder == folder.parent or len(result) >= 24:
            break
        result.append(folder)
        if (folder / '.git').exists():
            break
    return result


def locked_skills(lock_path, agent):
    lock = read(lock_path)
    rows = lock.get('skills', {}) if isinstance(lock, dict) and lock.get('version') == 1 else {}
    result = []
    for name, item in rows.items() if isinstance(rows, dict) else ():
        if not isinstance(name, str) or not re.fullmatch(r'@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*', name) or '..' in name:
            continue
        version = item.get('version') if isinstance(item, dict) else None
        if not isinstance(version, str) or '..' in version or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?', version):
            continue
        root = agent.parent / 'skillshare' / name / version
        try:
            if (root / '.skillshare-integrity').read_text().strip() == item.get('integrity'):
                result.append(root)
        except OSError:
            pass
    return result


def marketplaces(base, home, workspace, providers):
    result = {}
    claude_enabled = 'claude-plugins' in providers
    claude = home / '.claude'
    enable = read(claude / 'settings.json').get('enabledPlugins', {})
    enable = dict(enable) if isinstance(enable, dict) else {}
    for filename in ['settings.json', 'settings.local.json']:
        values = read(workspace / '.claude' / filename).get('enabledPlugins', {})
        if isinstance(values, dict): enable.update(values)
    inputs = [(claude / 'plugins/installed_plugins.json', 'claude'), (base / 'plugins/installed_plugins.json', 'user')]
    for folder in reversed(ancestors(workspace, home)):
        inputs.append((folder / '.omp/plugins/installed_plugins.json', 'project'))
    for path, origin in inputs:
        rows = read(path).get('plugins', {})
        for key, entries in rows.items() if isinstance(rows, dict) else ():
            if not isinstance(entries, list) or not entries:
                continue
            accepted = []
            for item in entries:
                if not isinstance(item, dict) or item.get('enabled') is False:
                    continue
                if origin == 'claude':
                    if enable.get(key) is False or item.get('scope', 'user') == 'user' and not claude_enabled:
                        continue
                    if item.get('scope') in {'local', 'project'} and enable.get(key) is not True:
                        target = expand(item.get('projectPath'), home, workspace)
                        if target not in ancestors(workspace, home): continue
                package = expand(item.get('installPath'), home, workspace)
                if package and package.is_dir():
                    accepted.append((package, 'project' if origin == 'project' or origin == 'claude' and item.get('scope') in {'local', 'project'} else 'user'))
            if origin != 'project' or accepted:
                result[key] = accepted
    return [item for items in result.values() for item in items]

def roots(spec, projects, bindings):
    projects = tuple(projects); owner = global_project(projects)
    if not owner:
        return ()
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home()); cwd = Path(spec.cwd or home)
    base = home / (env.get('PI_CONFIG_DIR') or '.omp')
    profile = next(iter(flags(spec.command, {'--profile'})), None) or env.get('OMP_PROFILE') or env.get('PI_PROFILE')
    agent = base / 'profiles' / profile / 'agent' if profile and profile != 'default' else expand(env.get('PI_CODING_AGENT_DIR') or str(base / 'agent'), home, cwd)
    global_config = native_settings(agent)
    overlay_paths = [v for v in env.get('PI_CONFIG_FILES', '').split(os.pathsep) if v] + flags(spec.command, {'--config'})
    explicit_extensions = flags(spec.command, {'--extension', '-e'})
    no_extensions = '--no-extensions' in spec.command
    workspaces = project_workspaces(projects, bindings, 'omp')
    user_plugins = plugins(base / 'plugins') if not no_extensions else {}
    result = []

    def configured(workspace, project=False):
        config = global_config
        if project:
            # These are the native settings providers' project inputs; YAML
            # native config has higher precedence than legacy JSON.
            for relative in ['.claude/settings.json', '.pi/settings.json', '.omp/settings.json', '.omp/config.yml']:
                config = merge(config, read(workspace / relative))
        for value in overlay_paths:
            path = expand(value, home, workspace)
            config = merge(config, read(path))
        return config

    def collect(config, workspace, project, *, user=False):
        skills = config.get('skills', {}); skills = skills if isinstance(skills, dict) else {}
        if skills.get('enabled') is False or '--no-skills' in spec.command:
            return []
        sources = []
        def add(path, *, enabled=True, prefix='', generated=False):
            if enabled:
                sources.extend(replace(item, generated=generated) for item in direct_roots(path, 'omp', 'OMP', project, prefix=prefix))
        providers = config.get('enabledProviders', [])
        providers = providers if isinstance(providers, list) else []
        # Global sources have global ownership. Project sources retain their
        # own placement, and native project overrides can suppress inheritance.
        if user:
            add(agent / 'skills', enabled=skills.get('enablePiUser', True))
            add(agent / 'managed-skills', generated=True)
            for root in locked_skills(agent / 'skills.lock.json', agent): add(root)
            for provider, folder, toggle in [('claude', '.claude', 'enableClaudeUser'), ('codex', '.codex', 'enableCodexUser'), ('agents', '.agents', 'enableAgentsUser'), ('agents', '.agent', 'enableAgentsUser')]:
                add(home / folder / 'skills', enabled=skills.get(toggle, provider == 'agents') or provider in providers)
        else:
            for ancestor in ancestors(workspace, home):
                for folder, toggle in [('.omp', 'enablePiProject'), ('.pi', 'enablePiProject'), ('.claude', 'enableClaudeProject'), ('.agents', 'enableAgentsProject'), ('.agent', 'enableAgentsProject')]:
                    add(ancestor / folder / 'skills', enabled=skills.get(toggle, True))
                add(ancestor / '.opencode/skills'); add(ancestor / '.github/skills')
                for root in locked_skills(ancestor / '.omp/skills.lock.json', agent): add(root)
        for value in skills.get('customDirectories', []) if isinstance(skills.get('customDirectories', []), list) else []:
            path = expand(value, home, workspace)
            if path:
                add(path)
        extensions = explicit_extensions + ([] if no_extensions else config.get('extensions', []) if isinstance(config.get('extensions', []), list) else [])
        for value in extensions:
            path = expand(value, home, workspace)
            if path and path.is_dir():
                add(path / 'skills')
        registry = user_plugins if user else plugins(workspace / '.omp/plugins') if not no_extensions else {}
        disabled = read(workspace / '.omp/plugin-overrides.json').get('disabled', [])
        for name, path in registry.items():
            if name not in disabled:
                add(path / 'skills')
        for package, scope in marketplaces(base, home, workspace, providers):
            if (scope == 'user') != user:
                continue
            standard = read(package / 'plugin.json')
            manifest = read(package / '.claude-plugin/plugin.json')
            add(package / 'skills')
            if not standard:
                paths = manifest.get('skills', []) if isinstance(manifest, dict) else []
                for value in [paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []:
                    path = expand(value, home, package)
                    if path and path.is_relative_to(package.resolve()): add(path)
        ignored = skills.get('ignoredSkills', []); included = skills.get('includeSkills', [])
        cli_include = flags(spec.command, {'--skills'})
        if cli_include:
            included = cli_include[-1].split(',')
        disabled_names = {value[6:] for value in config.get('disabledExtensions', []) if isinstance(value, str) and value.startswith('skill:')}
        def admitted(item):
            from .qwen_inventory import read_skill_name
            name = read_skill_name(item.path / 'SKILL.md') or item.path.name
            return name not in disabled_names and not any(fnmatch.fnmatchcase(name, pattern) for pattern in ignored if isinstance(pattern, str)) and (not included or any(fnmatch.fnmatchcase(name, pattern) for pattern in included if isinstance(pattern, str)))
        return [item for item in sources if admitted(item)]

    globals_ = collect(configured(cwd), cwd, owner, user=True)
    for root in globals_:
        blocked = []
        for project, workspace in workspaces:
            allowed = {item.path for item in collect(configured(workspace, True), workspace, owner, user=True)}
            if root.path not in allowed:
                blocked.append(project)
        result.append(replace(root, blocked_project_ids=tuple(sorted(set(blocked)))))
    for project, workspace in workspaces:
        result.extend(collect(configured(workspace, True), workspace, project))
    return tuple(result)
