"""Materialize selected skills into an owned execution home, never a native home.

Copies isolate skill self-edits. Replaced scopes remain recoverable in the
private home's backup directory. Live model-loading acceptance is separate.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import yaml

from app.capabilities.models import parse_capability_type
from drivers.base import ProjectionEntry, ProjectionResult
from drivers.hermes import config_yaml
from drivers.projection_store import atomic_write_text
from runtime.capability_matrix import SupportLevel

SELECTION_TYPE = "skill-selection"
MANIFEST = ".kaus-skills.json"


def selection_of(effective):
    value = effective.get(SELECTION_TYPE, "default")
    return value if value is not None and value.config.get("mode") != "native" else None


def without_selection(effective):
    return effective.model_copy(update={
        "entries": tuple(e for e in effective.entries if e.type_ref.name != "skills" and e.capability_type != SELECTION_TYPE),
        "blocked": tuple(e for e in effective.blocked if parse_capability_type(e.capability_type).name != "skills"),
    })


def unavailable(binding_id):
    return ProjectionEntry(capability_type=SELECTION_TYPE, capability_id="default",
                           level=SupportLevel.UNSUPPORTED, reason="no_convention",
                           detail="此连接使用原生目录，项目技能需要独立执行目录")


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in {".git", "__pycache__"})
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            if path.is_symlink():
                # A linked asset could let execution write outside its private
                # skill package. Support it only after an explicit copy policy.
                raise ValueError("技能资源包含外部链接")
            digest.update(str(path.relative_to(root)).encode())
            if path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
    return digest.hexdigest()


def _nested_skill(root: Path) -> bool:
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in {
            ".git", ".archive", ".hub", "__pycache__", "node_modules", ".venv", "venv"
        })
        if Path(current) == root:
            directories[:] = [d for d in directories if d not in {"references", "templates", "assets", "scripts"}]
        elif "SKILL.md" in files:
            return True
    return False


def _ensure_name(path: Path, name: str):
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        header = text[4:].split("\n---", 1)[0]
        parsed = yaml.safe_load(header)
        if isinstance(parsed, dict) and parsed.get("name"):
            return
        text = "---\nname: " + json.dumps(name, ensure_ascii=False) + "\n" + text[4:]
    else:
        text = "---\nname: " + json.dumps(name, ensure_ascii=False) + "\n---\n" + text
    path.write_text(text, encoding="utf-8")


def project_skills(*, home: Path, binding, effective, dry_run: bool):
    selection = selection_of(effective)
    if selection is None:
        return ProjectionResult(binding_id=binding.id, dry_run=dry_run)
    if not dry_run:
        marker = home / ".kaus-scope.json"
        ownership = json.loads(marker.read_text()) if marker.is_file() else {}
        if (home.is_symlink() or ownership.get("bindingId") != binding.id
                or ownership.get("projectId") != binding.project_id):
            raise ValueError("技能投射需要 Kaus 独立执行目录")
    assets = selection.config.get("assets", [])
    candidates = []
    unsupported = []
    for asset in assets:
        reason = None
        source = Path(asset["sourcePath"]).expanduser() if asset.get("sourcePath") else None
        if source and source.is_dir():
            source = source / "SKILL.md"
        if asset.get("capabilityType") not in {"skills", "hermes:skills", "hermes-acp:skills"}:
            reason = "技能不适用于此引擎"
        elif not source or not asset.get("available", True) or not source.is_file() or source.name != "SKILL.md":
            reason = "技能来源不可用"
        else:
            try:
                source = source.resolve()
                if _nested_skill(source.parent):
                    raise ValueError("Skill contains separately managed nested packages")
                fingerprint = _fingerprint(source.parent)
                candidates.append((asset, source, fingerprint))
            except (OSError, ValueError):
                reason = "技能资源无法复制到独立目录"
        if reason:
            unsupported.append(ProjectionEntry(capability_type=asset["capabilityType"],
                capability_id=asset["capabilityId"], level=SupportLevel.UNSUPPORTED,
                reason="invalid_config", detail=reason))
    valid = []
    names = [asset["name"] for asset, _, _ in candidates]
    for candidate in candidates:
        asset = candidate[0]
        if names.count(asset["name"]) > 1:
            unsupported.append(ProjectionEntry(capability_type=asset["capabilityType"],
                capability_id=asset["capabilityId"], level=SupportLevel.UNSUPPORTED,
                reason="invalid_config", detail="引擎无法区分同名技能，请保留一个有效来源"))
        else:
            valid.append(candidate)
    skills = home / "skills"
    manifest_path = home / MANIFEST
    original_manifest = manifest_path.read_text() if manifest_path.exists() else None
    previous = json.loads(original_manifest) if original_manifest else {"entries": {}}
    old_entries = previous.get("entries", {})
    manifest = {"runtimePolicy": {"version": 1, "bindingId": binding.id, "projectId": binding.project_id},
                "entries": {asset["id"]: {
        "directory": "kaus-" + hashlib.sha256(asset["id"].encode()).hexdigest()[:20],
        "sourceHash": fingerprint, "sourcePath": str(source), "name": asset["name"],
        "pluginName": asset["name"] if ":" in asset["name"] else None,
    } for asset, source, fingerprint in valid}}
    files_changed = (skills.is_symlink() or not skills.is_dir() or manifest != previous or any(
        not (skills / item["directory"] / "SKILL.md").is_file() for item in manifest["entries"].values()))
    action = "set" if files_changed else "unchanged"
    applied = [ProjectionEntry(capability_type=asset["capabilityType"], capability_id=asset["capabilityId"],
                               level=SupportLevel.NATIVE, key_path="skills", action=action,
                               after=asset["name"]) for asset, _, _ in valid]
    applied.append(ProjectionEntry(capability_type=SELECTION_TYPE, capability_id="default",
                                   level=SupportLevel.NATIVE, key_path="skills.selection", action=action,
                                   after=len(valid)))
    backup_path = None
    if not dry_run:
        config_path = home / "config.yaml"
        original_config = config_path.read_text()
        original_marker = marker.read_text()
        stage = Path(tempfile.mkdtemp(prefix=".kaus-skills-stage-", dir=home)) if files_changed else None
        backup = None
        installed = False
        try:
            # Preserve user-created skills inside the private home. Source
            # directories linked by old prepare() are never walked or edited.
            managed_names = {e["directory"] for e in old_entries.values()}
            if stage and skills.is_dir() and not skills.is_symlink():
                for item in skills.iterdir():
                    if item.name in managed_names:
                        continue
                    if item.is_symlink():
                        raise ValueError("独立技能目录存在未托管链接")
                    if item.is_dir():
                        _fingerprint(item)
                        shutil.copytree(item, stage / item.name)
                    else:
                        shutil.copy2(item, stage / item.name)
            for asset, source, fingerprint in valid:
                if stage is None:
                    break
                identity = asset["id"]
                directory = manifest["entries"][identity]["directory"]
                destination = stage / directory
                old = old_entries.get(identity)
                old_path = skills / directory
                # Retain self-evolved private contents while the original source
                # remains unchanged; the source itself is never a write target.
                copy_from = source.parent
                if old and old.get("sourceHash") == fingerprint and old_path.is_dir() and not skills.is_symlink():
                    _fingerprint(old_path)
                    copy_from = old_path
                shutil.copytree(copy_from, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                # Native skills without frontmatter use their directory name.
                # Keep that visible name when the private directory is opaque.
                _ensure_name(destination / "SKILL.md", asset["name"])
            if stage:
                if skills.exists() or skills.is_symlink():
                    backups = home / ".kaus-skill-backups"
                    backups.mkdir(exist_ok=True)
                    backup = backups / uuid.uuid4().hex
                    skills.rename(backup)
                    backup_path = str(backup)
                stage.rename(skills)
                installed = True
            values = config_yaml.load(original_config)
            settings = values.get("skills")
            settings = dict(settings) if isinstance(settings, dict) else {}
            settings.update({"external_dirs": [], "project_discovery": False,
                             "trusted_project_dirs": [], "create_dir": str(skills)})
            # The tree selection owns enabled/blocked state inside this private home.
            settings.pop("disabled", None)
            settings.pop("platform_disabled", None)
            desired_config = config_yaml.apply_top_level(original_config, {"skills": settings})
            if desired_config != original_config:
                atomic_write_text(config_path, desired_config)
            desired_manifest = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            if desired_manifest != original_manifest:
                atomic_write_text(manifest_path, desired_manifest)
            if not ownership.get("skillPolicyRequired"):
                atomic_write_text(marker, json.dumps({**ownership, "skillPolicyRequired": True}))
        except BaseException:
            if installed:
                shutil.rmtree(skills)
            if backup and (backup.exists() or backup.is_symlink()):
                backup.rename(skills)
            atomic_write_text(config_path, original_config)
            atomic_write_text(marker, original_marker)
            if original_manifest is not None:
                atomic_write_text(manifest_path, original_manifest)
            elif manifest_path.exists():
                manifest_path.unlink()
            raise
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage)
    return ProjectionResult(binding_id=binding.id, dry_run=dry_run, applied=tuple(applied),
                            unsupported=tuple(unsupported), backup_path=backup_path)
