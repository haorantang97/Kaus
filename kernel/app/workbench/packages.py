"""Bounded package installation; backend execution requires a separate grant."""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.api.api_errors import ApiError
from app.workbench.models import Manifest

ALLOWED_SUFFIXES = frozenset({".html", ".js", ".mjs", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".map"})
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000


def install_package(path: str, root: Path) -> tuple[Manifest, Path]:
    source = Path(path).expanduser()
    if not source.is_dir() or source.is_symlink():
        raise ApiError(400, "invalid_package", "请选择模块目录。")
    source = source.resolve()
    # All installation entry points apply the same privacy and built-asset checks.
    from app.workbench.intake_files import validate_package
    try:
        validate_package(source)
    except ApiError as exc:
        if exc.code in {"adaptation_entry_missing", "adaptation_backend_missing"}:
            raise ApiError(400, "invalid_manifest", "模块入口不存在。") from exc
        raise
    try:
        manifest_file = source / "kaus-module.json"
        if manifest_file.is_symlink() or manifest_file.stat().st_size > 16384:
            raise ValueError("manifest")
        manifest = Manifest.model_validate(json.loads(manifest_file.read_text()))
    except (OSError, ValueError, ValidationError) as exc:
        raise ApiError(400, "invalid_manifest", "模块清单无效。") from exc
    files = []
    total = 0
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ApiError(400, "invalid_package", "模块目录不能包含链接。")
        if not item.is_file():
            continue
        if item.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ApiError(400, "invalid_package", "模块包含不支持的文件。")
        total += item.stat().st_size
        files.append(item.relative_to(source))
        if total > MAX_PACKAGE_BYTES or len(files) > MAX_FILES:
            raise ApiError(400, "package_too_large", "模块文件过大。")
    if Path(manifest.entry) not in files:
        raise ApiError(400, "invalid_manifest", "模块入口不存在。")
    if manifest.backend and Path(manifest.backend.entry) not in files:
        raise ApiError(400, "invalid_manifest", "模块服务入口不存在。")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{manifest.id}-{uuid.uuid4().hex}"
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        for relative in files:
            src = source / relative
            if src.is_symlink() or source not in src.resolve().parents:
                raise ApiError(400, "invalid_package", "模块文件已变更。")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest, destination
