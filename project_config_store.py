"""Agent-independent project configuration, compatible with the existing tree UI."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MARKER = ".kaus-project.json"


def root(state_root: Path) -> Path:
    return state_root / "state" / "project-configs"


def directory(state_root: Path, name: str) -> Path:
    if not _ID.fullmatch(name):
        raise ValueError("项目标识无效")
    return root(state_root) / name


def contains(state_root: Path, name: str) -> bool:
    return bool(_ID.fullmatch(name) and (root(state_root) / name / MARKER).is_file())


def names(state_root: Path) -> list[str]:
    base = root(state_root)
    return sorted(p.name for p in base.iterdir() if contains(state_root, p.name)) if base.is_dir() else []


def create(state_root: Path, name: str, *, description: str = "", source: Path | None = None) -> Path:
    target = directory(state_root, name)
    target.mkdir(parents=True, exist_ok=False)
    try:
        (target / MARKER).write_text(json.dumps({"description": description}), encoding="utf-8")
        # Only configuration is cloned. Native histories, memories and login
        # material remain with the original conversation/connection.
        for filename in ("SOUL.md", "constitution.md"):
            original = source / filename if source else None
            (target / filename).write_text(original.read_text() if original and original.is_file() else "", encoding="utf-8")
        config = {}
        if source and (source / "config.yaml").is_file():
            import yaml
            import capability_import
            from drivers.secret_guard import carries_credential
            original = yaml.safe_load((source / "config.yaml").read_text()) or {}
            config = {key: value for key, value in original.items()
                      if key in capability_import.INHERITABLE_KEYS
                      and key not in ("providers", "auxiliary", "delegation")
                      and not carries_credential(value)}
        import yaml
        (target / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        if source and (source / "skills").is_dir():
            shutil.copytree(source / "skills", target / "skills", symlinks=True)
        else:
            (target / "skills").mkdir()
    except BaseException:
        shutil.rmtree(target)
        raise
    return target


def archive(state_root: Path, name: str) -> Path:
    source = directory(state_root, name)
    if not contains(state_root, name):
        raise ValueError("项目配置不存在")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = state_root / "state" / "project-config-archive" / f"{name}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return target
