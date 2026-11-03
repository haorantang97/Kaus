"""Project instruction content as ``instructions`` capability rows (host layer).

Two kinds of content live on a Project:

- the persona: one local row ``project-persona`` (``{"body": text}``);
- the rule layer (historically a ``constitution.md`` per node): free text becomes
  one local row ``constitution-<slug>`` and each ``### [rule:<id>]`` /
  ``### [override:<id>]`` heading one local row ``rule-<id>``; a
  ``### [disable:<id>]`` heading becomes a block row ``rule-<id>``. The resolver's
  override/block semantics therefore match the old per-layer resolution, and
  ``rows_to_text`` rebuilds the layer text exactly from the rows plus a small
  layout map (block rows carry no config, so their raw segment and position are
  kept in the Project metadata under ``INSTRUCTION_BLOCKS_KEY``).

Hermes-specific managed-block markers that older files carry are also defined
here so the importer and the host strip them the same way.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

INSTRUCTIONS_TYPE = "instructions"
PERSONA_ID = "project-persona"
INSTRUCTION_BLOCKS_KEY = "instruction_blocks"

#: Managed block the host writes into a native SOUL.md (unchanged historical markers).
CONSTITUTION_BEGIN = "<!-- ⚖ HERMES CONSTITUTION · 由仪表盘统一管理（沿组织树分层继承），勿手改本区块（下方是本 agent 的人格，可自由编辑） -->"
CONSTITUTION_END = "<!-- ⚖ END CONSTITUTION -->"
#: Managed block the Hermes projector writes (``drivers.instructions_projection``).
PROJECTION_MARKER = "kaus:instructions"

RULE_HEADING_RE = re.compile(
    r"^(#{1,6})\s+\[(rule|override|disable):([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\]\s*(.*)$", re.I)


def text_row_id(slug: str) -> str:
    return f"constitution-{slug}"


def rule_row_id(rule_id: str) -> str:
    return f"rule-{rule_id}"


def is_layer_row(slug: str, capability_id: str) -> bool:
    return capability_id == text_row_id(slug) or capability_id.startswith("rule-")


def strip_constitution(text: str) -> str:
    """Remove the managed constitution block (and the blank line after it)."""
    begin = text.find(CONSTITUTION_BEGIN)
    if begin == -1:
        return text
    end = text.find(CONSTITUTION_END, begin)
    if end == -1:
        return text
    rest = text[end + len(CONSTITUTION_END):].lstrip("\n")
    return text[:begin] + rest


def strip_projection_blocks(text: str) -> str:
    """Remove every ``kaus:instructions`` managed block the projector may have written."""
    try:
        from drivers import managed_block
    except ImportError:  # pragma: no cover - kernel not on path
        return text
    blocks = managed_block.find_blocks(text, PROJECTION_MARKER)
    for block in reversed(blocks):
        head = text[:block.start].rstrip("\n")
        tail = text[block.end:].lstrip("\n")
        text = head + ("\n\n" if head and tail else "\n" if head else "") + tail
    return text


def persona_from_file_text(text: str) -> str:
    """Persona body as the user wrote it: managed blocks removed."""
    return strip_projection_blocks(strip_constitution(text or "")).strip("\n")


def text_to_rows(slug: str, text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Layer text → ``(rows, block_layout)``.

    Segments are the leading free text and then one segment per rule heading
    (heading line plus the lines up to the next heading). Lines are kept raw so
    :func:`rows_to_text` is the exact inverse. A leading whitespace-only segment
    is attached to the first rule row (``lead``) rather than dropped.
    """
    lines = (text or "").split("\n")
    segments: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in lines:
        match = RULE_HEADING_RE.match(line)
        if match:
            segments.append((line, []))
        else:
            segments[-1][1].append(line)
    rows: list[dict[str, Any]] = []
    layout: dict[str, Any] = {}
    lead_lines = segments[0][1]
    lead = "\n".join(lead_lines)
    pending_lead: str | None = None
    if lead.strip():
        rows.append({"capability_id": text_row_id(slug), "assignment_mode": "local",
                     "config": {"body": lead, "seq": 0, "kind": "text"}})
    elif lead_lines:
        # Whitespace-only (or empty-line) lead: kept on the first rule so the text rebuilds exactly.
        pending_lead = lead
    for seq, (heading, body_lines) in enumerate(segments[1:], start=1):
        match = RULE_HEADING_RE.match(heading or "")
        assert match is not None
        op, rule_id, title = match.group(2).lower(), match.group(3).lower(), match.group(4).strip()
        raw = "\n".join([heading, *body_lines])
        config: dict[str, Any] = {"seq": seq}
        if pending_lead is not None:
            config["lead"] = pending_lead
            pending_lead = None
        if op == "disable":
            layout[rule_row_id(rule_id)] = {**config, "text": raw}
            rows.append({"capability_id": rule_row_id(rule_id), "assignment_mode": "block", "config": {}})
            continue
        rows.append({"capability_id": rule_row_id(rule_id), "assignment_mode": "local",
                     "config": {**config, "body": raw, "kind": "rule", "op": op, "rule_id": rule_id, "title": title}})
    if pending_lead is not None and not rows:
        # Whitespace only and no rules: nothing to store (an empty layer).
        pass
    return rows, layout


def rows_to_text(slug: str, rows: list[Mapping[str, Any]], layout: Mapping[str, Any] | None = None) -> str:
    """Rows (this node's ``instructions`` rows) + block layout → the layer text."""
    layout = layout or {}
    pieces: list[tuple[float, str | None, str]] = []  # (seq, lead, text)
    for row in rows:
        capability_id = str(row.get("capability_id") or "")
        if not is_layer_row(slug, capability_id):
            continue
        config = row.get("config") or {}
        if row.get("assignment_mode") == "block":
            entry = layout.get(capability_id)
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                lead = entry.get("lead")
                pieces.append((float(entry.get("seq", 1)), lead if isinstance(lead, str) else None, entry["text"]))
            else:
                pieces.append((1e9, None, f"### [disable:{capability_id[len('rule-'):]}]"))
            continue
        body = config.get("body")
        if not isinstance(body, str):
            continue
        seq = config.get("seq", 0 if capability_id == text_row_id(slug) else 1)
        lead = config.get("lead")
        pieces.append((float(seq) if isinstance(seq, (int, float)) else 1.0, lead if isinstance(lead, str) else None, body))
    pieces.sort(key=lambda item: item[0])
    out: list[str] = []
    for _seq, lead, text in pieces:
        if lead is not None:
            out.append(lead)
        out.append(text)
    return "\n".join(out)


__all__ = [
    "CONSTITUTION_BEGIN", "CONSTITUTION_END", "INSTRUCTION_BLOCKS_KEY", "INSTRUCTIONS_TYPE", "PERSONA_ID",
    "PROJECTION_MARKER", "RULE_HEADING_RE", "is_layer_row", "persona_from_file_text", "rows_to_text",
    "rule_row_id", "strip_constitution", "strip_projection_blocks", "text_row_id", "text_to_rows",
]
