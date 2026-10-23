"""Qwen's native roots and enabled, installed extension artifacts."""
import fnmatch
from dataclasses import replace
from pathlib import Path
from ._source_inventory import read, expand, global_project, direct_roots
from .inventory import project_workspaces
from .node_package import locate


def active(policy, workspace, legacy=()):
    enabled = policy.get('defaultActivation', 'enabled') == 'enabled'
    value = policy.get('workspaceOverrides', {}).get(str(workspace.resolve()))
    if value in {'enabled', 'disabled'}:
        return value == 'enabled'
    candidate = str(workspace.resolve()).replace('\\', '/').strip('/')
    candidate = '/' + candidate + '/'
    for rule in policy.get('legacyPathRules', legacy):
        if isinstance(rule, str) and fnmatch.fnmatchcase(candidate, rule.lstrip('!')):
            enabled = not rule.startswith('!')
    return enabled


def roots(spec, projects, bindings):
    projects = tuple(projects); owner = global_project(projects)
    if not owner:
        return ()
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home()); cwd = Path(spec.cwd or home)
    base = expand(env.get('QWEN_HOME') or '~/.qwen', home, cwd)
    settings = read(base / 'settings.json'); settings = settings if isinstance(settings, dict) else {}
    skills = settings.get('skills', {}); skills = skills if isinstance(skills, dict) else {}
    disabled = skills.get('disabledLevels', [])
    workspaces = project_workspaces(projects, bindings, 'qwen')
    result = []
    if 'user' not in disabled:
        for path in [base / 'skills', home / '.agents/skills']:
            result.extend(direct_roots(path, 'qwen', 'Qwen Code', owner))
        for value in skills.get('directories', []) if isinstance(skills.get('directories', []), list) else []:
            path = expand(value, home, cwd)
            if path:
                result.extend(direct_roots(path, 'qwen', 'Qwen Code', owner))
    package = locate(spec, '@qwen-code/qwen-code')
    if package and 'bundled' not in disabled:
        result.extend(direct_roots(package / 'bundled', 'qwen', 'Qwen Code', owner))
    for project, workspace in workspaces:
        local = read(workspace / '.qwen/settings.json'); local = local if isinstance(local, dict) else {}
        local_skills = local.get('skills', {}) if isinstance(local.get('skills', {}), dict) else {}
        if 'project' not in local_skills.get('disabledLevels', disabled):
            for relative in ['.qwen/skills', '.agents/skills']:
                result.extend(direct_roots(workspace / relative, 'qwen', 'Qwen Code', project))
        for value in local_skills.get('directories', []) if isinstance(local_skills.get('directories', []), list) else []:
            path = expand(value, home, workspace)
            if path:
                result.extend(direct_roots(path, 'qwen', 'Qwen Code', project))
    if 'extension' in disabled:
        return tuple(result)
    state = read(base / 'extension-store/state.json')
    state_exists = (base / 'extension-store/state.json').exists()
    policies = state.get('extensions', {}) if isinstance(state, dict) and state.get('version') == 2 else {}
    legacy = read(base / 'extensions/extension-enablement.json')
    try:
        folders = [p for p in (base / 'extensions').iterdir() if p.is_dir()]
    except OSError:
        folders = []
    for folder in folders:
        meta = read(folder / '.qwen-extension-install.json')
        artifact = expand(meta.get('source'), home, cwd) if isinstance(meta, dict) and meta.get('type') == 'link' else folder
        if artifact is None:
            continue
        manifest = read(artifact / 'qwen-extension.json')
        if not isinstance(manifest, dict) or not manifest.get('name'):
            manifest = read(artifact / 'plugin.json')
        name = manifest.get('name') if isinstance(manifest, dict) else None
        if not isinstance(name, str) or not name:
            continue
        matches = [p for p in policies.values() if isinstance(p, dict) and p.get('name') == name
                   and not p.get('declarationOnly') and p.get('artifactDirectory', name) == folder.name]
        if state_exists and len(matches) != 1:
            continue
        policy = matches[0] if matches else {}
        legacy_rules = legacy.get(name, {}).get('overrides', []) if isinstance(legacy, dict) and isinstance(legacy.get(name), dict) else []
        global_active = active(policy, home, legacy_rules)
        blocked = [pid for pid, workspace in workspaces if not active(policy, workspace, legacy_rules)]
        targets = [(owner, blocked)] if global_active else [(pid, []) for pid, workspace in workspaces if active(policy, workspace, legacy_rules)]
        for project, excluded in targets:
            for root in direct_roots(artifact / 'skills', 'qwen', 'Qwen Code', project, prefix=name + ':', blocked=excluded):
                # The native store can disable individual extension skills in
                # one workspace without disabling the rest of the extension.
                metadata = read_skill_name(root.path / 'SKILL.md') or root.path.name
                denied = set(root.blocked_project_ids)
                overrides = policy.get('skillWorkspaceOverrides', {})
                for pid, workspace in workspaces:
                    values = overrides.get(str(workspace.resolve()), {}) if isinstance(overrides, dict) else {}
                    if isinstance(values, dict) and values.get(metadata) is False:
                        denied.add(pid)
                if project not in denied:
                    result.append(replace(root, blocked_project_ids=tuple(sorted(denied))))
    return tuple(result)


def read_skill_name(path):
    import yaml
    try:
        text = path.read_text()[:65536]
        data = yaml.safe_load(text[4:].split('\n---', 1)[0]) if text.startswith('---\n') else {}
        return data.get('name') if isinstance(data, dict) else None
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
