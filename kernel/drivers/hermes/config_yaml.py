"""``config.yaml`` 的**最小差异**读-改-写（批次二十四）。

为什么不是 ``yaml.safe_load`` + ``yaml.safe_dump`` 整份重写
------------------------------------------------------------
那正是旧 ``server.py`` ``_write_config`` 的做法，代价是：用户在自己的
``config.yaml`` 里写的**注释、键顺序、空行、引号风格**每写一次就被抹一次。
这份文件是用户的东西，不是 Kaus 的账本——物化只该动它自己管的那几个键。

为什么不是 ruamel
-----------------
本环境里 ``import ruamel.yaml`` 是 ``ModuleNotFoundError``（已实测），而这批不
引入新依赖：物化要能在用户那台机器上原样跑起来，多一个 pip 依赖就多一种
「在我这儿是好的」。所以走**逐行的顶层块替换**——只用标准库 + 已在用的 PyYAML。

做法与它的边界（诚实地说清楚）
------------------------------
``config.yaml`` 的顶层是一个映射，每个顶层键的**块**= 从 ``^<key>:`` 那一行起，
到下一个顶层键之前（结尾的空行与顶层注释行归下一个键，不属于本块）。

- **没被点名的顶层键：逐字节原样保留**，注释、空行、顺序全在；
- **被点名的顶层键**：整块用 ``yaml.safe_dump({key: value})`` 重新生成 →
  该块**内部**的注释与格式会丢。这是本方案的已知代价，写进
  ``docs/ops/projection.md`` 与 AD-149，不假装没有；
- 原来没有的键：追加到文件末尾；
- 删除（``UNSET``）：整块删掉，**紧挨在它前面的注释行保留**——那些注释可能是
  在说整份文件的事，替用户删掉别人的话不是我们该做的决定。

:func:`apply_top_level` 是纯函数（文本进、文本出，不碰磁盘），所以它可以被
单独测：给一份带注释的文件，改一个键，断言别的行一个字节都没动。
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

import yaml

#: 「把这个键删掉」的哨兵。用独立对象而不是 ``None``——``None`` 在 YAML 里是
#: 一个**合法的值**（``key:`` 后面什么都不写），两者必须分得开。
UNSET: Final[object] = object()

#: 顶层键行：零缩进 + 键名 + 冒号。带引号的键名也认（``"a b": …``）。
_TOP_LEVEL_KEY = re.compile(r'^(?P<key>[^\s#][^:]*?)\s*:(?:\s|$)')


def load(text: str) -> dict[str, Any]:
    """解析成普通字典。解析不了、或顶层不是映射 → 空字典（与旧引擎同口径）。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def dump_value(key: str, value: Any) -> str:
    """把一个顶层键渲染成 YAML 文本块（末尾一定有换行）。"""
    text = yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return text if text.endswith("\n") else text + "\n"


def _unquote(raw: str) -> str:
    token = raw.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def top_level_key_of(line: str) -> str | None:
    """这一行是不是某个顶层键的开头；是就返回键名。"""
    if not line or line[0].isspace() or line.lstrip().startswith("#"):
        return None
    match = _TOP_LEVEL_KEY.match(line)
    if match is None:
        return None
    key = _unquote(match.group("key"))
    return key or None


def _blocks(lines: list[str]) -> list[tuple[str | None, int, int]]:
    """把文本切成 ``(键名或 None, 起始行, 结束行独占)`` 的块序列。

    键名为 ``None`` 的块是「不属于任何顶层键」的部分：文件开头的注释、块之间的
    空行、以及归属于下一个键的前置注释。
    """
    starts: list[tuple[int, str]] = [
        (index, key)
        for index, line in enumerate(lines)
        if (key := top_level_key_of(line)) is not None
    ]
    blocks: list[tuple[str | None, int, int]] = []
    cursor = 0
    for position, (index, key) in enumerate(starts):
        # 块的真正起点要把「紧挨在它上面的注释与空行」让出去（它们属于前言）。
        if index > cursor:
            blocks.append((None, cursor, index))
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        # 结尾的空行与顶层注释行归下一个键。
        while end > index + 1 and (
            not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
        ):
            end -= 1
        blocks.append((key, index, end))
        cursor = end
    if cursor < len(lines):
        blocks.append((None, cursor, len(lines)))
    return blocks


def apply_top_level(text: str, changes: Mapping[str, Any]) -> str:
    """按 ``changes`` 改写顶层键，其余内容逐字节保留。

    ``changes`` 的值是 :data:`UNSET` 表示删掉这个键。已存在的键就地替换（保持
    它在文件里的位置），不存在的键追加到末尾（按 ``changes`` 的给出顺序）。
    """
    if not changes:
        return text
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    out: list[str] = []
    for key, start, end in _blocks(lines):
        if key is None or key not in changes:
            out.extend(lines[start:end])
            continue
        seen.add(key)
        value = changes[key]
        if value is UNSET:
            continue  # 整块删掉（前置注释在别的块里，保留）
        out.append(dump_value(key, value))
    rendered = "".join(out)
    appended = [
        dump_value(key, value)
        for key, value in changes.items()
        if key not in seen and value is not UNSET
    ]
    if appended:
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        rendered += "".join(appended)
    return rendered


def get_path(data: Mapping[str, Any], key_path: str) -> Any:
    """按点号路径取值；路径上任何一段不存在 → ``UNSET``（区别于值就是 ``None``）。"""
    node: Any = data
    for segment in key_path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return UNSET
        node = node[segment]
    return node


def set_path(data: Mapping[str, Any], key_path: str, value: Any) -> dict[str, Any]:
    """按点号路径写值，返回**新的**顶层字典（不改入参）。"""
    root = dict(data)
    segments = key_path.split(".")
    node = root
    for segment in segments[:-1]:
        child = node.get(segment)
        child = dict(child) if isinstance(child, Mapping) else {}
        node[segment] = child
        node = child
    node[segments[-1]] = value
    return root


def unset_path(data: Mapping[str, Any], key_path: str) -> dict[str, Any]:
    """按点号路径删键，返回**新的**顶层字典。父级删空后不顺手删父级——
    「这一段变成空映射」与「这一段不存在」在引擎那边可能不是一回事。"""
    root = dict(data)
    segments = key_path.split(".")
    node = root
    for segment in segments[:-1]:
        child = node.get(segment)
        if not isinstance(child, Mapping):
            return root
        child = dict(child)
        node[segment] = child
        node = child
    node.pop(segments[-1], None)
    return root


__all__ = [
    "UNSET",
    "apply_top_level",
    "dump_value",
    "get_path",
    "load",
    "set_path",
    "top_level_key_of",
    "unset_path",
]
