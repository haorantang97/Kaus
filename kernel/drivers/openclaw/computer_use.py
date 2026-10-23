"""Reuse OpenClaw tool policy and query native readiness without taking control.

Only a Gateway's redacted configuration snapshot is inspected. No native
credentials, endpoints, browser profiles, or execution settings are projected
into a Kaus Agent. Status queries do not start browsers, capture screens, or
invoke a computer action.
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import Mapping

from drivers.acp.client import AcpRpcError, AcpTransportError
from drivers.base import UnsupportedCapabilityError
from .driver import OpenClawGateway


_NAME = re.compile(r"^[A-Za-z0-9_.*?:/-]{1,200}$")
_PROFILES = {"minimal", "coding", "messaging", "full"}
_READ_METHODS = {"config.get", "tools.effective", "computer.status", "node.list", "browser.request"}


def tool_policy(value):
    """Copy only the native tool-policy schema; never arbitrary tool settings."""
    if not isinstance(value, Mapping):
        return {}
    result = {}
    if "profile" in value and (not isinstance(value["profile"], str) or value["profile"] not in _PROFILES):
        raise UnsupportedCapabilityError("原生工具策略无法完整继承")
    if value.get("profile") in _PROFILES:
        result["profile"] = value["profile"]
    for key in ("allow", "alsoAllow", "deny"):
        items = value.get(key)
        if key in value and not isinstance(items, list):
            raise UnsupportedCapabilityError("原生工具策略无法完整继承")
        if isinstance(items, list):
            if not all(isinstance(item, str) and _NAME.fullmatch(item)
                       and item != "__OPENCLAW_REDACTED__" for item in items):
                raise UnsupportedCapabilityError("原生工具策略无法完整继承")
            result[key] = list(items)
    for key in ("byProvider", "toolsBySender"):
        if key in value and not isinstance(value[key], Mapping):
            raise UnsupportedCapabilityError("原生工具策略无法完整继承")
        if isinstance(value.get(key), Mapping):
            result[key] = {name: tool_policy(policy) for name, policy in value[key].items()
                           if isinstance(name, str) and 0 < len(name) <= 256
                           and not any(ord(char) < 32 for char in name)
                           and name != "__OPENCLAW_REDACTED__"
                           and isinstance(policy, Mapping)}
            if len(result[key]) != len(value[key]):
                raise UnsupportedCapabilityError("原生工具策略无法完整继承")
    sandbox = value.get("sandbox")
    if isinstance(sandbox, Mapping) and isinstance(sandbox.get("tools"), Mapping):
        result["sandbox"] = {"tools": tool_policy(sandbox["tools"])}
    return result


def _option(spec, name):
    args = tuple(spec.command)
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None


def source_agent_id(config, spec, binding=None):
    """Resolve an explicit source or an unambiguous native owner; never guess."""
    entries = config.get("agents", {}).get("entries", {})
    if not isinstance(entries, Mapping):
        return None
    runtime = getattr(binding, "runtime_config", {}) or {}
    explicit = runtime.get("native_agent_id")
    session = _option(spec, "--session")
    if not explicit and isinstance(session, str) and session.startswith("agent:"):
        explicit = session.split(":", 2)[1]
    if explicit:
        if explicit not in entries:
            raise UnsupportedCapabilityError("原生 Agent 配置不存在")
        return explicit
    agents = config.get("agents", {})
    system = agents.get("defaults", {}).get("systemAgent", {}).get("agentId")
    if isinstance(system, str) and system in entries:
        return system
    defaults = [name for name, value in entries.items()
                if isinstance(value, Mapping) and value.get("default") is True]
    if len(defaults) == 1:
        return defaults[0]
    candidates = [name for name in entries if not name.startswith("kaus-")]
    return candidates[0] if len(candidates) == 1 else None


def inherited_agent_policy(config, spec, binding=None):
    source = source_agent_id(config, spec, binding)
    entry = config.get("agents", {}).get("entries", {}).get(source, {})
    policy = tool_policy(entry.get("tools")) if isinstance(entry, Mapping) else {}
    return {"tools": policy} if policy else {}


class ReadinessGateway(OpenClawGateway):
    allowed_methods = frozenset(_READ_METHODS)

    async def call(self, method, params):
        if method == "browser.request" and params != {"method": "GET", "path": "/"}:
            raise ValueError("Only browser status can be inspected")
        return await super().call(method, params)


def _row(status="unknown", *, enabled=None, requirement=None, desktop=False):
    return {"provider": "openclaw-native", "status": status, "enabled": enabled,
            "exposed": enabled is not False, "verified": status == "ready",
            "requirements": [requirement] if requirement else [],
            **({"resource": "desktop:local"} if desktop else {})}


def _explicit_policy(config, agent_id, tool):
    entries = config.get("agents", {}).get("entries", {})
    entry = entries.get(agent_id, {}) if isinstance(entries, Mapping) else {}
    scopes = [config.get("tools", {}), entry.get("tools", {})]
    # These are only explicit grants/denials. Native tools.effective remains
    # authoritative for profiles, providers, model vision and sandbox filtering.
    granted = False
    groups = {"group:openclaw", "group:ui" if tool == "browser" else "group:nodes"}
    for scope in scopes:
        if not isinstance(scope, Mapping):
            continue
        for rule in scope.get("deny", []):
            if isinstance(rule, str) and (rule in groups
                                         or fnmatch.fnmatchcase(tool, rule)):
                return False
        granted |= scope.get("profile") == "full"
        granted |= any(isinstance(rule, str) and (rule in groups
                         or fnmatch.fnmatchcase(tool, rule))
                       for rule in [*scope.get("allow", []), *scope.get("alsoAllow", [])])
    return True if granted else None


async def probe_native(spec, binding=None):
    """Return sanitized native browser/desktop facts, with no model/tool action."""
    result = {"browser": _row(requirement="native-status"),
              "desktop": _row(requirement="native-status", desktop=True)}
    gateway = ReadinessGateway(spec, timeout=5)
    try:
        snapshot = await gateway.call("config.get", {})
        config = snapshot.get("config")
        if not isinstance(config, Mapping):
            return result
        agent_id = source_agent_id(config, spec, binding)
        for kind, tool in (("browser", "browser"), ("desktop", "computer")):
            enabled = _explicit_policy(config, agent_id, tool)
            result[kind] = _row("disabled" if enabled is False else "unknown",
                enabled=enabled, requirement="native-tool-policy" if enabled is False else "native-tool-inventory",
                desktop=kind == "desktop")
        browser_config = config.get("browser", {})
        plugins = config.get("plugins", {})
        if (browser_config.get("enabled") is False or plugins.get("enabled") is False
                or plugins.get("entries", {}).get("browser", {}).get("enabled") is False):
            result["browser"] = _row("disabled", enabled=False, requirement="native-browser-disabled")
        inventory = await _read(gateway, "tools.effective", {"agentId": agent_id} if agent_id else {})
        names = None
        if inventory is not None and isinstance(inventory.get("groups"), list):
            names = {tool.get("id") for group in inventory["groups"] if isinstance(group, Mapping)
                     for tool in group.get("tools", []) if isinstance(tool, Mapping)}
        if result["browser"]["status"] not in {"denied", "disabled"}:
            result["browser"] = await _browser_readiness(gateway, names, result["browser"])
        if result["desktop"]["status"] not in {"denied", "disabled"}:
            result["desktop"] = await _desktop_readiness(gateway, names, result["desktop"])
        return result
    except (AcpRpcError, AcpTransportError, OSError, asyncio.TimeoutError, UnsupportedCapabilityError):
        return result
    finally:
        await gateway.close()


async def _read(gateway, method, params):
    try:
        return await gateway.call(method, params)
    except (AcpRpcError, AcpTransportError, OSError, asyncio.TimeoutError):
        return None


async def _browser_readiness(gateway, names, prior):
    if names is None or "browser" not in names:
        return prior
    status = await _read(gateway, "browser.request", {"method": "GET", "path": "/"})
    if status is None:
        return _row(enabled=True, requirement="native-browser-status")
    if status.get("enabled") is False:
        return _row("disabled", enabled=False, requirement="native-browser-disabled")
    if status.get("running") is True and status.get("cdpReady") is True:
        return _row("ready", enabled=True)
    return _row("unavailable", enabled=True, requirement="native-browser-start")


async def _desktop_readiness(gateway, names, prior):
    status = await _read(gateway, "computer.status", {})
    nodes = await _read(gateway, "node.list", {})
    enabled = True if names is not None and "computer" in names else prior["enabled"]
    if status is None:
        return _row(enabled=enabled, requirement="native-desktop-status", desktop=True)
    # A configured Gateway desktop is never redirected to a different node.
    if status.get("configured") is True:
        ready = status.get("available") is True
    elif status.get("configured") is False and nodes is not None:
        ready = any(isinstance(node, Mapping) and node.get("connected") is True
                    and {"computer.act", "screen.snapshot"}.issubset(node.get("commands", []))
                    for node in nodes.get("nodes", []))
    else:
        return _row(enabled=enabled, requirement="native-desktop-status", desktop=True)
    if names is None or "computer" not in names:
        return _row(enabled=enabled, requirement="native-tool-inventory", desktop=True)
    return _row("ready" if ready else "unavailable", enabled=True,
                requirement=None if ready else "native-computer-provider", desktop=True)


probe_computer_use = probe_native


_OWNED_SESSION = re.compile(r"^agent:kaus-[a-f0-9]{24}:kaus$")
_TERMINAL = {"done", "failed", "killed", "timeout"}
_NO_BASELINE = object()


class StopGateway(OpenClawGateway):
    allowed_methods = frozenset({"sessions.abort", "sessions.describe", "sessions.resolve"})

    def __init__(self, spec, *, owned_key=None, **kwargs):
        super().__init__(spec, **kwargs)
        self.owned_key = owned_key

    async def call(self, method, params):
        if method == "sessions.abort" and (
            set(params) != {"key", "clearQueued"}
            or params.get("clearQueued") is not True
            or not (_OWNED_SESSION.fullmatch(str(params.get("key", "")))
                    or params.get("key") == self.owned_key)
        ):
            raise ValueError("Only an owned Kaus session can be stopped")
        return await super().call(method, params)


class CompletionGateway(OpenClawGateway):
    allowed_methods = frozenset({"sessions.describe", "sessions.resolve"})


async def computer_use_reference(spec, session_id, metadata=None):
    from .source_session import _connection_id
    route = metadata.get("_meta", {}) if isinstance(metadata, Mapping) else {}
    key = route.get("sessionKey") if isinstance(route, Mapping) else None
    reference = {"schema": 1, "connectionId": _connection_id(spec), "nativeSessionId": session_id,
                 "baselineKnown": False, **({"sessionKey": key} if isinstance(key, str) else {})}
    gateway = CompletionGateway(spec, timeout=5)
    try:
        key = key or await gateway.session_key(session_id, require_existing=True)
        snapshot = await gateway.call("sessions.describe", {"key": key})
        session = snapshot.get("session")
        if "session" in snapshot and (session is None or isinstance(session, Mapping) and session.get("key") == key):
            previous = session.get("lastRunId") if session else None
            if previous is None or isinstance(previous, str):
                reference.update(sessionKey=key, baselineKnown=True, previousRunId=previous)
    except (AcpRpcError, AcpTransportError, OSError, ValueError, asyncio.TimeoutError):
        pass
    finally:
        await gateway.close()
    return reference


async def recover_computer_use(spec, reference):
    from .source_session import _connection_id
    if (reference.get("schema") != 1 or reference.get("connectionId") != _connection_id(spec)
            or not isinstance(reference.get("nativeSessionId"), str)
            or reference.get("baselineKnown") is not True):
        return False
    key = reference.get("sessionKey")
    metadata = {"_meta": {"sessionKey": key}} if isinstance(key, str) else {}
    return await confirm_computer_use_complete(spec, reference["nativeSessionId"], metadata,
                                               after_run=reference.get("previousRunId"))


async def confirm_computer_use_complete(spec, session_id, metadata=None, *, after_run=_NO_BASELINE):
    """Observe parent and descendant work after a reply; never abort success."""
    route = metadata.get("_meta", {}) if isinstance(metadata, Mapping) else {}
    key = route.get("sessionKey") if isinstance(route, Mapping) else None
    gateway = CompletionGateway(spec, timeout=5)
    try:
        resolved = key or await gateway.session_key(session_id, require_existing=True)
        return await _terminal_tree(gateway, resolved, after_run=after_run)
    except (AcpRpcError, AcpTransportError, OSError, ValueError, asyncio.TimeoutError):
        return False
    finally:
        await gateway.close()


async def abort_computer_use(spec, session_id, ownership=None):
    """Stop an owned native session and confirm its projected execution ended.

    ACP disconnect is not a Gateway stop. Native sessions.abort cancels queued
    work and descendants; its acknowledgement alone does not release a lease.
    Missing/older status contracts leave the resource held for reconciliation.
    """
    from .source_session import owns_session
    if not isinstance(session_id, str) or not (
        _OWNED_SESSION.fullmatch(session_id) or owns_session(spec, session_id, ownership)
    ):
        return False
    gateway = StopGateway(spec, timeout=5, owned_key=session_id)
    try:
        stopped = await gateway.call("sessions.abort", {"key": session_id, "clearQueued": True})
        if stopped.get("ok") is not True:
            return False
        return await _terminal_tree(gateway, session_id)
    except (AcpRpcError, AcpTransportError, OSError, asyncio.TimeoutError):
        return False
    finally:
        await gateway.close()

async def _terminal_tree(gateway, session_id, *, after_run=_NO_BASELINE):
    pending, seen = [session_id], set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 64:
            return False
        response = await gateway.call("sessions.describe", {"key": key})
        session = response.get("session")
        if not isinstance(session, Mapping) or session.get("key") != key:
            return False
        if key == session_id and after_run is not _NO_BASELINE and (
                not isinstance(session.get("lastRunId"), str) or session["lastRunId"] == after_run):
            return False
        if session.get("status") not in _TERMINAL:
            return False
        if session.get("hasActiveSubagentRun") is True:
            return False
        swarm = session.get("swarm", {})
        if isinstance(swarm, Mapping):
            if swarm.get("otherActiveGroups", 0):
                return False
            if any(group.get("queued", 0) or group.get("running", 0)
                   for group in swarm.get("groups", []) if isinstance(group, Mapping)):
                return False
        children = session.get("childSessions", [])
        if not isinstance(children, list) or not all(isinstance(child, str) for child in children):
            return False
        pending.extend(children)
    return True


async def stop_computer_use(spec, session_id, metadata=None):
    # ACP UUIDs are bridge identities, not the Gateway Agent session route.
    route = metadata.get("_meta", {}) if isinstance(metadata, Mapping) else {}
    key = route.get("sessionKey") if isinstance(route, Mapping) else None
    ownership = metadata.get('_kausOpenClawOwnership') if isinstance(metadata, Mapping) else None
    from .source_session import owns_session
    if isinstance(key, str) and (_OWNED_SESSION.fullmatch(key) or owns_session(spec, key, ownership)):
        return await abort_computer_use(spec, key, ownership)
    if isinstance(session_id, str) and _OWNED_SESSION.fullmatch(session_id):
        return await abort_computer_use(spec, session_id, ownership)
    # Older native conversations retain their original route and identity.
    # Without an ownership receipt, only observe completion; never cancel them.
    gateway = StopGateway(spec, timeout=5)
    try:
        resolved = key or await gateway.session_key(session_id, require_existing=True)
        return await _terminal_tree(gateway, resolved)
    except (AcpRpcError, AcpTransportError, OSError, ValueError, asyncio.TimeoutError):
        return False
    finally:
        await gateway.close()
