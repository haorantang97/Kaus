"""Immutable copies of the resolved Skill set; never write to native sources."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from drivers.base import ProjectionEntry, ProjectionResult, UnsupportedCapabilityError
from runtime.capability_matrix import SupportLevel

SELECTION_TYPE = "skill-selection"


@dataclass
class SkillPlan:
    report: ProjectionResult
    root: Path | None = None
    paths: tuple[Path, ...] = ()
    names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    spec: Any = None


def state_root(spec) -> Path:
    env = spec.build_env()
    return Path(env.get("KAUS_SKILL_STATE_ROOT") or
                Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "kaus/skill-projections").expanduser()


def selection_of(effective):
    selection = effective.get(SELECTION_TYPE, "default")
    return selection if selection is not None and selection.config.get("mode") != "native" else None


def is_skill_entry(entry):
    return entry.capability_type == SELECTION_TYPE or entry.type_ref.name == "skills"


def unsupported(binding, detail, *, dry_run=False):
    if not dry_run:
        raise UnsupportedCapabilityError(detail)
    return SkillPlan(ProjectionResult(binding_id=binding.id, dry_run=dry_run, unsupported=(
        ProjectionEntry(capability_type=SELECTION_TYPE, capability_id="default",
                        level=SupportLevel.UNSUPPORTED, reason="no_convention", detail=detail),)))


def _package_digest(source: Path) -> str:
    digest = hashlib.sha256()
    size = count = 0
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in {".git", "__pycache__"})
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            if path.is_symlink():
                raise ValueError("技能资源包含链接")
            digest.update(str(path.relative_to(source)).encode())
            if path.is_file():
                count += 1
                size += path.stat().st_size
                if count > 4000 or size > 50 * 1024 * 1024:
                    raise ValueError("技能资源超过复制限制")
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _rewrite_name(path: Path, name: str, description: str):
    text = path.read_text(encoding="utf-8")
    metadata, body = {}, text
    if text.startswith("---\n") and "\n---" in text[4:]:
        head, body = text[4:].split("\n---", 1)
        parsed = yaml.safe_load(head)
        if isinstance(parsed, dict):
            metadata = parsed
        body = body.lstrip("\r\n")
    metadata.update(name=name)
    metadata.setdefault("description", description or name)
    path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body)


def materialize(spec, binding, effective, *, layout: str, dry_run: bool) -> SkillPlan:
    selection = selection_of(effective)
    if selection is None:
        return SkillPlan(ProjectionResult(binding_id=binding.id, dry_run=dry_run))
    candidates, rejected = [], []
    for item in selection.config.get("assets", []):
        try:
            if item.get("capabilityType") not in {"skills", binding.backend_key + ":skills",
                                                       str(selection.config.get("scopeKey") or binding.backend_key) + ":skills"}:
                raise ValueError("技能不适用于此引擎")
            source = Path(item.get("sourcePath") or "").expanduser()
            if source.is_dir():
                source /= "SKILL.md"
            if not item.get("available", True) or source.name != "SKILL.md" or not source.is_file():
                raise ValueError("技能来源不可用")
            source = source.resolve()
            fingerprint = _package_digest(source.parent)
            identity = str(item["capabilityType"]) + ":" + str(item["capabilityId"])
            # A stable suffix disambiguates equal names without letting a native
            # same-name skill override the selected copy.
            suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
            label = re.sub(r"[^a-z0-9-]+", "-", str(item.get("name") or "skill").lower()).strip("-")[:42] or "skill"
            candidates.append((item, source, fingerprint, f"{label}-{suffix}"))
        except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
            rejected.append(ProjectionEntry(capability_type=item.get("capabilityType", "skills"),
                capability_id=item.get("capabilityId", "unknown"), level=SupportLevel.UNSUPPORTED,
                reason="invalid_config", detail=str(exc)))
    if rejected and not dry_run:
        raise UnsupportedCapabilityError("项目技能不可用：" + "; ".join(item.detail or "配置无效" for item in rejected[:3]))
    manifest = [{"id": item["capabilityId"], "type": item["capabilityType"], "source": str(source),
                 "hash": fingerprint, "name": name} for item, source, fingerprint, name in candidates]
    encoded = json.dumps({"bindingId": binding.id, "layout": layout, "skills": manifest}, sort_keys=True)
    revision = hashlib.sha256(encoded.encode()).hexdigest()[:24]
    owner = hashlib.sha256(binding.id.encode()).hexdigest()[:24]
    root = state_root(spec) / owner / revision
    relative = Path(".agents/skills") if layout == "roots" else Path("skills")
    paths = tuple(root / relative / name / "SKILL.md" for _, _, _, name in candidates)
    if not dry_run:
        root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root.is_symlink() or root.parent.is_symlink():
            raise ValueError("技能执行目录归属不匹配")
        marker = root / ".kaus-skill-selection.json"
        if root.exists():
            if not marker.is_file() or marker.read_text() != encoded:
                raise ValueError("技能执行目录归属不匹配")
            if any(not path.is_file() for path in paths):
                raise ValueError("技能执行副本缺失")
        else:
            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=root.parent))
            try:
                for item, source, _, name in candidates:
                    target = stage / relative / name
                    shutil.copytree(source.parent, target, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                    _rewrite_name(target / "SKILL.md", name, item.get("description", ""))
                if layout == "plugin":
                    (stage / ".claude-plugin").mkdir()
                    (stage / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "kaus-project"}))
                (stage / ".kaus-skill-selection.json").write_text(encoded)
                try:
                    stage.rename(root)
                except FileExistsError:
                    if not marker.is_file() or marker.read_text() != encoded:
                        raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    applied = tuple(ProjectionEntry(capability_type=item["capabilityType"], capability_id=item["capabilityId"],
                    level=SupportLevel.NATIVE, target_ref=str(path), action="set")
                    for (item, _, _, _), path in zip(candidates, paths))
    applied += (ProjectionEntry(capability_type=SELECTION_TYPE, capability_id="default", level=SupportLevel.NATIVE,
                               target_ref=str(root), action="set"),)
    return SkillPlan(ProjectionResult(binding_id=binding.id, dry_run=dry_run, applied=applied,
                                     unsupported=tuple(rejected)), root, paths,
                     tuple(name for _, _, _, name in candidates))
