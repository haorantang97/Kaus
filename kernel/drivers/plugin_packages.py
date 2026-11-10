"""Read external plugin packages without executing anything in them (2026-11-09, 设计稿第 6 节).

Kaus defines no plugin format of its own. Three external layouts are recognised,
each by its manifest file:

| format   | manifest                      | reference                                              |
| -------- | ----------------------------- | ------------------------------------------------------ |
| ``claude`` | ``.claude-plugin/plugin.json`` | code.claude.com/docs/en/plugins-reference             |
| ``codex``  | ``.codex-plugin/plugin.json``  | openai/codex ``codex-rs/core-plugins/src/manifest.rs`` |
| ``gemini`` | ``gemini-extension.json``      | geminicli.com/docs/extensions (reference.md)           |

A marketplace is only a repository with a manifest listing plugins
(``.claude-plugin/marketplace.json``; Codex ``.agents/plugins/marketplace.json``,
``codex-rs/core-plugins/src/marketplace.rs``). Inspecting one lists its plugins;
the user picks one. No marketplace API is called.

Everything here is filesystem reading: manifests are parsed as JSON, skill
directories are counted by their ``SKILL.md``, MCP server definitions are read
from ``.mcp.json`` / the manifest. Hooks, commands and agents are only detected
(``bool``); their contents are never loaded or run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

FORMATS: Final[tuple[str, ...]] = ("claude", "codex", "gemini")
MANIFESTS: Final[dict[str, str]] = {
    "claude": ".claude-plugin/plugin.json",
    "codex": ".codex-plugin/plugin.json",
    "gemini": "gemini-extension.json",
}
MARKETPLACE_MANIFESTS: Final[tuple[tuple[str, str], ...]] = (
    ("codex", ".agents/plugins/marketplace.json"),
    ("claude", ".claude-plugin/marketplace.json"),
)
#: Package-root placeholders the formats use inside MCP definitions. All are
#: substituted regardless of format: a plugin written for one loader is often
#: reused as-is by another.
ROOT_PLACEHOLDERS: Final[tuple[str, ...]] = ("${CLAUDE_PLUGIN_ROOT}", "${extensionPath}", "${CODEX_PLUGIN_ROOT}")

#: Copy limits for an installed package (common.py uses 4000 / 50 MB for one skill).
MAX_FILES: Final[int] = 5000
MAX_BYTES: Final[int] = 200 * 1024 * 1024
#: Never copied into the library.
IGNORED_DIRS: Final[frozenset[str]] = frozenset({".git", "node_modules", "__pycache__", ".DS_Store"})
_MANIFEST_LIMIT: Final[int] = 1024 * 1024
_RELATIVE_RE: Final[re.Pattern[str]] = re.compile(r"^\.(/|$)")


class PackageError(Exception):
    """A package could not be read; ``code`` is the stable API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PluginPackage:
    path: Path
    format: str
    name: str
    version: str | None
    description: str
    #: Skill directories (each holds a ``SKILL.md``), relative to ``path``.
    skills: tuple[str, ...] = ()
    #: MCP server definitions by name, placeholders substituted (see :data:`ROOT_PLACEHOLDERS`).
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    hooks: bool = False
    commands: bool = False
    agents: bool = False

    def contents(self) -> dict[str, Any]:
        """The API ``contents`` object: counts and names only."""
        return {"skills": len(self.skills), "mcpServers": sorted(self.mcp_servers),
                "hooks": self.hooks, "commands": self.commands, "agents": self.agents}

    def skill_dirs(self) -> tuple[Path, ...]:
        return tuple(self.path / relative for relative in self.skills)


@dataclass(frozen=True)
class MarketplaceEntry:
    name: str
    description: str
    #: Relative path inside the marketplace (``./plugins/x``) or an external Git URL.
    source: str
    ref: str | None = None
    #: Subdirectory inside an external repository (Codex ``git-subdir``).
    subdir: str | None = None

    @property
    def is_relative(self) -> bool:
        return bool(_RELATIVE_RE.match(self.source))


@dataclass(frozen=True)
class Marketplace:
    path: Path
    format: str
    name: str
    plugins: tuple[MarketplaceEntry, ...]

    def find(self, name: str) -> MarketplaceEntry | None:
        return next((entry for entry in self.plugins if entry.name == name), None)


def _read_json(path: Path, *, code: str = "invalid_manifest") -> Any:
    try:
        if path.stat().st_size > _MANIFEST_LIMIT:
            raise PackageError(code, f"{path.name} 超过 1 MB")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageError(code, f"{path.name} 不是有效的 JSON：{exc}") from exc


def _inside(root: Path, relative: str) -> Path | None:
    """``./x`` style path resolved under ``root``; ``None`` when it escapes."""
    if not isinstance(relative, str) or ".." in Path(relative).parts:
        return None
    candidate = (root / relative.removeprefix("./")).resolve() if relative not in (".", "./") else root.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def detect_format(root: Path) -> str | None:
    """Which plugin format a directory is, by manifest; ``None`` when none."""
    for fmt in FORMATS:
        if (root / MANIFESTS[fmt]).is_file():
            return fmt
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _skill_dirs(root: Path, manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Skill directories: manifest ``skills`` entries plus the conventional ``skills/``."""
    roots: list[Path] = []
    for raw in _string_list(manifest.get("skills")):
        candidate = _inside(root, raw)
        if candidate is not None and candidate.is_dir():
            roots.append(candidate)
    default = root / "skills"
    if default.is_dir() and default.resolve() not in {r.resolve() for r in roots}:
        roots.append(default)
    found: list[str] = []
    for base in roots:
        candidates = [base] if (base / "SKILL.md").is_file() else sorted(
            child for child in base.iterdir() if child.is_dir() and not child.is_symlink())
        for folder in candidates:
            if (folder / "SKILL.md").is_file():
                relative = str(folder.resolve().relative_to(root.resolve()))
                if relative not in found:
                    found.append(relative)
    return tuple(found)


def substitute_root(value: Any, root: Path) -> Any:
    """Replace package-root placeholders in strings (recursively through lists/maps)."""
    if isinstance(value, str):
        for placeholder in ROOT_PLACEHOLDERS:
            value = value.replace(placeholder, str(root))
        return value
    if isinstance(value, list):
        return [substitute_root(item, root) for item in value]
    if isinstance(value, dict):
        return {key: substitute_root(item, root) for key, item in value.items()}
    return value


def _normalize_server(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Gemini's ``httpUrl`` becomes the common ``url`` + ``type: http``; others pass through."""
    server = dict(raw)
    if "httpUrl" in server and "url" not in server:
        server["url"] = server.pop("httpUrl")
        server.setdefault("type", "http")
    return server


def _servers_from(value: Any, root: Path) -> dict[str, dict[str, Any]]:
    """A ``{"mcpServers": {...}}`` document or a bare ``{name: server}`` map → servers."""
    if not isinstance(value, Mapping):
        return {}
    table = value.get("mcpServers") if isinstance(value.get("mcpServers"), Mapping) else value
    servers: dict[str, dict[str, Any]] = {}
    for name, raw in table.items():
        if isinstance(name, str) and name and isinstance(raw, Mapping):
            servers[name] = _normalize_server(substitute_root(dict(raw), root))
    return servers


def _mcp_servers(root: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    declared = manifest.get("mcpServers")
    if isinstance(declared, Mapping):
        servers.update(_servers_from(declared, root))
    for raw in _string_list(declared):
        path = _inside(root, raw)
        if path is not None and path.is_file():
            servers.update(_servers_from(_read_json(path), root))
    default = root / ".mcp.json"
    if default.is_file() and not isinstance(declared, (str, list)):
        servers.update(_servers_from(_read_json(default), root))
    return servers


def _has_files(folder: Path) -> bool:
    return folder.is_dir() and any(child.is_file() for child in folder.rglob("*"))


def _declared_or_dir(root: Path, manifest: Mapping[str, Any], key: str, folder: str) -> bool:
    value = manifest.get(key)
    if isinstance(value, (Mapping, list)) and value:
        return True
    if isinstance(value, str):
        path = _inside(root, value)
        if path is not None and (path.is_file() or _has_files(path)):
            return True
    return _has_files(root / folder)


def read_package(root: Path) -> PluginPackage:
    """Parse a plugin directory. Raises :class:`PackageError` (``not_a_plugin`` / ``invalid_manifest``)."""
    root = Path(root)
    if not root.is_dir():
        raise PackageError("source_not_found", f"目录不存在：{root}")
    fmt = detect_format(root)
    if fmt is None:
        raise PackageError("not_a_plugin", "目录里没有 .claude-plugin/plugin.json、.codex-plugin/plugin.json 或 gemini-extension.json")
    manifest = _read_json(root / MANIFESTS[fmt])
    if not isinstance(manifest, Mapping):
        raise PackageError("invalid_manifest", f"{MANIFESTS[fmt]} 的顶层必须是对象")
    name = manifest.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else root.name
    version = manifest.get("version")
    description = manifest.get("description")
    hooks = _declared_or_dir(root, manifest, "hooks", "hooks") or (root / "hooks" / "hooks.json").is_file()
    return PluginPackage(
        path=root, format=fmt, name=name[:200],
        version=str(version).strip()[:64] if isinstance(version, (str, int, float)) and str(version).strip() else None,
        description=description[:1200] if isinstance(description, str) else "",
        skills=_skill_dirs(root, manifest), mcp_servers=_mcp_servers(root, manifest),
        hooks=hooks, commands=_declared_or_dir(root, manifest, "commands", "commands"),
        agents=_declared_or_dir(root, manifest, "agents", "agents"),
    )


def _marketplace_entry(raw: Mapping[str, Any]) -> MarketplaceEntry | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    source = raw.get("source")
    ref = subdir = None
    if isinstance(source, Mapping):
        kind = source.get("source")
        if kind == "local":
            location = source.get("path")
        elif kind == "github":
            repo = source.get("repo")
            location = f"https://github.com/{repo}.git" if isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else None
            ref = source.get("ref") if isinstance(source.get("ref"), str) else None
        elif kind in ("url", "git", "git-subdir"):
            location = source.get("url")
            ref = source.get("ref") if isinstance(source.get("ref"), str) else None
            subdir = source.get("path") if isinstance(source.get("path"), str) else None
        else:
            location = None
    else:
        location = source
    if not isinstance(location, str) or not location.strip():
        return None
    description = raw.get("description")
    return MarketplaceEntry(name=name.strip()[:200], description=description[:1200] if isinstance(description, str) else "",
                            source=location.strip(), ref=ref, subdir=subdir)


def read_marketplace(root: Path) -> Marketplace | None:
    """A marketplace manifest at ``root``, or ``None`` when the directory has none."""
    root = Path(root)
    for fmt, relative in MARKETPLACE_MANIFESTS:
        path = root / relative
        if not path.is_file():
            continue
        manifest = _read_json(path)
        if not isinstance(manifest, Mapping) or not isinstance(manifest.get("plugins"), list):
            raise PackageError("invalid_manifest", f"{relative} 缺少 plugins 列表")
        entries = tuple(entry for entry in (_marketplace_entry(item) for item in manifest["plugins"]
                                            if isinstance(item, Mapping)) if entry is not None)
        name = manifest.get("name")
        return Marketplace(path=root, format=fmt, name=name.strip() if isinstance(name, str) and name.strip() else root.name,
                           plugins=entries)
    return None


def resolve_relative(root: Path, relative: str) -> Path | None:
    """A marketplace entry's relative source → directory inside the marketplace (or ``None``)."""
    return _inside(root, relative)


def copy_package(source: Path, target: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> tuple[int, int]:
    """Copy a package tree without following symlinks; returns ``(files, bytes)``.

    Symlinks are refused rather than skipped (a skipped link is a silent loss, a
    followed one can leave the source tree). ``.git`` and the other
    :data:`IGNORED_DIRS` are not copied. The target must not exist.
    """
    source = Path(source)
    target = Path(target)
    if target.exists():
        raise PackageError("install_failed", "目标目录已存在")
    check_limits(source, max_files=max_files, max_bytes=max_bytes)
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in IGNORED_DIRS)
        relative = Path(current).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in IGNORED_DIRS:
                continue
            shutil.copy2(Path(current) / name, target / relative / name, follow_symlinks=False)
    return check_limits(target, max_files=max_files, max_bytes=max_bytes)


def check_limits(root: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> tuple[int, int]:
    """Size / count check of a tree already on disk (a fresh clone); symlinks refused."""
    count = size = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d not in IGNORED_DIRS]
        for name in [*directories, *files]:
            path = Path(current) / name
            if path.is_symlink():
                raise PackageError("symlink_not_allowed", f"包里含符号链接：{path.relative_to(root)}")
        for name in files:
            if name in IGNORED_DIRS:
                continue
            count += 1
            size += (Path(current) / name).stat().st_size
            if count > max_files or size > max_bytes:
                raise PackageError("too_large", f"包超过复制限制（{max_files} 个文件 / {max_bytes // (1024 * 1024)} MB）")
    return count, size


__all__ = [
    "FORMATS",
    "MANIFESTS",
    "MARKETPLACE_MANIFESTS",
    "MAX_BYTES",
    "MAX_FILES",
    "Marketplace",
    "MarketplaceEntry",
    "PackageError",
    "PluginPackage",
    "check_limits",
    "copy_package",
    "detect_format",
    "read_marketplace",
    "read_package",
    "resolve_relative",
    "substitute_root",
]
