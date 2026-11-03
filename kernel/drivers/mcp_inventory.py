"""Read-only discovery of an Agent's native global MCP server definitions.

Sources are declared per preset in ``native_mcp_sources.json`` (storage
conventions, not behaviour). Custom drivers may expose ``mcp_inventory_sources``
with :class:`NativeMcpSource` values. No subprocesses, model calls, directory
creation or native configuration writes.

Secrets never enter the record: environment and header *values* are dropped
(only their names are kept), URLs lose user info, query and fragment, and
arguments that look like credentials are omitted. Records are catalogue entries
of the backend-scoped type ``<agent>:mcp``; the source Agent keeps loading the
definition natively, so the adapter boundary never injects them again
(see ``app.capabilities.effective``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from drivers.secret_guard import SUSPICIOUS_KEY_RE

SOURCES: dict[str, tuple[dict, ...]] = {
    key: tuple(value) for key, value in
    json.loads(Path(__file__).with_name("native_mcp_sources.json").read_text()).items()}
MAX_BYTES = 64 * 1024 * 1024
_SAFE_ARG = re.compile(r"^[\w@./:+-]{1,120}$")
_TOKEN_LIKE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-]{24,}$")
_CACHE: dict[tuple[str, int, int], Any] = {}


@dataclass(frozen=True)
class NativeMcpSource:
    path: Path
    agent_key: str
    agent_label: str
    capability_scope: str
    project_id: str
    format: str = "json"
    key: str = "mcpServers"
    shape: str = "standard"


def _expand(raw: str, home: Path, env: Mapping[str, str]) -> Path:
    if raw.startswith("$XDG_CONFIG_HOME/"):
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        return base / raw.removeprefix("$XDG_CONFIG_HOME/")
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def preset_sources(preset_id: str, *, agent_key: str, agent_label: str, project_id: str,
                   home: Path, env: Mapping[str, str]) -> tuple[NativeMcpSource, ...]:
    """Resolve the declared files, honouring the preset's config-root override."""
    result = []
    for spec in SOURCES.get(preset_id, ()):
        override = env.get(spec["env"]) if spec.get("env") else None
        if override:
            path = _expand(override, home, env) / spec["envPath"]
        else:
            path = _expand(spec["path"], home, env)
        result.append(NativeMcpSource(path, agent_key, agent_label, preset_id, project_id,
                                      spec.get("format", "json"), spec.get("key", "mcpServers"),
                                      spec.get("shape", "standard")))
    return tuple(result)


def configured_mcp_sources(registry, projects, bindings=(), *, home: Path | None = None,
                           env: Mapping[str, str] | None = None) -> tuple[NativeMcpSource, ...]:
    """Sources of connected Agents only; absent files are harmless."""
    home = home or Path.home()
    root = next((p.id for p in projects if p.slug == "default"), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if root is None or registry is None:
        return ()
    project_ids = {p.id for p in projects}
    result: list[NativeMcpSource] = []
    seen: set[tuple[str, Path]] = set()
    for driver in registry.list_drivers():
        preset = getattr(driver, "preset", None)
        preset_id = getattr(preset, "id", None) or driver.backend_id.removeprefix("backend:")
        agent_key = getattr(preset, "skill_scope_key", None) or preset_id
        label = getattr(preset, "label", None) or agent_key
        if env is None:
            spec = getattr(driver, "agent_spec", None)
            try:
                source_env = spec.build_env() if spec is not None else os.environ
            except Exception:  # noqa: BLE001 - inventory never blocks the tree
                source_env = os.environ
        else:
            source_env = env
        specs = list(preset_sources(preset_id, agent_key=agent_key, agent_label=label,
                                    project_id=root, home=home, env=source_env))
        provider = getattr(driver, "mcp_inventory_sources", ())
        extra = provider(projects, bindings) if callable(provider) else provider
        specs.extend(s for s in extra if isinstance(s, NativeMcpSource) and s.project_id in project_ids)
        for source in specs:
            if (source.agent_key, source.path) not in seen:
                seen.add((source.agent_key, source.path))
                result.append(source)
    return tuple(result)


def strip_jsonc(text: str) -> str:
    """Remove comments and trailing commas outside strings."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True; out.append(c); i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue
            out.append(c); i += 1
        else:
            out.append(c); i += 1
    return "".join(out)


def _servers(source: NativeMcpSource):
    """Only the MCP section is retained; the cache key follows file identity."""
    path = source.path.expanduser()
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_BYTES:
            return None, None
        resolved = path.resolve()
        key = (f"{source.format}\0{source.key}\0{resolved}", stat.st_mtime_ns, stat.st_size)
        if key not in _CACHE:
            text = resolved.read_text(encoding="utf-8")
            if source.format == "toml":
                parsed = tomllib.loads(text)
            else:
                parsed = json.loads(strip_jsonc(text) if source.format == "jsonc" else text)
            section = parsed.get(source.key) if isinstance(parsed, Mapping) else None
            if len(_CACHE) > 32:
                _CACHE.clear()
            _CACHE[key] = section if isinstance(section, Mapping) else None
        return _CACHE[key], resolved
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, None


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path, "", ""))[:300]


def safe_args(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    hide_next = False
    for value in values[:24]:
        if not isinstance(value, str):
            continue
        hide, hide_next = hide_next, False
        flag = value.split("=", 1)[0]
        if flag.startswith("-") and SUSPICIOUS_KEY_RE.search(flag.lstrip("-")):
            hide_next = "=" not in value
            continue
        if hide or "=" in value or "://" in value or not _SAFE_ARG.match(value) or _TOKEN_LIKE.match(value):
            continue
        result.append(value)
        if len(result) == 8:
            break
    return result


def _names(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return sorted(str(k)[:120] for k in value)
    if isinstance(value, list):
        return sorted(str(k)[:120] for k in value if isinstance(k, str))
    return []


def server_summary(raw: Mapping[str, Any], shape: str) -> dict | None:
    """Public, credential-free description of one native server definition."""
    if raw.get("enabled") is False or raw.get("disabled") is True:
        return None
    env_keys: list[str] = []
    header_keys: list[str] = []
    command, args, url, transport = None, [], None, None
    if shape == "opencode":
        kind = raw.get("type")
        if kind == "remote":
            transport, url = "http", safe_url(raw.get("url"))
            header_keys = _names(raw.get("headers"))
        else:
            parts = raw.get("command")
            if isinstance(parts, list) and parts and isinstance(parts[0], str):
                command, args = parts[0], safe_args(parts[1:])
            transport = "stdio"
            env_keys = _names(raw.get("environment"))
    else:
        command = raw.get("command") if isinstance(raw.get("command"), str) else None
        args = safe_args(raw.get("args"))
        env_keys = _names(raw.get("env"))
        header_keys = _names(raw.get("headers"))
        if shape == "codex":
            env_keys = sorted({*env_keys, *_names(raw.get("env_vars"))}
                              | ({raw["bearer_token_env_var"]} if isinstance(raw.get("bearer_token_env_var"), str) else set()))
            header_keys = sorted({*_names(raw.get("http_headers")), *_names(raw.get("env_http_headers"))})
        if shape == "gemini" and raw.get("httpUrl"):
            transport, url = "http", safe_url(raw.get("httpUrl"))
        elif raw.get("url"):
            declared = raw.get("type") or raw.get("transport")
            transport = "sse" if declared == "sse" or shape == "gemini" else "http"
            url = safe_url(raw.get("url"))
        elif command:
            transport = "stdio"
    if transport is None or (transport == "stdio" and not command) or (transport != "stdio" and not url):
        return None
    summary: dict[str, Any] = {"transport": transport}
    if command:
        summary["command"] = command[:300]
        if args:
            summary["args"] = args
    if url:
        summary["url"] = url
    if env_keys:
        summary["envKeys"] = env_keys[:40]
    if header_keys:
        summary["headerKeys"] = header_keys[:40]
    return summary


def _description(summary: Mapping[str, Any]) -> str:
    if summary.get("url"):
        target = summary["url"]
    else:
        target = " ".join([Path(summary["command"]).name, *summary.get("args", ())])
    return f"{summary['transport']} · {target}"[:400]


def discover_mcp(sources: Iterable[NativeMcpSource]) -> list[dict]:
    result: dict[str, dict] = {}
    for source in sources:
        servers, resolved = _servers(source)
        if not isinstance(servers, Mapping):
            continue
        for name, raw in servers.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(raw, Mapping):
                continue
            summary = server_summary(raw, source.shape)
            if summary is None:
                continue
            identity = "\0".join(("mcp", source.agent_key, str(resolved), name))
            record_id = "native-mcp-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            if record_id in result:
                continue
            result[record_id] = {
                "id": record_id,
                "kind": "mcp",
                "name": name[:200],
                "description": _description(summary),
                "agentKey": source.agent_key,
                "agentLabel": source.agent_label,
                "sourcePath": str(resolved),
                "available": True,
                "defaultProjectId": source.project_id,
                "defaultProjectIds": [source.project_id],
                "defaultBlockedProjectIds": [],
                "capabilityType": source.capability_scope + ":mcp",
                "capabilityId": record_id,
                "server": summary,
            }
    return list(result.values())
