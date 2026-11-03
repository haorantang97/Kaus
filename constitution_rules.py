"""Constitution (守则) rule resolution across Project layers.

A Project's effective constitution is its ancestor chain (root → … → self)
with every layer's text stacked; explicit rule blocks are keyed by ``rule_id``
so a child can override or disable an ancestor's rule. This module is the pure
text part: parse one layer into free-text and rule entries, render a rule,
and resolve a list of layers. Which layers exist and what they contain is the
host's business (``server.py`` reads them from the domain database).

Rule block syntax (Markdown heading, see :data:`project_instructions.RULE_HEADING_RE`)::

    ### [rule:family-love] 家庭偏好
    ### [override:family-love] 家庭偏好
    ### [disable:family-love]

Unmarked text is appended as the layer's free text and cannot be overridden
per entry by a descendant.
"""
from __future__ import annotations

from typing import Mapping, Sequence

import project_instructions

RULE_HEADING_RE = project_instructions.RULE_HEADING_RE


def parse_entries(node: str, label: str, text: str, is_self: bool) -> list:
    """把一层文本解析成自由文本块 + 显式规则块。

    规则块语法（Markdown heading）：
      ### [rule:family-love] 家庭偏好
      ### [override:family-love] 家庭偏好
      ### [disable:family-love]

    未标记文本作为该层的自由文本追加，不能被子级按条覆盖。
    """
    entries, loose, current = [], [], None

    def flush_loose() -> None:
        nonlocal loose
        chunk = "\n".join(loose).strip()
        if chunk:
            entries.append({"kind": "text", "node": node, "label": label, "text": chunk, "is_self": is_self})
        loose = []

    def flush_rule() -> None:
        nonlocal current
        if current:
            current["body"] = "\n".join(current.pop("body_lines", [])).strip()
            entries.append(current)
            current = None

    for line in (text or "").splitlines():
        match = RULE_HEADING_RE.match(line)
        if match:
            flush_rule()
            flush_loose()
            op = match.group(2).lower()
            rule_id = match.group(3).lower()
            title = match.group(4).strip()
            current = {
                "kind": "rule",
                "op": op,
                "rule_id": rule_id,
                "title": title,
                "node": node,
                "label": label,
                "is_self": is_self,
                "body_lines": [],
            }
        elif current:
            current["body_lines"].append(line)
        else:
            loose.append(line)
    flush_rule()
    flush_loose()
    return entries


def render_rule(entry: dict) -> str:
    title = (entry.get("title") or "").strip()
    body = (entry.get("body") or "").strip()
    parts = []
    if title:
        parts.append(f"### {title}")
    if body:
        parts.append(body)
    return "\n".join(parts).strip()


def resolve_layers(layers: Sequence[Mapping]) -> dict:
    """沿祖先链按 rule_id 解析守则。

    ``layers`` 是根在前的各层 ``{node, label, text, is_self}``（只含有内容的层）。

    - text(未标记): 追加块，永远参与最终守则。
    - rule / override: 写入同一 rule_id 的当前值；子级同 id 会遮住父级同 id。
    - disable: 屏蔽同一 rule_id 的祖先规则；后代可再次 rule/override 打开。

    判断依据是显式 rule_id/操作痕迹，不做语义猜测，也不比较文本是否相同。
    """
    items: list[dict] = []
    rules: dict[str, dict] = {}
    rule_seen: set[str] = set()
    resolved_rules: list[dict] = []

    for layer in layers:
        for entry in parse_entries(layer["node"], layer["label"], layer["text"], layer["is_self"]):
            if entry["kind"] == "text":
                items.append({"kind": "text", "entry": entry})
                continue

            rule_id = entry["rule_id"]
            if rule_id not in rule_seen:
                rule_seen.add(rule_id)
                items.append({"kind": "rule", "rule_id": rule_id})

            if entry["op"] == "disable":
                rules[rule_id] = {**entry, "disabled": True}
            else:
                rules[rule_id] = {**entry, "disabled": False}

    sections = []
    for item in items:
        if item["kind"] == "text":
            entry = item["entry"]
            sections.append({
                "kind": "text",
                "node": entry["node"],
                "label": entry["label"],
                "text": entry["text"],
                "is_self": entry["is_self"],
            })
            continue

        rule_id = item["rule_id"]
        entry = rules.get(rule_id)
        if not entry:
            continue
        resolved_rules.append({
            "id": rule_id,
            "op": entry.get("op"),
            "node": entry.get("node"),
            "label": entry.get("label"),
            "title": entry.get("title") or "",
            "disabled": bool(entry.get("disabled")),
            "is_self": bool(entry.get("is_self")),
        })
        if entry.get("disabled"):
            continue
        text = render_rule(entry)
        if not text:
            continue
        suffix = " · override" if entry.get("op") == "override" else ""
        sections.append({
            "kind": "rule",
            "rule_id": rule_id,
            "op": entry.get("op"),
            "node": entry["node"],
            "label": f"{entry['label']} · {rule_id}{suffix}",
            "text": text,
            "is_self": entry["is_self"],
        })

    effective = "\n\n".join(f"## ⟦{s['label']}⟧\n{s['text']}" for s in sections if (s.get("text") or "").strip())
    return {"sections": sections, "rules": resolved_rules, "effective": effective}


__all__ = ["RULE_HEADING_RE", "parse_entries", "render_rule", "resolve_layers"]
