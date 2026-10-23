"""Static native skill sources. Never import plugins or infer cached enablement."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from drivers.skill_inventory import NativeSkillRoot


def _read(path: Path, *, toml=False):
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        parsed = tomllib.loads(path.read_text()) if toml else json.loads(path.read_text())
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _resolved(path):
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def project_workspaces(projects, bindings, agent_key):
    """Return registered workspace ownership; no source discovery or process IO."""
    projects = tuple(projects)
    known = {p.id for p in projects}
    result = {(p.id, _resolved(p.workspace_root)) for p in projects if p.workspace_root}
    for binding in bindings:
        if binding.project_id in known and binding.backend_key == agent_key:
            path = binding.runtime_config.get('workspace_root')
            if isinstance(path, str) and path:
                result.add((binding.project_id, _resolved(path)))
    return tuple(sorted((project, path) for project, path in result if path))


def _plugin_skill_roots(package, manifest, *, agent, label, project, name, blocked=()):
    paths = manifest.get("skills", [])
    paths = [paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []
    # Claude's manifest paths supplement its conventional skills directory.
    if agent == "claude-code":
        paths = ["skills", *paths]
    result, seen = [], set()
    for relative in paths:
        if not isinstance(relative, str):
            continue
        path = _resolved(package / relative)
        if path and path.is_relative_to(package) and path.is_dir() and path not in seen:
            seen.add(path)
            result.append(NativeSkillRoot(path, agent, label, project, name_prefix=name + ":", blocked_project_ids=tuple(blocked)))
    return result


def additional_roots(projects, *, home, env, diagnostics=None, bindings=()):
    """Return source roots with explicit project ownership, without DB writes.

    diagnostics contains codes and plugin identifiers, never configuration bodies.
    Unknown cached plugins and ambiguous versions are intentionally not seeded.
    """
    notes = diagnostics if diagnostics is not None else []
    projects = tuple(projects)
    global_id = next((p.id for p in projects if p.slug == "default"), None)
    global_id = global_id or next((p.id for p in projects if not p.parent_project_id), None)
    if not global_id:
        return ()
    codex = Path(env.get("CODEX_HOME") or home / ".codex").expanduser()
    claude = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    roots = [NativeSkillRoot(home / ".agents/skills", "codex", "Codex", global_id)]
    known_projects = {p.id for p in projects}
    workspaces = {(p.id, _resolved(p.workspace_root), agent) for p in projects if p.workspace_root
                  for agent in ("codex", "claude-code")}
    for binding in bindings:
        if binding.project_id in known_projects and binding.backend_key in {"codex", "claude-code"}:
            path = binding.runtime_config.get("workspace_root")
            if isinstance(path, str) and path:
                workspaces.add((binding.project_id, _resolved(path), binding.backend_key))
    workspaces = sorted((pid, path, agent) for pid, path, agent in workspaces if path)
    for pid, workspace, agent in workspaces:
        folder, label = (".agents/skills", "Codex") if agent == "codex" else (".claude/skills", "Claude Code")
        roots.append(NativeSkillRoot(workspace / folder, agent, label, pid))

    def note(code, plugin):
        notes.append({"code": code, "plugin": plugin})

    codex_config = _read(codex / "config.toml", toml=True).get("plugins", {})
    codex_overrides = [(pid, _read(workspace / ".codex/config.toml", toml=True).get("plugins", {}))
                      for pid, workspace, agent in workspaces if agent == "codex"]
    if isinstance(codex_config, dict):
        for key, config in codex_config.items():
            if not isinstance(config, dict) or config.get("enabled") is not True:
                continue
            blocked = sorted({pid for pid, values in codex_overrides if isinstance(values, dict)
                              and isinstance(values.get(key), dict) and values[key].get("enabled") is False})
            name, separator, market = key.rpartition("@")
            if not separator or not all(part and part not in {".", ".."} and "/" not in part and "\\" not in part
                                        for part in (name, market)):
                note("codex_plugin_identity_invalid", key)
                continue
            base = codex / "plugins/cache" / market / name
            package = None
            if isinstance(config.get("path"), str):
                configured = Path(config["path"]).expanduser()
                package = _resolved(configured if configured.is_absolute() else codex / configured)
            elif isinstance(config.get("version"), str):
                configured = _resolved(base / config["version"])
                if configured and configured.is_relative_to(base.resolve()):
                    package = configured
            else:
                try:
                    candidates = {p.resolve() for p in base.iterdir()
                                  if p.is_dir() and (p / ".codex-plugin/plugin.json").is_file()}
                except (OSError, RuntimeError):
                    candidates = set()
                if len(candidates) == 1:
                    package = candidates.pop()
                elif len(candidates) > 1:
                    note("codex_plugin_version_ambiguous", key)
            manifest = _read(package / ".codex-plugin/plugin.json") if package else {}
            if manifest.get("name") != name:
                note("codex_plugin_manifest_unresolved", key)
                continue
            roots.extend(_plugin_skill_roots(package, manifest, agent="codex", label="Codex",
                                            project=global_id, name=name, blocked=blocked))

    installed = _read(claude / "plugins/installed_plugins.json").get("plugins", {})
    global_enabled = _read(claude / "settings.json").get("enabledPlugins", {})
    global_enabled = global_enabled if isinstance(global_enabled, dict) else {}
    overrides = {}
    for pid, workspace, agent in workspaces:
        if agent != "claude-code":
            continue
        current = {}
        for filename in ("settings.json", "settings.local.json"):
            values = _read(workspace / ".claude" / filename).get("enabledPlugins", {})
            if isinstance(values, dict):
                current.update(values)
        overrides[(pid, workspace)] = current
    for key, records in installed.items() if isinstance(installed, dict) else ():
        records = [records] if isinstance(records, dict) else records if isinstance(records, list) else []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("installPath"), str):
                continue
            scope = record.get("scope")
            blocked = ()
            if scope == "user":
                blocked = sorted({pid for (pid, _), values in overrides.items() if values.get(key) is False})
                targets = [global_id] if global_enabled.get(key) is not False else []
            elif scope in {"project", "local"}:
                path = _resolved(record.get("projectPath"))
                targets = [pid for pid, workspace, agent in workspaces if agent == "claude-code" and path and path == workspace
                           and overrides[(pid, workspace)].get(key, global_enabled.get(key)) is not False]
                if not targets:
                    note("claude_plugin_project_unmapped_or_disabled", key)
            else:
                note("claude_plugin_scope_unsupported", key)
                continue
            package = _resolved(record["installPath"])
            if not package or not package.is_dir():
                note("claude_plugin_install_missing", key)
                continue
            manifest = _read(package / ".claude-plugin/plugin.json")
            name = manifest.get("name") or key.partition("@")[0]
            if not isinstance(name, str) or not name:
                continue
            for pid in targets:
                roots.extend(_plugin_skill_roots(package, manifest, agent="claude-code", label="Claude Code",
                                                project=pid, name=name, blocked=blocked))
    return tuple(roots)


def driver_roots(spec, adapter_key, projects, bindings=()):
    """Honor registered runtime config homes without inspecting credentials."""
    adapter_key = {"claude": "claude-code"}.get(adapter_key, adapter_key)
    if adapter_key not in {"codex", "claude-code"}:
        return ()
    env = spec.build_env()
    home = Path(env.get("HOME") or Path.home()).expanduser()
    projects = tuple(projects)
    root_id = next((p.id for p in projects if p.slug == "default"), None)
    root_id = root_id or next((p.id for p in projects if not p.parent_project_id), None)
    if not root_id:
        return ()
    variable, folder, label = (("CODEX_HOME", ".codex", "Codex") if adapter_key == "codex"
                               else ("CLAUDE_CONFIG_DIR", ".claude", "Claude Code"))
    native = NativeSkillRoot(Path(env.get(variable) or home / folder).expanduser() / "skills",
                             adapter_key, label, root_id)
    return (native, *(r for r in additional_roots(projects, home=home, env=env, bindings=bindings)
                      if r.agent_key == adapter_key))
