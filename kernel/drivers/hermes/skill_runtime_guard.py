"""Process-local skill loader policy for Kaus-owned Hermes execution homes.

This module runs inside the installed Hermes interpreter. It deliberately has
no Kaus package dependencies and never modifies the installed package. Plugin
tools/hooks remain registered; only their skill registration/read surface is
restricted. This is a loader boundary, not a Python/plugin security sandbox.
"""
from __future__ import annotations

import inspect
import hashlib
import json
import os
from pathlib import Path

MANIFEST = ".kaus-skills.json"
POLICY_VERSION = 1
PROMPT_LOADER_HASH = '41caed92f78a0754012fde288d7d12e825a366dd91856ec05bf7752cfbba4e0b'


def install(home: Path, scope: Path | None = None):
    home = Path(home).resolve()
    try:
        from .skill_snapshots import ENV_KEY, pin_session, validate_scope
    except ImportError:
        from skill_snapshots import ENV_KEY, pin_session, validate_scope
    configured = scope or os.environ.get(ENV_KEY)
    scope = validate_scope(home, Path(configured)) if configured else pin_session(home, 'process:' + str(os.getpid()))
    marker_path, manifest_path = home / ".kaus-scope.json", scope / MANIFEST
    if marker_path.is_symlink() or manifest_path.is_symlink():
        raise RuntimeError("Kaus skill policy must be owned by its execution home")
    marker = json.loads(marker_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    policy = manifest.get("runtimePolicy", {})
    if policy != {"version": POLICY_VERSION, "bindingId": marker.get("bindingId"),
                  "projectId": marker.get("projectId")}:
        raise RuntimeError("Kaus skill policy version or ownership does not match")
    skills = scope / "skills"
    if skills.is_symlink() or not skills.is_dir():
        raise RuntimeError("Kaus skills must use a private directory")

    def private_path(path):
        try:
            path = Path(path)
            resolved = path.resolve()
            return resolved if resolved.is_relative_to(skills) and path.is_file() else None
        except (OSError, ValueError):
            return None

    sources = {}
    plugin_destinations = {}
    visible_names = {}
    for entry in manifest.get("entries", {}).values():
        destination = private_path(skills / entry["directory"] / "SKILL.md")
        if destination is None:
            raise RuntimeError("Kaus selected skill is missing or outside its private directory")
        if entry.get("name"):
            visible_names[entry["name"]] = destination.parent
        source = entry.get("sourcePath")
        plugin_name = entry.get("pluginName")
        if source and plugin_name:
            sources[(plugin_name, str(Path(source).resolve()))] = destination
            plugin_destinations[plugin_name] = destination

    def selected_path(path, qualified_name):
        try:
            resolved = Path(path).resolve()
            selected = sources.get((qualified_name, str(resolved)))
            if selected is not None:
                return selected
            expected = plugin_destinations.get(qualified_name)
            return expected if expected == resolved and private_path(path) else None
        except (OSError, ValueError):
            return None

    # Check all hooks before installing any: a changed upstream API must stop
    # the managed launch instead of accidentally running with partial guards.
    from agent import skill_utils
    from hermes_cli import plugins
    from hermes_constants import get_bundled_skills_dir
    from tools import skills_tool, skill_manager_tool
    required_utils = ("get_all_skills_dirs", "get_project_skills_dirs", "get_scan_ordered_skills_dirs", "get_skill_create_dir",
                      "iter_skill_index_files", "get_disabled_skill_names")
    required_manager = ("find_plugin_skill", "list_plugin_skills", "list_plugin_skill_metadata")
    if (any(not callable(getattr(module, name, None)) for module, name in (
                (skills_tool, "_skills_dir"), (skills_tool, "_is_skill_disabled"),
                (skill_manager_tool, "_skills_dir"), (skill_manager_tool, "_find_skill")))
            or any(not callable(getattr(skill_utils, name, None)) for name in required_utils)
            or any(not callable(getattr(plugins.PluginManager, name, None)) for name in required_manager)
            or not {"name", "path"} <= set(inspect.signature(plugins.PluginContext.register_skill).parameters)
            or not {"kind", "key", "release"} <= set(inspect.signature(plugins.PluginRegistration).parameters)):
        raise RuntimeError("Installed Hermes does not support Kaus skill loader isolation")

    if getattr(plugins.PluginContext.register_skill, "_kaus_skill_home", None) == str(scope):
        return
    if getattr(plugins.PluginContext.register_skill, "_kaus_skill_home", None):
        raise RuntimeError("A Hermes process cannot switch Kaus skill execution homes")

    from agent import prompt_builder
    original_prompt = prompt_builder.build_skills_system_prompt
    if (hashlib.sha256(inspect.getsource(original_prompt).encode()).hexdigest() != PROMPT_LOADER_HASH
            or 'skills_dir_override' not in inspect.signature(original_prompt).parameters):
        raise RuntimeError('Installed Hermes skill prompt loader is not verified')

    original_iterator = skill_utils.iter_skill_index_files
    original_register = plugins.PluginContext.register_skill
    original_find = plugins.PluginManager.find_plugin_skill
    original_metadata = plugins.PluginManager.list_plugin_skill_metadata

    def only_private_dirs():
        return [skills]

    def no_project_dirs():
        return []

    def iter_private_files(directory, filename):
        try:
            if not Path(directory).resolve().is_relative_to(skills):
                return
        except (OSError, ValueError):
            return
        for path in original_iterator(directory, filename):
            resolved = private_path(path)
            if resolved is not None and not any(resolved.is_relative_to(p.parent) for p in plugin_destinations.values()):
                yield path

    def register_skill(context, name, path, *args, **kwargs):
        namespace = context.manifest.skill_namespace or context.manifest.name
        selected = selected_path(path, namespace + ":" + name)
        if selected is None:
            # A real inert registration handle keeps plugin teardown contracts
            # intact without disabling its tools, hooks, or other registrations.
            handle = plugins.PluginRegistration(kind="skill", key=name, release=lambda: None)
            handle.dispose()
            return handle
        return original_register(context, name, selected, *args, **kwargs)

    register_skill._kaus_skill_home = str(scope)

    def find_plugin_skill(manager, name):
        found = original_find(manager, name)
        return selected_path(found, name) if found else None

    def list_plugin_skill_metadata(manager):
        return [entry for entry in original_metadata(manager)
                if find_plugin_skill(manager, entry["name"]) is not None]

    def list_plugin_skills(manager, namespace):
        prefix = namespace + ":"
        return sorted(entry["name"][len(prefix):] for entry in list_plugin_skill_metadata(manager)
                      if entry["name"].startswith(prefix))

    # Patch before native prompt/tool modules import these aliases. Also update
    # aliases already imported while loading the upstream plugin module.
    replacements = {name: (getattr(skill_utils, name), replacement) for name, replacement in (
        ("get_all_skills_dirs", only_private_dirs),
        ("get_project_skills_dirs", no_project_dirs),
        ("get_scan_ordered_skills_dirs", only_private_dirs),
        ("iter_skill_index_files", iter_private_files),
        ("get_skill_create_dir", lambda: skills),
        ("get_disabled_skill_names", lambda *args, **kwargs: set()),
    )}
    import sys
    for module_name, module in tuple(sys.modules.items()):
        if module and module_name.startswith(("agent.", "tools.", "hermes_cli.")):
            for name, (original, replacement) in replacements.items():
                if getattr(module, name, None) is original:
                    setattr(module, name, replacement)
    # The native writer computes its default from HERMES_HOME at call time.
    # Keep state/memory there, but route both create and edit to this conversation.
    for module in (skills_tool, skill_manager_tool):
        if not callable(getattr(module, "_skills_dir", None)):
            raise RuntimeError("Installed Hermes skill writer cannot select a runtime scope")
        module.SKILLS_DIR = skills
        module._skills_dir = lambda: skills
    # Newer native prompt assembly explicitly passes <session-home>/skills.
    # Route that explicit root to the same immutable conversation selection as
    # Skill tools; otherwise the private iterator hides even selected entries.
    def scoped_prompt(*args, **kwargs):
        bound = inspect.signature(original_prompt).bind_partial(*args, **kwargs)
        bound.arguments['skills_dir_override'] = skills
        return original_prompt(*bound.args, **bound.kwargs)
    for module in tuple(sys.modules.values()):
        if module and getattr(module, 'build_skills_system_prompt', None) is original_prompt:
            module.build_skills_system_prompt = scoped_prompt
    skills_tool._is_skill_disabled = lambda *args, **kwargs: False
    original_writer_find = skill_manager_tool._find_skill
    def find_writable_skill(name):
        folder = visible_names.get(name)
        if folder and private_path(folder / "SKILL.md"):
            return {"path": folder}
        return original_writer_find(name)
    skill_manager_tool._find_skill = find_writable_skill
    plugins.PluginContext.register_skill = register_skill
    plugins.PluginManager.find_plugin_skill = find_plugin_skill
    plugins.PluginManager.list_plugin_skill_metadata = list_plugin_skill_metadata
    plugins.PluginManager.list_plugin_skills = list_plugin_skills
    # Hermes' CLI seeds bundled skills on startup, even when skills discovery
    # is disabled. Use its existing packaged-source override to keep that sync
    # from restoring unselected skills. Selected bundled files are already in
    # the private tree; the installed source and its config are untouched.
    empty_bundled = home / ".kaus-empty-bundled-skills"
    if empty_bundled.is_symlink():
        raise RuntimeError("Kaus bundled source override must be a private directory")
    empty_bundled.mkdir(exist_ok=True)
    if any(empty_bundled.iterdir()):
        raise RuntimeError("Kaus bundled source override must stay empty")
    os.environ["HERMES_BUNDLED_SKILLS"] = str(empty_bundled)
    if Path(get_bundled_skills_dir()).resolve() != empty_bundled:
        raise RuntimeError("Installed Hermes cannot restrict its bundled skill source")
