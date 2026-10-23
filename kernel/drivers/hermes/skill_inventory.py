"""Native Hermes skill source roots, including profile allocations."""
from pathlib import Path
import json
import hashlib
import re

import yaml

from drivers.skill_inventory import NativeSkillRoot


def plugin_roots(home: Path, project_id: str):
    """Read installed manifests and their conventional skills directories only.

    No plugin Python is imported. Arbitrary register_skill paths and pip entry
    points require a native inventory protocol; they are not guessed here.
    """
    result = []
    base = home / "plugins"
    pending = [(base, "", 0)]
    seen = set()
    while pending:
        folder, prefix, depth = pending.pop()
        try:
            resolved = folder.resolve()
            if resolved in seen or depth > 2:
                continue
            seen.add(resolved)
            for child in sorted(folder.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                native, portable = child / "plugin.yaml", child / "plugin.json"
                if native.is_file():
                    data = yaml.safe_load(native.read_text())
                    namespace = data.get("name") if isinstance(data, dict) else None
                elif portable.is_file():
                    data = json.loads(portable.read_text())
                    name = data.get("name") if isinstance(data, dict) else None
                    key = prefix + child.name if prefix else name
                    slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-" for ch in (key or "").lower()).strip("-_") or "plugin"
                    namespace = "agent-plugin-" + slug + "-" + hashlib.sha256((key or "").encode()).hexdigest()[:8]
                else:
                    pending.append((child, prefix + child.name + "/", depth + 1))
                    continue
                if isinstance(namespace, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", namespace):
                    result.append(NativeSkillRoot(child / "skills", "hermes", "Hermes", project_id,
                                                  name_prefix=namespace + ":"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return result


def roots_for(base: Path, projects, bindings):
    root = next((p.id for p in projects if p.slug == "default"), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if not root:
        return ()
    by_slug = {p.slug: p.id for p in projects}
    project_ids = {p.id for p in projects}
    profile_projects = {b.native_scope_ref: b.project_id for b in bindings
                        if b.backend_key.startswith("hermes") and b.native_scope_ref and b.project_id in project_ids}
    result = [NativeSkillRoot(base / "skills", "hermes", "Hermes", root, nested_skills=True)]
    result.extend(plugin_roots(base, root))
    try:
        profiles = sorted((base / "profiles").iterdir())
    except OSError:
        profiles = []
    for profile in profiles:
        project = profile_projects.get(profile.name) or by_slug.get(profile.name)
        if project:
            result.append(NativeSkillRoot(profile / "skills", "hermes", "Hermes", project, nested_skills=True))
            result.extend(plugin_roots(profile, project))
    # Runtime-created/evolved skills are owned by their originating Project.
    # Untouched execution copies are not imported back as duplicate sources.
    from drivers.hermes.skill_snapshots import digest, validate_scope
    for marker in sorted((base / ".kaus-scopes").glob("*/.kaus-scope.json")):
        try:
            owner = json.loads(marker.read_text())
            if owner.get("projectId") in project_ids:
                for output in sorted((marker.parent / ".kaus-native-skill-outputs").glob("*")):
                    if output.is_dir() and not output.is_symlink():
                        mode_path = marker.parent / ".kaus-skill-modes" / (output.name + ".json")
                        mode = json.loads(mode_path.read_text()) if mode_path.is_file() else {}
                        result.append(NativeSkillRoot(output, "hermes", "Hermes", owner["projectId"], nested_skills=True,
                            generated=True, source_conversation_id=mode.get("conversationId")))
        except (OSError, ValueError):
            continue
    for session_marker in sorted((base / ".kaus-scopes").glob("*/.kaus-skill-sessions/*/*/.kaus-skill-session.json")):
        scope = session_marker.parent
        home = scope.parents[2]
        try:
            owner = json.loads(session_marker.read_text())
            if owner.get("projectId") not in project_ids:
                continue
            validate_scope(home, scope)
            manifest = json.loads((scope / ".kaus-skills.json").read_text())
            managed = {entry["directory"]: {**entry, "id": key} for key, entry in manifest.get("entries", {}).items()}
            baseline = home / ".kaus-skill-generations" / owner["generation"] / "skills"
            for folder in sorted((scope / "skills").iterdir()):
                if not folder.is_dir() or folder.is_symlink():
                    continue
                prior = baseline / folder.name
                if prior.is_dir() and digest(prior) == digest(folder):
                    continue
                plugin_name = managed.get(folder.name, {}).get("pluginName") or ""
                prefix = plugin_name.rpartition(":")[0] + ":" if ":" in plugin_name else ""
                result.append(NativeSkillRoot(folder, "hermes", "Hermes", owner["projectId"],
                    nested_skills=True, name_prefix=prefix, generated=True,
                    source_conversation_id=owner["conversationId"], replaces_id=managed.get(folder.name, {}).get("id")))
        except (OSError, ValueError, KeyError, RuntimeError):
            continue
    return tuple(result)
