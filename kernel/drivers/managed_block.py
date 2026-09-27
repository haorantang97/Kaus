"""**受管块**：在别人的文件里占一小段，块外一个字都不动（批次四十四 / AD-165）。

为什么是块而不是整份文件
------------------------
项目指令要落到引擎在工作目录里读的那份文件上（``CLAUDE.md`` 之类）。那份文件**是
用户的**：里面大概率已经有他自己写的规矩。整份覆盖等于把用户的东西删了换成我们
的；「先读出来再合并」等于要理解他写了什么。两条路都不成立，所以只剩第三条：

    在文件里占一段有清楚边界的区域，里面的内容由 Kaus 负责，**边界之外一个字节
    都不碰**。

::

    <!-- kaus:instructions:begin project=project:media -->
    …拼好的正文…
    <!-- kaus:instructions:end -->

五种结果（:data:`BlockAction`）
------------------------------
==================  =======================================================
``create``          文件不存在（或是空白）→ 新建，内容只有这个块。
``append``          文件有内容但没有块 → **追加到末尾**，前面留一个空行。
``replace``         已有块 → 只换块内正文。
``unchanged``       已有块且正文就是要写的那份 → 一个字节都不用动。
``refuse_shared``   已有块，但块头上的 ``project=`` 是**别的项目**
                    → 拒绝写，原样返回。
==================  =======================================================

``refuse_shared`` 为什么是拒绝而不是覆盖
----------------------------------------
两个项目共用同一个工作目录时，后写的会把先写的整段抹掉，而界面上两边都显示
「已应用」。谁该赢这件事我们不知道，也不该替用户猜（AD-166）——所以这里停下来，
把「这个目录已经属于另一个项目」这句话交给界面去说。

纯函数
------
本模块不碰磁盘、不认识任何引擎、也不知道「指令」是什么——``marker`` 是调用方给的
字符串。它只做「文本进、文本出」，因此可以被逐个边角情形单测（CRLF、末尾没有
换行、块出现两次、块头没有 ``project=``）。
"""

from __future__ import annotations

import re
from typing import Final, Literal, NamedTuple

BlockAction = Literal["create", "append", "replace", "unchanged", "refuse_shared"]
"""一次 :func:`upsert_block` 对文本做了什么。"""

#: 块头/块尾的样子。``marker`` 由调用方给（如 ``kaus:instructions``）。
BEGIN_TEMPLATE: Final[str] = "<!-- {marker}:begin project={project_id} -->"
END_TEMPLATE: Final[str] = "<!-- {marker}:end -->"


def begin_line(marker: str, project_id: str) -> str:
    return BEGIN_TEMPLATE.format(marker=marker, project_id=project_id)


def end_line(marker: str) -> str:
    return END_TEMPLATE.format(marker=marker)


def _pattern(marker: str) -> re.Pattern[str]:
    """匹配一整个块。

    ``project=`` 那一段写成可选的：块头被人手工改坏（删掉了 project）时，我们仍要
    **认出**这是一个受管块——认不出就会在它下面再追加一个，文件里于是有两个块。
    认出来之后按 ``project_id`` 为空处理，落到 ``refuse_shared`` 那一档。
    """
    quoted = re.escape(marker)
    return re.compile(
        rf"[ \t]*<!--[ \t]*{quoted}:begin(?:[ \t]+project=(?P<project>[^\s>]*))?[ \t]*-->"
        rf"(?P<body>.*?)"
        rf"[ \t]*<!--[ \t]*{quoted}:end[ \t]*-->",
        re.DOTALL,
    )


class Block(NamedTuple):
    """文件里的一个受管块。"""

    project_id: str
    body: str
    start: int
    end: int


class Upsert(NamedTuple):
    """:func:`upsert_block` 的结果。

    比任务书写的二元组多一个 ``warnings``：「同一个文件里有两个块」这件事必须有
    出口，不然它会被静默吞掉（D-03）。``text`` / ``action`` 仍是前两位，
    解包顺序与文档一致。
    """

    text: str
    action: BlockAction
    warnings: tuple[str, ...] = ()


def _newline_of(text: str) -> str:
    """跟随文件自己的换行风格。在 CRLF 文件里写 LF 会让整段在编辑器里显示成一行。"""
    return "\r\n" if "\r\n" in text else "\n"


def normalize_body(body: str) -> str:
    """块内正文的规范形态：去掉首尾空行，行尾统一成 ``\\n`` 供比较。

    对外暴露是因为**比较**这件事必须两边同一把尺：调用方拿「算出来的正文」与
    「文件里读回来的正文」比对（对账的 in_sync / drifted 就是这一比），各自
    规范化一次就会出现「看起来一样却判成漂移」。
    """
    return body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


#: 旧名，模块内部仍在用。
_normalize_body = normalize_body


def find_blocks(text: str, marker: str) -> tuple[Block, ...]:
    """文本里全部受管块，按出现顺序。"""
    return tuple(
        Block(
            project_id=(match.group("project") or ""),
            body=_normalize_body(match.group("body")),
            start=match.start(),
            end=match.end(),
        )
        for match in _pattern(marker).finditer(text)
    )


def read_block(text: str, marker: str) -> tuple[str, str] | None:
    """第一个受管块的 ``(project_id, 正文)``；没有块就是 ``None``。"""
    blocks = find_blocks(text, marker)
    if not blocks:
        return None
    return blocks[0].project_id, blocks[0].body


def render_block(marker: str, body: str, project_id: str, *, newline: str = "\n") -> str:
    """拼出一个完整的块（不含它前后的空行）。"""
    lines = [begin_line(marker, project_id)]
    normalized = _normalize_body(body)
    if normalized:
        lines.extend(normalized.split("\n"))
    lines.append(end_line(marker))
    return newline.join(lines)


def upsert_block(text: str, marker: str, body: str, project_id: str) -> Upsert:
    """把 ``body`` 写进 ``text`` 的受管块里，**块外一个字节都不动**。

    ``text`` 是文件当前内容（文件不存在时传空串）。返回新文本与这次做了什么。
    """
    warnings: list[str] = []
    blocks = find_blocks(text, marker)
    if len(blocks) > 1:
        warnings.append(
            f"这个文件里有 {len(blocks)} 个 {marker} 受管块，只认第一个；"
            "多出来的那些请手工删掉（我们不替你删别人写的内容）"
        )
    newline = _newline_of(text)
    wanted = _normalize_body(body)

    if not blocks:
        block = render_block(marker, wanted, project_id, newline=newline)
        if not text.strip():
            # 空文件（或只有空白）：内容就是这个块，不留多余空行。
            return Upsert(block + newline, "create", tuple(warnings))
        prefix = text if text.endswith(("\n", "\r")) else text + newline
        return Upsert(prefix + newline + block + newline, "append", tuple(warnings))

    first = blocks[0]
    if first.project_id != project_id:
        return Upsert(text, "refuse_shared", tuple(warnings))
    if first.body == wanted:
        return Upsert(text, "unchanged", tuple(warnings))
    block = render_block(marker, wanted, project_id, newline=newline)
    return Upsert(
        text[: first.start] + block + text[first.end :], "replace", tuple(warnings)
    )


__all__ = [
    "BEGIN_TEMPLATE",
    "END_TEMPLATE",
    "Block",
    "BlockAction",
    "Upsert",
    "begin_line",
    "end_line",
    "find_blocks",
    "normalize_body",
    "read_block",
    "render_block",
    "upsert_block",
]
