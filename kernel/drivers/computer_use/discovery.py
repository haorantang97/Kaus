"""Static discovery of explicitly configured or installed computer-use providers.

No commands, permission requests, package installs, network calls or credential
reads happen here. Agent drivers own their native runtime probes.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Mapping, Sequence

from .policy import CAPABILITIES, Candidate
from . import settings

_SERVER_KEYS = {"command", "args", "url", "type", "transport", "kausComputerUse"}
_STATUS = {"ready", "needs_setup", "unknown", "unavailable", "disabled", "denied"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _package(argument: str, name: str) -> bool:
    return argument == name or argument.startswith(name + "@")


def known_provider(server: Mapping[str, Any]) -> tuple[str, str] | None:
    """Recognize exact documented executables/packages, never connection labels."""
    command = Path(str(server.get("command", ""))).name
    raw_args = server.get("args", ())
    args = tuple(str(value) for value in raw_args) if isinstance(raw_args, (list, tuple)) else ()
    if command == "playwright-mcp":
        return "playwright", "browser"
    runner = command in {"npx", "bunx"} or (
        command in {"npm", "pnpm"} and args[:1] in {("exec",), ("dlx",)}
    )
    if runner:
        if any(_package(arg, "@playwright/mcp") for arg in args):
            return "playwright", "browser"
        if any(_package(arg, "@steipete/peekaboo") for arg in args):
            return "peekaboo", "desktop"
    if command == "peekaboo-mcp" or (command == "peekaboo" and args[:1] == ("mcp",)):
        return "peekaboo", "desktop"
    if command == "cua-driver" and args[:1] == ("mcp",):
        return "cua-driver", "desktop"
    return None


def _resource(capability: str, raw: Mapping[str, Any], server: Mapping[str, Any],
              provider: str | None, context_id: str | None) -> str:
    if capability == "desktop":
        # All providers controlling this Mac share the resource. Per-conversation
        # IDs must never create independent locks for the same pointer/keyboard.
        return "desktop:local"
    configured = raw.get("resource")
    if isinstance(configured, str) and configured.startswith("browser:") and configured != "browser:":
        return configured
    args = server.get("args", ())
    if provider == "playwright" and isinstance(args, (tuple, list)):
        if "--isolated" in args and not any(
            str(arg).split("=", 1)[0] in {"--cdp-endpoint", "--extension", "--endpoint", "--shared-browser-context"}
            for arg in args
        ) and context_id:
            return "browser:context:" + context_id
    # Unknown/shared native profiles must serialize; a name does not prove isolation.
    return "browser:shared"


def discover_project(
    effective: Any, *, readiness: Mapping[str, Mapping[str, Any]] | None = None,
    context_id: str | None = None,
) -> tuple[Candidate, ...]:
    """Extract raw Project MCP declarations before ACP drops host annotations.

    Accepts EffectiveCapabilities or a sequence of server dictionaries. Annotation:
    ``kausComputerUse: {capabilities: ['browser'], enabled: false, resource: ...}``.
    Existing configured MCPs remain eligible without a new probe; ``verified`` is
    false unless a caller supplies fresh readiness evidence.
    """
    servers: list[dict[str, Any]] = []
    if isinstance(effective, (list, tuple)):
        servers = [dict(value) for value in effective if isinstance(value, Mapping)]
    else:
        for entry in getattr(effective, "entries", ()):
            if getattr(entry, "capability_type", None) != "mcp":
                continue
            config = _mapping(getattr(entry, "config", {}))
            value = _mapping(config.get("value", config))
            if _SERVER_KEYS & value.keys():
                servers.append({"name": entry.capability_id, **value})
            else:
                servers.extend({"name": str(name), **raw} for name, raw in value.items()
                               if isinstance(raw, Mapping))
    by_name: dict[str, dict[str, Any]] = {}
    for server in servers:
        by_name[str(server.get("name", ""))] = server
    result = []
    for name, server in by_name.items():
        if not name:
            continue
        annotation = _mapping(server.get("kausComputerUse"))
        known = known_provider(server)
        scopes = annotation.get("capabilities")
        if not isinstance(scopes, (list, tuple)):
            scopes = (["browser", "desktop"] if known and known[0] == "cua-driver"
                      else [known[1]] if known else [])
        probe = _mapping((readiness or {}).get(name))
        status = str(probe.get("status", "ready"))
        if status not in _STATUS:
            status = "unknown"
        requirements = tuple(str(value) for value in probe.get("requirements", ())
                             ) if isinstance(probe.get("requirements", ()), (list, tuple)) else ()
        if server.get("disabled") is True or server.get("enabled") is False or annotation.get("enabled") is False:
            status, requirements = "disabled", ("project-disabled",)
        elif annotation.get("denied") is True:
            status, requirements = "denied", ("project-denied",)
        for capability in dict.fromkeys(scope for scope in scopes if scope in CAPABILITIES):
            result.append(Candidate(
                provider_id=name, capability=capability, source="project",
                status=status, requirements=requirements, exposed=status not in {"disabled", "denied"},
                mcp_server=server,
                resource_id=_resource(capability, annotation, server, known[0] if known else None, context_id),
                verified=probe.get("verified") is True,
            ))
    return tuple(result)


def fallback_allowed(effective: Any) -> bool:
    """A blocked MCP's removed config cannot be safely classified by its name.

    Do not silently replace that explicit choice with an automatically added MCP.
    Existing native/project providers are unaffected.
    """
    return not any(getattr(value, "capability_type", None) in {"mcp", "computer-use"}
                   for value in getattr(effective, "blocked", ()))


def discover_fallbacks(
    *, context_id: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    readiness: Mapping[str, Mapping[str, Any]] | None = None,
    platform: str | None = None,
) -> tuple[Candidate, ...]:
    """Return installed mature providers and explicit unmet prerequisites.

    Playwright executable and flags: microsoft/playwright-mcp package.json/README.
    CuaDriver uses its signed, managed app and a desktop-only MCP projection.
    Executable discovery alone
    never establishes macOS permissions or browser runtime availability.
    """
    platform = platform or sys.platform
    providers = (
        ("playwright", "browser", which("playwright-mcp"), ["--isolated"],
         ("browser-runtime", "mcp-probe")),
        ("cua-driver", "desktop", settings.managed_executable() if platform == "darwin" else None,
         ["mcp"],
         ("screen-recording", "accessibility", "mcp-probe")),
    )
    result = []
    for name, capability, executable, args, requirements in providers:
        if not executable:
            result.append(Candidate(name, capability, "fallback", "unavailable",
                                    ("install-provider",) if platform == "darwin" or capability == "browser" else ("macos-required",)))
            continue
        probe = _mapping((readiness or {}).get(name))
        status = str(probe.get("status", "needs_setup"))
        if status not in _STATUS:
            status = "unknown"
        remaining = probe.get("requirements", requirements)
        if not isinstance(remaining, (list, tuple)):
            remaining = requirements
        if status == "ready" and "requirements" not in probe:
            remaining = ()
        server = {"name": f"kaus-{name}", "command": os.fspath(executable),
                  "args": args, "env": []}
        if name == "cua-driver":
            from .cua_proxy import server_spec
            server = server_spec(executable)
        result.append(Candidate(
            name, capability, "fallback", status,
            tuple(str(value) for value in remaining), mcp_server=server,
            resource_id=("desktop:local" if capability == "desktop" else
                         f"browser:context:{context_id}" if context_id else "browser:shared"),
            verified=probe.get("verified") is True,
        ))
    return tuple(result)
