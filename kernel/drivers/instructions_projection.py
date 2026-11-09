"""有效能力 → 一段**有效指令正文**（纯函数，批次四十四）。

Resolver 已经把继承、override 与 block 都算完了（:class:`EffectiveCapabilities`
里剩下的就是「这个项目此刻实际拥有的那些指令」）。这里只做最后一步：**排序并拼接**。

排序（同输入必须同输出）
------------------------
按 ``(order, 来源深度 根→叶, capability_id)``：

1. ``order``：能力自己写的整数，小的在前（默认 100）；
2. **来源深度**：同一个 ``order`` 时，根项目定的规矩排在子项目前面——先读通则、
   再读细则，是人读一份规矩时的自然顺序。深度取
   ``len(contributing_project_ids)``（Resolver 按根→叶记的贡献链）；
3. ``capability_id``：仍然打平时的最后一道，纯粹为了**确定**——两次拼接出两种
   顺序，引擎那边就会无缘无故地换一次行为，而界面上什么都看不出来。

正文形状
--------
每条一节：``## <标题>`` + 空行 + body；节与节之间空一行。标题取 config 里的
``title``（项目配置编辑器给每节起的名字，2026-11-09 起），没写或只有空白时回落
到 ``capability_id``——id 是**用户在项目页上看得见的那个名字**，出了问题他能
照着找回来。旧守则行（``rule-*``）的正文以 ``### [rule:<id>] 标题`` 一行开头，
标题已经在节标题里了，拼接时把这一行去掉，不让同一个标题出现两遍。
"""

from __future__ import annotations

import re
from typing import Final

from app.capabilities.instructions import (
    DEFAULT_ORDER,
    INSTRUCTIONS_CAPABILITY_TYPE,
    spec_of,
)
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability

#: 受管块的 marker（:mod:`drivers.managed_block` 的入参）。它出现在用户文件里，
#: 所以一经发布就不能改——改了等于把旧块变成「别人写的东西」，再也认不出来。
INSTRUCTIONS_MARKER: Final[str] = "kaus:instructions"

#: 节与节之间的分隔。
SECTION_SEPARATOR: Final[str] = "\n\n"


def _sort_key(entry: EffectiveCapability) -> tuple[int, int, str]:
    spec = spec_of(entry.config)
    order = spec.order if spec is not None else DEFAULT_ORDER
    return (order, len(entry.contributing_project_ids), entry.capability_id)


def instruction_entries(
    effective: EffectiveCapabilities,
) -> tuple[EffectiveCapability, ...]:
    """有效能力里的 ``instructions`` 条目，**已按最终顺序排好**。

    正文形状不对的条目（``body`` 缺失/空白）在这里就被滤掉——它们在拼接结果里
    只会是一个空小节。调用方要报「这一条没落地」的话，拿它与
    ``effective.by_type(...)`` 求差即可，不必再解析一遍 config。
    """
    entries = [
        entry
        for entry in effective.by_type(INSTRUCTIONS_CAPABILITY_TYPE)
        if spec_of(entry.config) is not None
    ]
    return tuple(sorted(entries, key=_sort_key))


def invalid_instruction_entries(
    effective: EffectiveCapabilities,
) -> tuple[EffectiveCapability, ...]:
    """正文形状不对、因而进不了拼接的那些 ``instructions`` 条目。

    它们**必须**被调用方报进 ``unsupported``（D-03：不静默丢失）。最常见的情形是
    这条能力的 config 里压根没有 ``body``——在项目页上看着是加过了的，拼接结果里
    却什么都没有，不说一声的话没人能发现。
    """
    return tuple(
        entry
        for entry in effective.by_type(INSTRUCTIONS_CAPABILITY_TYPE)
        if spec_of(entry.config) is None
    )


#: 上面那种条目在投射报告里的一句话。
INVALID_DETAIL: Final[str] = (
    "这条指令没有正文（config 里缺 body，或 body 只有空白），没有可写的内容"
)


#: 旧守则层的小节标题行：``### [rule:<id>] 标题`` / ``### [override:<id>] 标题``。
#: 与宿主 ``project_instructions.RULE_HEADING_RE`` 认同一种写法（disable 行不会
#: 进有效集合，这里不用认）。
LEGACY_RULE_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^#{1,6}\s+\[(?:rule|override):[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\]\s*.*$", re.I
)


def section_title(entry: EffectiveCapability) -> str:
    """这一节的小标题：config 的 ``title``（非空白）优先，否则 ``capability_id``。

    同时认 ``{"value": {...}}`` 包装与裸键值，与 :func:`spec_of` 同一口径。
    """
    config = entry.config
    inner = config.get("value") if isinstance(config.get("value"), dict) else config
    title = inner.get("title") if isinstance(inner, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return entry.capability_id


def section_body(body: str) -> str:
    """节正文：去掉首尾空白；首行若是旧守则标题行就整行去掉（标题已在节标题里）。"""
    text = body.strip()
    first, newline, rest = text.partition("\n")
    if LEGACY_RULE_HEADING_RE.match(first):
        return rest.strip() if newline else ""
    return text


def compose_instructions(effective: EffectiveCapabilities) -> str:
    """有效能力 → 拼好的指令正文。没有任何指令时返回空串。"""
    sections: list[str] = []
    for entry in instruction_entries(effective):
        spec = spec_of(entry.config)
        assert spec is not None  # instruction_entries 已经滤过
        body = section_body(spec.body)
        sections.append(f"## {section_title(entry)}\n\n{body}" if body else f"## {section_title(entry)}")
    return SECTION_SEPARATOR.join(sections)


__all__ = [
    "INSTRUCTIONS_MARKER",
    "INVALID_DETAIL",
    "LEGACY_RULE_HEADING_RE",
    "SECTION_SEPARATOR",
    "compose_instructions",
    "instruction_entries",
    "invalid_instruction_entries",
    "section_body",
    "section_title",
]
