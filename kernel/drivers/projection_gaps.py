"""Which effective Project capabilities did not reach a session (D-03: no silent loss).

Shared by the Session Host (projection raised) and drivers (a kind they deliver
outside session options is not in place). Only capability ids leave this
module; configuration values never do.
"""

from __future__ import annotations

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from drivers.base import UnappliedCapability
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
)

INSTRUCTIONS = "instructions"
MCP = "mcp"
SKILLS = "skills"
PROJECT = "project"

NOT_MATERIALIZED = "not_materialized"
PROJECTION_FAILED = "projection_failed"
UNSUPPORTED = "unsupported"
INVALID_CONFIG = "invalid_config"


def _kind_of(entry: EffectiveCapability) -> str | None:
    from drivers.skill_execution.common import is_skill_entry

    if entry.capability_type == INSTRUCTIONS:
        return INSTRUCTIONS
    if entry.capability_type == MCP:
        return MCP
    try:
        if is_skill_entry(entry):
            return SKILLS
    except Exception:  # noqa: BLE001 - a malformed type is not a skill
        return None
    return None


def unapplied_kinds(
    effective: EffectiveCapabilities | None, reason: str = PROJECTION_FAILED
) -> tuple[UnappliedCapability, ...]:
    """Every session-delivered kind in ``effective``, all reported as not applied.

    ``effective is None`` means the set itself could not be computed: the whole
    Project configuration is reported. A native skill mode is not a projection
    request and is not reported.
    """
    if effective is None:
        return (UnappliedCapability(kind=PROJECT, reason=reason),)
    from drivers.skill_execution.common import SELECTION_TYPE, selection_of

    grouped: dict[str, list[str]] = {}
    for entry in effective.entries:
        kind = _kind_of(entry)
        if kind is None:
            continue
        if kind == SKILLS and entry.capability_type == SELECTION_TYPE and selection_of(effective) is None:
            continue
        grouped.setdefault(kind, []).append(entry.capability_id)
    return tuple(
        UnappliedCapability(kind=kind, names=tuple(dict.fromkeys(names)), reason=reason)
        for kind, names in grouped.items()
    )


def instructions_gap(
    effective: EffectiveCapabilities,
    *,
    workspace_root: str | None,
    filename: str | None,
    guard: bool = True,
) -> UnappliedCapability | None:
    """Instructions that only reach the engine through a managed workspace file.

    Delivered means the managed block in ``<workspace_root>/<filename>`` already
    holds the current text (a dry run reports ``unchanged``). Nothing is written.
    """
    from drivers.workspace_projection import materialize_block

    valid = instruction_entries(effective)
    invalid = invalid_instruction_entries(effective)
    if not valid and not invalid:
        return None
    if valid:
        outcome = materialize_block(
            rows=tuple((entry.capability_type, entry.capability_id) for entry in valid),
            body=compose_instructions(effective),
            workspace_root=workspace_root,
            filename=filename,
            marker=INSTRUCTIONS_MARKER,
            project_id=effective.project_id,
            dry_run=True,
            guard=guard,
        )
        delivered = bool(outcome.applied) and not outcome.unsupported and all(
            row.action == "unchanged" for row in outcome.applied
        )
        if not delivered:
            reason = NOT_MATERIALIZED if outcome.applied else (
                next((row.reason for row in outcome.unsupported if row.reason), None) or UNSUPPORTED
            )
            names = (*(entry.capability_id for entry in valid), *(entry.capability_id for entry in invalid))
            return UnappliedCapability(kind=INSTRUCTIONS, names=tuple(dict.fromkeys(names)), reason=reason)
    if invalid:
        return UnappliedCapability(
            kind=INSTRUCTIONS,
            names=tuple(dict.fromkeys(entry.capability_id for entry in invalid)),
            reason=INVALID_CONFIG,
        )
    return None


__all__ = [
    "INSTRUCTIONS",
    "MCP",
    "NOT_MATERIALIZED",
    "PROJECT",
    "PROJECTION_FAILED",
    "SKILLS",
    "instructions_gap",
    "unapplied_kinds",
]
