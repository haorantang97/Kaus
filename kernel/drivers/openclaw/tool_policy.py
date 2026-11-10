"""Reuse an OpenClaw source Agent's tool policy for the Kaus-owned Agent.

Only a Gateway's redacted configuration snapshot is inspected. No native
credentials, endpoints, browser profiles, or execution settings are projected
into a Kaus Agent.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from drivers.base import UnsupportedCapabilityError


_NAME = re.compile(r"^[A-Za-z0-9_.*?:/-]{1,200}$")
_PROFILES = {"minimal", "coding", "messaging", "full"}


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
