"""Apply computer-use selection to an existing ACP projection.

Tools stay in their native Agent/MCP loop. This module changes only the
per-session projection, and never writes a source configuration or Skill plan.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
import tomllib
from .discovery import discover_project, fallback_allowed, known_provider
from .policy import CAPABILITIES, from_native, effective_controls, project_controls, select_provider
from drivers.base import UnsupportedCapabilityError

_native_cache: dict[tuple, tuple[float, dict]] = {}
_quarantined: dict[str, tuple] = {}
_quarantine_monitors: set[asyncio.Task] = set()
_QUARANTINE_POLL_SECONDS = 1.0


def server_fingerprint(server):
    """Identify a host-injected declaration without publishing its credentials."""
    return hashlib.sha256(json.dumps(server, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def quarantine(leases, confirm, spec, session_id, metadata, *, monitor=False):
    """Retain a remote resource until its native service confirms termination."""
    leases = tuple(leases)
    for lease in leases:
        _quarantined[lease.resource_id] = (lease, confirm, spec, session_id, metadata)
    if monitor and leases:
        def owns_any():
            return any((current := _quarantined.get(lease.resource_id)) is not None
                       and current[0] is lease for lease in leases)

        async def observe_until_complete():
            while owns_any():
                await asyncio.sleep(_QUARANTINE_POLL_SECONDS)
                if not owns_any():
                    return
                try:
                    stopped = await asyncio.wait_for(confirm(spec, session_id, metadata), timeout=8)
                except Exception:
                    stopped = False
                if stopped is True:
                    release_quarantine(leases)
                    return
        task = asyncio.create_task(observe_until_complete())
        _quarantine_monitors.add(task)
        task.add_done_callback(_quarantine_monitors.discard)


def release_quarantine(leases):
    """Release only the recorded ownership, never a newer holder of a resource."""
    for lease in leases:
        current = _quarantined.get(lease.resource_id)
        if current is not None and current[0] is lease:
            lease.release()
            _quarantined.pop(lease.resource_id)


async def reconcile_resources():
    from .durable import reconcile_persisted
    for resource, (lease, confirm, spec, session_id, metadata) in list(_quarantined.items()):
        try:
            stopped = await asyncio.wait_for(confirm(spec, session_id, metadata), timeout=8)
        except Exception:
            stopped = False
        if stopped is True:
            release_quarantine([lease])
    persisted = await reconcile_persisted()
    from .cua_guard import reconcile as reconcile_desktop
    desktop = () if await asyncio.to_thread(reconcile_desktop) else ("desktop:local",)
    return tuple(dict.fromkeys([*_quarantined, *persisted, *desktop]))


def _read_config(path: Path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        raw = path.read_text()
        value = tomllib.loads(raw) if path.suffix == ".toml" else json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def ambient_mcp(spec, adapter):
    """Read only tool declarations from the native homes actually being used."""
    name = getattr(adapter, "__name__", "").rsplit(".", 1)[-1]
    env = spec.build_env()
    home = Path(env.get("HOME") or Path.home())
    rows = []
    if name == "codex":
        files = [(Path(env.get("CODEX_HOME") or home / ".codex") / "config.toml", "mcp_servers")]
        if spec.cwd:
            files.append((Path(spec.cwd) / ".codex/config.toml", "mcp_servers"))
    elif name == "claude":
        files = [(Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "settings.json", "mcpServers"),
                 (home / ".claude.json", "mcpServers")]
        if spec.cwd:
            files.append((Path(spec.cwd) / ".mcp.json", "mcpServers"))
    else:
        return ()
    for path, key in files:
        config = _read_config(path)
        servers = config.get(key)
        if not isinstance(servers, dict):
            continue
        disabled = config.get("disabledMcpServers", [])
        for server, value in servers.items():
            if isinstance(value, dict):
                rows.append({"name": server, **value, **({"disabled": True} if server in disabled else {})})
    # A declaration is not proof that a project is trusted or its MCP is loaded.
    # Keep a potential shared-resource lease without claiming verified readiness.
    return tuple(replace(candidate, source="native", provider_id=f"{name}:{candidate.provider_id}",
                         status="unknown" if candidate.status == "ready" else candidate.status)
                 for candidate in discover_project(rows))


async def native_status(adapter, spec, binding=None, *, force=False):
    probe = getattr(adapter, "probe_computer_use", None)
    if probe is None:
        return {}
    # Identity includes the private runtime environment; no cross-project reuse.
    key = (getattr(adapter, "__name__", ""), tuple(spec.command), spec.cwd,
           tuple(sorted(spec.env.items())), getattr(binding, "id", None))
    cached = _native_cache.get(key)
    if not force and cached and time.monotonic() - cached[0] < 15:
        return cached[1]
    try:
        result = await asyncio.wait_for(probe(spec, binding=binding), timeout=15)
        if not isinstance(result, dict) or not result:
            raise ValueError("Native capability probe returned no result")
    except (Exception,):
        # An inaccessible native service may still be operating. Do not treat
        # a failed probe as permission to inject an alternate controller.
        result = {cap: {"status": "unknown", "provider": "native", "exposed": True,
                        "requirements": ["native_probe_incomplete"]} for cap in CAPABILITIES}
    if len(_native_cache) > 128:
        _native_cache.clear()
    _native_cache[key] = (time.monotonic(), result)
    return result


def _controls(effective):
    return effective_controls(effective)


async def prepare_projection(spec, adapter, binding, conversation, effective, metadata, *, supports_mcp=True):
    """Return private per-runtime metadata; preserve unrelated MCP and all Skills."""
    metadata = dict(metadata)
    if metadata.get("_kausExecutionPolicy") == "context-only":
        metadata.pop("_kausComputerUse", None)
        return metadata
    from . import settings
    enabled = settings.is_enabled()
    if not enabled:
        previous = metadata.get("_kausComputerUse", {})
        owned = {row["name"]: row["fingerprint"] for row in previous.get("autoInjectedServers", [])}
        if owned:
            metadata["mcpServers"] = [server for server in metadata.get("mcpServers", [])
                if owned.get(server.get("name")) != server_fingerprint(server)]
    native_permission = getattr(adapter, "native_computer_use_permission_control", None)
    agent_managed = (native_permission is not None and
        native_permission(effective, metadata, conversation=conversation) == "agent")
    # Existing source-Agent permissions are not revoked by a host tool opt-in.
    # Their exposed tools still participate in shared-resource ownership.
    native = list(from_native(await native_status(adapter, spec, binding))) if enabled or agent_managed else []
    native.extend(ambient_mcp(spec, adapter))
    project = discover_project(effective, context_id=conversation.id)
    # A source may explicitly enable its native service while status is unknown.
    # It still owns that resource and must not race a second conversation.
    from . import probe as probes
    controls = project_controls(effective) if agent_managed else _controls(effective)
    fallback_scope = getattr(adapter, "computer_use_fallback_allowed", None)
    allow_fallback = enabled and supports_mcp and fallback_allowed(effective) and (
        fallback_scope is None or fallback_scope(effective, metadata) is True
    )
    candidates = (*native, *project) if enabled else tuple(native)
    needed = []
    for cap in CAPABILITIES:
        primary = select_provider(cap, candidates, enabled=controls[cap], allow_fallback=False)
        if primary.status not in {"ready", "disabled", "denied"} and not (
                primary.candidate and primary.candidate.exposed):
            needed.append(cap)
    fallbacks = await probes.probe_fallbacks(context_id=conversation.id, capabilities=tuple(needed)) if (
        needed and allow_fallback) else ()
    selections = {
        cap: select_provider(cap, (*candidates, *fallbacks),
                             enabled=controls[cap], allow_fallback=allow_fallback)
        for cap in CAPABILITIES
    }
    controls = {cap: controls[cap] and selections[cap].status not in {"disabled", "denied"}
                for cap in CAPABILITIES}
    policy_scopes = getattr(adapter, "COMPUTER_USE_POLICY_SCOPES", ())
    from . import native_policy
    native_disabled = []
    for candidate in native:
        if candidate.exposed and not controls[candidate.capability] and (
                candidate.capability not in policy_scopes):
            if candidate.mcp_server is not None and native_policy.supported(adapter):
                native_disabled.append(candidate.mcp_server["name"])
            else:
                raise UnsupportedCapabilityError("原生电脑工具与项目权限冲突，请调整工具配置")
    servers = [dict(server) for server in metadata.get("mcpServers", ())]
    remove = set()
    for candidate in project:
        if not enabled:
            remove.add(candidate.provider_id)
            continue
        raw = candidate.mcp_server or {}
        annotation = raw.get("kausComputerUse", {})
        dedicated = known_provider(raw) is not None or (
            isinstance(annotation, dict) and annotation.get("exclusive") is True)
        if not dedicated:
            if not controls[candidate.capability]:
                raise UnsupportedCapabilityError("此 MCP 包含已关闭的电脑工具，请调整项目 MCP 配置")
            continue
        covered = [item for item in project if item.provider_id == candidate.provider_id]
        # A multi-capability server cannot keep exposing a forbidden capability.
        if any(not controls[item.capability] for item in covered):
            if any(controls[item.capability] and selections[item.capability].candidate is not None
                   and selections[item.capability].candidate.provider_id == candidate.provider_id
                   for item in covered):
                raise UnsupportedCapabilityError("此 MCP 无法分别关闭电脑工具，请调整项目 MCP 配置")
            remove.add(candidate.provider_id)
            continue
        if all(selections[item.capability].status in {"disabled", "denied"} or (
                selections[item.capability].candidate is not None and
                selections[item.capability].candidate.source == "native" and
                selections[item.capability].status == "ready") for item in covered):
            remove.add(candidate.provider_id)
    servers = [server for server in servers if server.get("name") not in remove]
    names = {server.get("name") for server in servers}
    auto_injected = {}
    for capability, selection in selections.items():
        candidate = selection.selected
        if candidate and candidate.source == "fallback" and candidate.mcp_server:
            server = dict(candidate.mcp_server)
            if server.get("name") not in names:
                servers.append(server)
                names.add(server.get("name"))
                auto_injected[server.get("name")] = {
                    "name": server.get("name"), "capabilities": [],
                    "fingerprint": server_fingerprint(server),
                }
            injected = auto_injected.get(server.get("name"))
            if injected and injected["fingerprint"] == server_fingerprint(server):
                injected["capabilities"].append(capability)
    if servers or "mcpServers" in metadata:
        metadata["mcpServers"] = servers
    resources = set()
    for selection in selections.values():
        candidate = selection.candidate
        if candidate and (selection.status == "ready" or candidate.exposed) and candidate.resource_id:
            resources.add(candidate.resource_id)
    # Retained mixed-purpose MCPs and pre-existing native tools still need a lease.
    for candidate in (*native, *project):
        if controls[candidate.capability] and candidate.exposed and candidate.resource_id and (
                candidate.source == "native" or candidate.provider_id in names):
            resources.add(candidate.resource_id)
    if not resources and not native and not project and not policy_scopes and not enabled:
        metadata.pop("_kausComputerUse", None)
        return metadata
    if not resources and all(controls.values()) and all(
            item.status == "unavailable" for item in selections.values()):
        # Ordinary conversations without these tools retain their existing
        # projection shape. Missing optional providers belong in setup.
        metadata.pop("_kausComputerUse", None)
        return metadata
    if "browser:shared" in resources:
        # Attached/native browsers can share visible windows with desktop tools.
        # Isolated Playwright contexts do not contend for the local desktop.
        resources.add("desktop:local")
    metadata["_kausComputerUse"] = {**{cap: selection.to_dict() for cap, selection in selections.items()},
                                    "controls": controls,
                                    "enabled": enabled,
                                    "revision": settings.runtime_revision(),
                                    "nativeDisabledServers": sorted(set(native_disabled)),
                                    "resources": sorted(resources),
                                    "autoInjectedServers": list(auto_injected.values()),
                                    **({"nativePermissionControl": "agent"} if agent_managed else {})}
    return metadata
