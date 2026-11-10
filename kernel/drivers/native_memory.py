"""Agent 自带记忆：有没有、能不能按次关掉、能不能读出来（2026-11-11）。

product-decisions「Agent 自带记忆」：导入名单里多一项「Agent 自带记忆」。勾选 = 把它已有的
记忆复制进根 Project 的项目记忆；不勾 = 不复制。两种都在开对话时关掉自带的那份
（``launch_delivery`` 的 ``disable_native_memory``）。关不掉的 Agent 这一项置灰。

这一层不认识引擎名，只看适配器模块的声明：

``HAS_NATIVE_MEMORY: bool``
    ``True`` = 确有自带记忆；``False`` = 没有。不声明又没有关闭钩子的，不知道有没有，名单里不列。
``disable_native_memory``
    有这个钩子就能按次关掉，也视为确有自带记忆。
``read_native_memory(*, home=None) -> list[dict]``
    只读地取出已有记忆，每条 ``topic`` / ``body`` / ``source``（来源文件）。``home`` 只给测试用，
    替换该 Agent 的主目录。没有这个钩子的只关闭、不复制。
"""
from __future__ import annotations

import re
from typing import Any

MAX_ENTRIES = 500
MAX_TOPIC = 80
MAX_BODY = 4000


def describe(adapter: Any) -> dict | None:
    """``{"closable", "readable"}``；没有或不知道有没有自带记忆时返回 ``None``。"""
    if adapter is None:
        return None
    closable = callable(getattr(adapter, "disable_native_memory", None))
    declared = getattr(adapter, "HAS_NATIVE_MEMORY", None)
    if declared is False or (not closable and declared is not True):
        return None
    return {"closable": closable, "readable": callable(getattr(adapter, "read_native_memory", None))}


def read(adapter: Any, *, home: Any = None) -> list[dict]:
    """已有记忆，按项目记忆的长度上限截好；读不到时为空。"""
    reader = getattr(adapter, "read_native_memory", None)
    if not callable(reader):
        return []
    try:
        rows = reader(home=home) or []
    except (OSError, UnicodeError, ValueError):
        return []
    entries = []
    for row in rows:
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        topic = str(row.get("topic") or "").strip() or body.splitlines()[0]
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY - 1] + "…"
        entries.append({"topic": topic[:MAX_TOPIC], "body": body, "source": str(row.get("source") or "")})
        if len(entries) >= MAX_ENTRIES:
            break
    return entries


def connected_sources(registry: Any, adapter_for: Any) -> dict[str, dict]:
    """已接入、确有自带记忆的 Agent：``{agentKey: {agentLabel, closable, readable, read}}``。"""
    sources: dict[str, dict] = {}
    for driver in registry.list_drivers() if registry is not None else ():
        preset = getattr(driver, "preset", None)
        if preset is None:
            continue
        key = getattr(preset, "skill_scope_key", None) or preset.id
        if key in sources:
            continue
        try:
            adapter = adapter_for(preset.skill_projection_adapter)
        except Exception:  # noqa: BLE001 - an adapter that cannot load offers nothing
            continue
        info = describe(adapter)
        if info is not None:
            sources[key] = {"agentLabel": getattr(preset, "label", None) or key, **info,
                            "read": lambda adapter=adapter: read(adapter)}
    return sources


# --------------------------------------------------------------------------- #
# 适配器共用的 Markdown 读法
# --------------------------------------------------------------------------- #

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``---`` 包着的简单 ``key: value`` 头与正文。"""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[match.end():].strip()


def markdown_sections(text: str) -> list[tuple[str, str]]:
    """按 ``#`` / ``##`` 标题切开：``[(标题, 正文)]``；没有标题时整篇一条（标题为空）。"""
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] = ("", [])
    for line in text.splitlines():
        heading = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line)
        if heading:
            if current[0] or "".join(current[1]).strip():
                sections.append(current)
            current = (heading.group(1), [])
        else:
            current[1].append(line)
    if current[0] or "".join(current[1]).strip():
        sections.append(current)
    return [(title, "\n".join(lines).strip()) for title, lines in sections if "\n".join(lines).strip()]


__all__ = ["MAX_ENTRIES", "connected_sources", "describe", "markdown_sections", "read", "split_frontmatter"]
