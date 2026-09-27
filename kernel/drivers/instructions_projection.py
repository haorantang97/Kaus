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
每条一节：``## <capability_id>`` + 空行 + body；节与节之间空一行。小标题用
``capability_id`` 而不是某个「显示名」，因为 id 是**用户在项目页上看得见的那个
名字**，出了问题他能照着找回来。
"""

from __future__ import annotations

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


def compose_instructions(effective: EffectiveCapabilities) -> str:
    """有效能力 → 拼好的指令正文。没有任何指令时返回空串。"""
    sections: list[str] = []
    for entry in instruction_entries(effective):
        spec = spec_of(entry.config)
        assert spec is not None  # instruction_entries 已经滤过
        sections.append(f"## {entry.capability_id}\n\n{spec.body.strip()}")
    return SECTION_SEPARATOR.join(sections)


__all__ = [
    "INSTRUCTIONS_MARKER",
    "INVALID_DETAIL",
    "SECTION_SEPARATOR",
    "compose_instructions",
    "instruction_entries",
    "invalid_instruction_entries",
]
