"""Prewrite a workspace trust record before a native CLI resumes in a terminal.

First launches in an unknown folder stop at the agent's own "trust this folder?"
menu. Kaus only ever adds the one folder it is opening, never changes other
fields, never creates a missing config file and never overrides an explicit
decision already recorded there. Record formats follow stablyai/orca
``src/main/agent-trust-presets.ts`` and ``claude-folder-trust-file.ts`` (MIT).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import tomllib
import unicodedata
from uuid import uuid4

GRANTED = "granted"
UNCHANGED = "unchanged"
MISSING = "missing-config"
LOCKED = "locked"
UNREADABLE = "unreadable"

KINDS = ("claude", "codex", "cursor", "copilot")


def grant_workspace_trust(kind: str, workspace: str, config_dir: str | None = None,
                          *, home: str | None = None) -> str:
    """Return one of the outcome constants; never raises for user-file problems."""
    home = home or str(Path.home())
    folder = _canonical(workspace)
    try:
        if kind == "claude":
            return _claude(folder, workspace, config_dir, home)
        if kind == "codex":
            return _codex(folder, Path(config_dir or Path(home) / ".codex") / "config.toml")
        if kind == "cursor":
            return _cursor(folder, Path(config_dir or Path(home) / ".cursor"))
        if kind == "copilot":
            return _copilot(folder, Path(config_dir or Path(home) / ".copilot") / "config.json")
    except OSError:
        return UNREADABLE
    raise ValueError(f"Unknown workspace trust kind: {kind}")


def _canonical(path: str) -> str:
    # Agents compare realpaths, including macOS /var -> /private/var.
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _read_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _replace(target: Path, text: str, *, mode: int | None = None) -> None:
    """Atomic replace that keeps the existing permission bits."""
    real = Path(os.path.realpath(target))
    mode = mode if mode is not None else (real.stat().st_mode & 0o777 if real.exists() else 0o600)
    temporary = real.with_name(f"{real.name}.kaus-trust-{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, real)
    finally:
        if temporary.exists():
            temporary.unlink()


def _claude_config_file(config_dir: str | None, home: str) -> Path:
    # Claude prefers legacy .config.json, then .claude.json under CLAUDE_CONFIG_DIR or home.
    legacy = Path(config_dir or Path(home) / ".claude") / ".config.json"
    if legacy.exists():
        return legacy
    return Path(config_dir or home) / ".claude.json"


def _claude(folder: str, given: str, config_dir: str | None, home: str) -> str:
    config_file = _claude_config_file(config_dir, home)
    if not config_file.exists():
        return MISSING
    keys = tuple(dict.fromkeys(unicodedata.normalize("NFC", os.path.normpath(path)) for path in (folder, given)))

    def change(config: dict) -> dict | None | bool:
        projects = config.get("projects", {})
        if not isinstance(projects, dict):
            return False
        if any(isinstance(projects.get(key), dict) and projects[key].get("hasTrustDialogAccepted") is True
               for key in keys):
            return None
        updated = dict(projects)
        for key in keys:
            entry = updated.get(key)
            updated[key] = {**entry, "hasTrustDialogAccepted": True} if isinstance(entry, dict) else {"hasTrustDialogAccepted": True}
        return {**config, "projects": updated}

    probe = _read_object(config_file)
    if probe is None:
        return UNREADABLE
    planned = change(probe)
    if planned is None:
        return UNCHANGED
    if planned is False:
        return UNREADABLE
    # Claude locks the literal path with a sibling directory (proper-lockfile); defer to it.
    lock = Path(f"{config_file}.lock")
    for delay in (0, .05, .1, .2, .25):
        time.sleep(delay)
        try:
            lock.mkdir()
            break
        except FileExistsError:
            continue
    else:
        return LOCKED
    try:
        current = _read_object(config_file)
        if current is None:
            return UNREADABLE
        updated = change(current)
        if updated is None:
            return UNCHANGED
        if updated is False:
            return UNREADABLE
        _replace(config_file, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
        return GRANTED
    finally:
        lock.rmdir()


def _codex(folder: str, config_file: Path) -> str:
    if not config_file.exists():
        return MISSING
    try:
        text = config_file.read_text(encoding="utf-8")
        config = tomllib.loads(text)
    except (OSError, ValueError):
        return UNREADABLE
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return UNREADABLE
    if folder in projects:
        # Any explicit level, including "untrusted", is the user's decision.
        return UNCHANGED
    block = "[projects." + json.dumps(folder) + ']\ntrust_level = "trusted"\n'
    updated = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text.strip() else "") + block
    try:
        if tomllib.loads(updated).get("projects", {}).get(folder, {}).get("trust_level") != "trusted":
            return UNREADABLE
    except ValueError:
        # e.g. an inline `projects = {…}` table cannot take a new header.
        return UNREADABLE
    _replace(config_file, updated)
    return GRANTED


def _cursor(folder: str, root: Path) -> str:
    slug = folder.lstrip("/").replace("/", "-")
    if not slug:
        return UNREADABLE
    marker = root / "projects" / slug / ".workspace-trusted"
    if marker.exists():
        return UNCHANGED
    if not root.is_dir():
        return MISSING
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"trustedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "workspacePath": folder}
    _replace(marker, json.dumps(payload, indent=2) + "\n", mode=0o644)
    return GRANTED


def _copilot(folder: str, config_file: Path) -> str:
    if not config_file.exists():
        return MISSING
    config = _read_object(config_file)
    if config is None:
        return UNREADABLE
    existing = config.get("trustedFolders", [])
    if not isinstance(existing, list):
        return UNREADABLE
    if folder in {_canonical(item) for item in existing if isinstance(item, str)}:
        return UNCHANGED
    config["trustedFolders"] = [*existing, folder]
    # Copilot keeps tokens here; keep owner-only permissions.
    _replace(config_file, json.dumps(config, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return GRANTED
