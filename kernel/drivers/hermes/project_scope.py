"""Private execution homes for new project associations; existing native homes stay put.

Authentication is referenced from the connected engine. Session databases,
memory, generated instructions and projected settings belong to this scope.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from drivers.hermes import config_yaml
from drivers.projection_store import Provenance, atomic_write_text

SCOPE_KEY = "project_execution"


def enabled(binding) -> bool:
    return binding.runtime_config.get(SCOPE_KEY) == "isolated"


def home_for(root: Path, binding) -> Path:
    identity = hashlib.sha256(binding.id.encode()).hexdigest()[:24]
    return root / ".kaus-scopes" / identity


def prepare(root: Path, source: Path, binding, *, refresh: bool = False) -> Path:
    home = home_for(root, binding)
    marker = home / ".kaus-scope.json"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if marker.exists():
        ownership = json.loads(marker.read_text())
        if ownership != {"bindingId": binding.id, "projectId": binding.project_id}:
            raise ValueError("项目执行目录归属不匹配")
        if not refresh:
            return home
    # Only the original engine supplies the baseline. Never seed another
    # project's managed values, histories or private memory into a new project.
    source_config = source / "config.yaml"
    text = source_config.read_text() if source_config.is_file() else ""
    values = config_yaml.load(text)
    owned = Provenance.load(source)
    for key in owned.records:
        top = key.split(".")[0]
        values.pop(top, None)
    atomic_write_text(home / "config.yaml", config_yaml.apply_top_level("", values))
    os.chmod(home / "config.yaml", 0o600)
    # This generated home is rebuilt from the connection baseline before each
    # projection. Removing a project override must also remove its old value.
    provenance = Provenance.load(home)
    provenance.records.clear()
    provenance.save()
    for name in (".env", "auth.json", "credentials", "skills", "plugins"):
        original, target = source / name, home / name
        if original.exists() and not target.exists() and not target.is_symlink():
            target.symlink_to(original.resolve(), target_is_directory=original.is_dir())
    # A named native persona may supply identity. Project instructions are
    # added in the private copy, never in the source persona file.
    soul = source / "SOUL.md"
    text = soul.read_text() if binding.native_scope_ref and binding.native_scope_ref != "default" and soul.is_file() else ""
    atomic_write_text(home / "SOUL.md", text)
    atomic_write_text(marker, json.dumps({"bindingId": binding.id, "projectId": binding.project_id}))
    return home
