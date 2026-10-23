"""Read-only native Skill discovery. Source ownership is not compatibility inference.

No subprocesses, model calls, directory creation, or native configuration writes.
Custom drivers can expose ``skill_inventory_roots`` with the same root contract.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import yaml


@dataclass(frozen=True)
class NativeSkillRoot:
    path: Path
    agent_key: str
    agent_label: str
    project_id: str
    nested_skills: bool = False
    name_prefix: str = ""
    blocked_project_ids: tuple[str, ...] = ()
    generated: bool = False
    source_conversation_id: str | None = None
    replaces_id: str | None = None


def configured_roots(registry, projects, bindings, *, home: Path | None = None,
                     env: Mapping[str, str] | None = None) -> tuple[NativeSkillRoot, ...]:
    """Known installed locations plus registered driver roots; absent paths are harmless."""
    home = home or Path.home()
    env = os.environ if env is None else env
    project_ids = {p.id for p in projects}
    root = next((p.id for p in projects if p.slug == "default"), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if root is None:
        return ()
    result = [
        NativeSkillRoot(Path(env.get("CODEX_HOME") or home / ".codex") / "skills", "codex", "Codex", root),
        NativeSkillRoot(Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "skills", "claude-code", "Claude Code", root),
    ]
    for driver in registry.list_drivers() if registry else ():
        provider = getattr(driver, "skill_inventory_roots", ())
        roots = provider(projects, bindings) if callable(provider) else provider
        for spec in roots:
            if isinstance(spec, NativeSkillRoot) and spec.project_id in project_ids:
                result.append(spec)
    from drivers.skill_execution.inventory import additional_roots
    result.extend(additional_roots(projects, home=home, env=env, bindings=bindings))
    return tuple(result)


def discover_skills(roots: Iterable[NativeSkillRoot]) -> list[dict]:
    result: dict[str, dict] = {}
    for source in roots:
        base = source.path.expanduser()
        if not base.is_dir():
            continue
        files: list[Path] = []
        seen: set[Path] = set()
        pending = [(base, 0)]
        while pending:
            folder, depth = pending.pop()
            try:
                resolved_folder = folder.resolve()
                if resolved_folder in seen or depth > 12:
                    continue
                seen.add(resolved_folder)
                if (folder / "SKILL.md").is_file():
                    files.append(folder / "SKILL.md")
                    if not source.nested_skills:
                        continue
                    support = {"references", "templates", "assets", "scripts"}
                else:
                    support = set()
                # Follow native category symlinks too, with cycle detection.
                pending.extend((p, depth + 1) for p in sorted(folder.iterdir(), reverse=True)
                               if p.name not in {".git", ".github", ".hub", ".archive", ".venv", "venv",
                                                 "node_modules", "site-packages", "__pycache__", ".tox", ".nox",
                                                 ".pytest_cache", ".mypy_cache", ".ruff_cache", "_org"} | support and p.is_dir())
            except (OSError, RuntimeError):
                continue
        for path in sorted(files):
            try:
                with path.open(encoding="utf-8") as stream:
                    text = stream.read(65536)
                resolved = str(path.resolve())
                identity = source.agent_key + "\0" + (source.name_prefix + "\0" if source.name_prefix else "") + resolved
                skill_id = "native-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                if skill_id in result:
                    projects = result[skill_id]["defaultProjectIds"]
                    if source.project_id not in projects:
                        projects.append(source.project_id)
                    blocked = result[skill_id]["defaultBlockedProjectIds"]
                    blocked.extend(pid for pid in source.blocked_project_ids if pid not in blocked)
                    continue
                metadata = {}
                if text.startswith("---\n"):
                    header = text[4:].split("\n---", 1)[0]
                    try:
                        parsed = yaml.safe_load(header)
                        metadata = parsed if isinstance(parsed, dict) else {}
                    except yaml.YAMLError:
                        pass
                name = metadata.get("name")
                description = metadata.get("description")
                result[skill_id] = {
                    "id": skill_id,
                    "name": source.name_prefix + (str(name)[:200] if isinstance(name, str) and name.strip() else path.parent.name),
                    "description": description[:1200] if isinstance(description, str) else "",
                    "agentKey": source.agent_key,
                    "agentLabel": source.agent_label,
                    "sourcePath": resolved,
                    "available": True,
                    "defaultProjectId": source.project_id,
                    "defaultProjectIds": [source.project_id],
                    "defaultBlockedProjectIds": list(source.blocked_project_ids),
                    "capabilityType": source.agent_key + ":skills",
                    "capabilityId": skill_id,
                    **({"generated": True, "originProjectId": source.project_id,
                        "sourceConversationId": source.source_conversation_id, "replacesId": source.replaces_id}
                        if source.generated else {}),
                }
            except (OSError, UnicodeError):
                continue
    return list(result.values())
