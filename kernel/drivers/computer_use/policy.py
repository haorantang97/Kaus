"""Computer-use provider selection, independent of any agent or model loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

Capability = Literal["browser", "desktop"]
Source = Literal["native", "project", "fallback"]
Status = Literal["ready", "needs_setup", "unknown", "unavailable", "disabled", "denied"]
CAPABILITIES = ("browser", "desktop")
_BLOCKED = {"disabled", "denied"}
_PRIORITY = {"native": 0, "project": 1, "fallback": 2}


@dataclass(frozen=True)
class Candidate:
    provider_id: str
    capability: Capability
    source: Source
    status: Status = "ready"
    requirements: tuple[str, ...] = ()
    # Already present in the agent's tool namespace, even if its readiness is unknown.
    exposed: bool = False
    # Private projection payload. Never included in public summaries or repr.
    mcp_server: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    resource_id: str | None = None
    verified: bool = False
    system_permissions: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES or self.source not in _PRIORITY:
            raise ValueError("Invalid computer-use capability or source")
        if self.status not in {"ready", "needs_setup", "unknown", "unavailable", *_BLOCKED}:
            raise ValueError("Invalid computer-use provider status")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source": self.source, "status": self.status, "provider": self.provider_id,
            "requirements": list(self.requirements), "exposed": self.exposed,
            "verified": self.verified,
        }
        if self.resource_id:
            result["resource"] = self.resource_id
        if self.system_permissions:
            result["systemPermissions"] = dict(self.system_permissions)
        return result


@dataclass(frozen=True)
class Selection:
    capability: Capability
    candidate: Candidate | None
    status: Status
    requirements: tuple[str, ...] = ()
    considered: tuple[Candidate, ...] = field(default=(), repr=False)

    @property
    def selected(self) -> Candidate | None:
        return self.candidate if self.status == "ready" else None

    def to_dict(self) -> dict[str, Any]:
        result = self.candidate.to_dict() if self.candidate else {}
        return {**result, "status": self.status, "requirements": list(self.requirements)}


def select_provider(
    capability: Capability,
    candidates: Sequence[Candidate],
    *,
    enabled: bool = True,
    execution_policy: str | None = None,
    allow_fallback: bool = True,
) -> Selection:
    """Choose one provider without overriding a refusal or duplicating exposed tools.

    ``ready`` means eligible for the current connection; ``verified`` separately
    records whether a probe confirmed readiness. This function executes nothing.
    A configured/exposed provider needing setup is reported rather than silently
    adding another tool implementation to the same model.
    """
    if capability not in CAPABILITIES:
        raise ValueError("Unknown computer-use capability")
    matching = tuple(c for c in candidates if c.capability == capability)
    ordered = tuple(sorted(matching, key=lambda c: _PRIORITY[c.source]))
    if execution_policy == "context-only":
        return Selection(capability, None, "disabled", ("context-only",), ordered)
    if not enabled:
        return Selection(capability, None, "disabled", ("project-disabled",), ordered)
    # A refused system permission or explicitly disabled capability is not a
    # missing implementation. Another provider must not bypass that decision.
    refused = next((c for c in ordered if c.status in _BLOCKED), None)
    if refused:
        return Selection(capability, refused, refused.status, refused.requirements, ordered)
    usable = tuple(c for c in ordered if allow_fallback or c.source != "fallback")
    for source in ("native", "project", "fallback"):
        tier = tuple(c for c in usable if c.source == source)
        ready = next((c for c in tier if c.status == "ready"), None)
        if ready:
            return Selection(capability, ready, "ready", ready.requirements, ordered)
        present = next((c for c in tier if c.exposed), None)
        if present:
            return Selection(capability, present, present.status, present.requirements, ordered)
    pending = next((c for c in usable if c.status in {"needs_setup", "unknown"}), None)
    if pending:
        return Selection(capability, pending, pending.status, pending.requirements, ordered)
    requirements = ("project-mcp-blocked",) if not allow_fallback else ()
    return Selection(capability, None, "unavailable", requirements, ordered)


def from_native(probe: Mapping[str, Any] | None) -> tuple[Candidate, ...]:
    """Adapt driver probe metadata; never infer abilities from an engine name."""
    result = []
    for capability in CAPABILITIES:
        raw = (probe or {}).get(capability)
        if not isinstance(raw, Mapping):
            continue
        status = raw.get("status", "unknown")
        if not isinstance(status, str) or status not in {"ready", "needs_setup", "unknown", "unavailable", *_BLOCKED}:
            status = "unknown"
        requirements = raw.get("requirements", ())
        if not isinstance(requirements, (list, tuple)):
            requirements = ()
        result.append(Candidate(
            provider_id=str(raw.get("provider", "native")),
            capability=capability, source="native", status=status,
            requirements=tuple(str(value) for value in requirements),
            exposed=raw.get("exposed") is True or raw.get("enabled") is True,
            resource_id=str(raw["resource"]) if raw.get("resource") else (
                "desktop:local" if capability == "desktop" else "browser:shared"
            ),
            verified=raw.get("verified") is True,
        ))
    return tuple(result)


def project_controls(effective: Any) -> dict[str, bool]:
    """Read existing Project policies; unknown values never grant extra access."""
    result = {capability: True for capability in CAPABILITIES}
    for blocked in getattr(effective, "blocked", ()):
        if (getattr(blocked, "capability_type", None) == "policies" and
                getattr(blocked, "capability_id", None) == "computer_use"):
            return {capability: False for capability in CAPABILITIES}
    for entry in getattr(effective, "entries", ()):
        if (getattr(entry, "capability_type", None) != "policies" or
                getattr(entry, "capability_id", None) != "computer_use"):
            continue
        config = getattr(entry, "config", {})
        if not isinstance(config, Mapping):
            continue
        value = config.get("value", config)
        if not isinstance(value, Mapping):
            continue
        for capability in CAPABILITIES:
            if capability in value:
                result[capability] = value[capability] == "auto"
    return result


def effective_controls(effective):
    """The user's opt-in is a ceiling over individual Project settings."""
    from . import settings
    enabled = settings.is_enabled()
    return {cap: enabled and allowed for cap, allowed in project_controls(effective).items()}
