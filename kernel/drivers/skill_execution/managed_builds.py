"""Verified private builds for scoped skills and explicit compatibility hooks.

Native launch commands and vendor installations remain untouched. A build is
immutable; registration switches only the Kaus-owned manifest under a lock.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from drivers.base import UnsupportedCapabilityError
from .common import state_root


def build_root(spec):
    return state_root(spec) / "managed-runtimes"


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _document(root):
    path = root / "skill-builds.json"
    if not path.exists():
        return {"schemaVersion": 1, "builds": {}, "history": {}}
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("运行组件记录不可读取")
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get("schemaVersion") != 1
            or not isinstance(data.get("builds"), dict)
            or not isinstance(data.get("history", {}), dict)):
        raise ValueError("运行组件记录格式无效")
    return data


def _verified(root, entry):
    if not isinstance(entry, dict):
        raise ValueError("运行组件未安装")
    path = Path(entry.get("binaryPath", ""))
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("运行组件路径无效")
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    if not path.is_file() or not os.access(path, os.X_OK) or file_hash(path) != entry.get("binaryHash"):
        raise ValueError("运行组件校验失败，请重新安装")
    return path


def resolve_managed_binary(spec, *, engine_key, source_version, patch_version,
                           expected_source_commit=None, expected_patch_hash=None) -> Path:
    root = build_root(spec)
    try:
        entry = _document(root)["builds"].get(engine_key)
        if not entry or entry.get("sourceVersion") != source_version or entry.get("patchVersion") != patch_version:
            raise ValueError(f"请安装 {engine_key} 的项目技能运行组件")
        if ((expected_source_commit is not None and entry.get("sourceCommit") != expected_source_commit)
                or (expected_patch_hash is not None and entry.get("patchHash") != expected_patch_hash)):
            raise ValueError(f"请更新 {engine_key} 的项目技能运行组件")
        return _verified(root, entry)
    except (OSError, ValueError, TypeError) as exc:
        raise UnsupportedCapabilityError(str(exc)) from exc


def register_managed_build(spec, *, engine_key, binary_path, source_version,
                           source_commit, source_hash, patch_version, patch_hash,
                           license_id, source_url, notices=(), build_profile=None,
                           runtime_files=()):
    """Copy a tested build into managed storage and atomically select it.

    The caller performs protocol/loader verification before registration.
    Notices and already-verified runtime companions are copied before the
    manifest selects the complete immutable bundle.
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", engine_key):
        raise ValueError("Invalid engine key")
    for digest in (source_hash, patch_hash):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid provenance hash")
    if not all(isinstance(v, str) and v for v in (source_version, source_commit, patch_version, license_id, source_url)):
        raise ValueError("Missing build provenance")
    source = Path(binary_path)
    if source.is_symlink() or not source.is_file() or not os.access(source, os.X_OK):
        raise ValueError("Build executable unavailable")
    binary_hash = file_hash(source)
    notices = tuple(notices)
    assets = {}
    reserved = {'agent', 'provenance.json', *(Path(notice).name for notice in notices)}
    for value in runtime_files:
        path = Path(value)
        if path.name in reserved or path.name in assets or path.is_symlink() or not path.is_file():
            raise ValueError('Invalid runtime companion')
        assets[path.name] = (path, file_hash(path))
    asset_hashes = {name: value[1] for name, value in sorted(assets.items())}
    identity = binary_hash if not assets else hashlib.sha256(json.dumps(
        {'binary': binary_hash, 'runtimeFiles': asset_hashes}, sort_keys=True).encode()).hexdigest()
    root = build_root(spec)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("Managed runtime directory is linked")
    build = root / engine_key / identity
    build.parent.mkdir(exist_ok=True, mode=0o700)
    if build.parent.is_symlink() or build.is_symlink():
        raise ValueError("Managed build directory is linked")
    entry = {"sourceVersion": source_version, "sourceCommit": source_commit,
             "sourceHash": source_hash, "patchVersion": patch_version,
             "patchHash": patch_hash, "binaryPath": str((build / "agent").absolute()),
             "binaryHash": binary_hash, "license": license_id, "sourceUrl": source_url}
    if asset_hashes:
        entry['runtimeFiles'] = asset_hashes
    if build_profile is not None:
        if build_profile not in {"dev", "release"}:
            raise ValueError("Invalid build profile")
        entry["buildProfile"] = build_profile
    with (root / ".skill-builds.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _document(root)
        if not build.exists():
            stage = Path(tempfile.mkdtemp(prefix=".build-", dir=build.parent))
            try:
                shutil.copyfile(source, stage / "agent")
                (stage / "agent").chmod(0o700)
                if file_hash(stage / "agent") != binary_hash:
                    raise ValueError("Build changed during registration")
                for notice in notices:
                    notice = Path(notice)
                    if notice.name in {"agent", "provenance.json"} or notice.is_symlink() or not notice.is_file():
                        raise ValueError("Invalid build notice")
                    shutil.copyfile(notice, stage / notice.name)
                for name, (path, expected) in assets.items():
                    destination = stage / name
                    shutil.copyfile(path, destination)
                    destination.chmod(0o700 if os.access(path, os.X_OK) else 0o600)
                    if file_hash(destination) != expected:
                        raise ValueError('Runtime companion changed during registration')
                (stage / "provenance.json").write_text(json.dumps(entry, indent=2) + "\n")
                stage.rename(build)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        _verified(root, entry)
        for name, expected in asset_hashes.items():
            companion = build / name
            if companion.is_symlink() or not companion.is_file() or file_hash(companion) != expected:
                raise ValueError('Runtime companion verification failed')
        previous = data["builds"].get(engine_key)
        if previous and previous != entry:
            data.setdefault("history", {}).setdefault(engine_key, []).append(previous)
        data["builds"][engine_key] = entry
        fd, name = tempfile.mkstemp(prefix=".skill-builds-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, root / "skill-builds.json")
        finally:
            Path(name).unlink(missing_ok=True)
    return Path(entry["binaryPath"])


def managed_build_snapshot(spec, engine_key):
    root = build_root(spec)
    try:
        entry = _document(root)["builds"].get(engine_key)
        if not entry:
            return None
        _verified(root, entry)
        return {**entry, "available": True}
    except (OSError, ValueError, TypeError):
        return {"available": False}
