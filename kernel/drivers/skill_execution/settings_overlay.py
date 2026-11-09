"""Kaus 自己持有的、只给一次启动用的配置覆盖文件。

有些 Agent 没有按次启动的开关，只认「再读一个设置文件」（系统设置路径环境变量之类）。
这里把合并好的内容写成一个按内容寻址、不可变的文件，放在 Kaus 的状态目录里；用户的
任何配置文件都不动。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from drivers.base import UnsupportedCapabilityError
from .common import state_root


def write_overlay(spec, group: str, content: str, *, suffix: str = ".json") -> Path:
    """把 ``content`` 写进 ``<state>/launch-overlays/<group>/<sha256>.json`` 并返回路径。

    同一内容只写一次；路径或内容被动过（符号链接、内容不符）时拒绝，不覆盖。
    """
    root = state_root(spec) / "launch-overlays" / group
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise UnsupportedCapabilityError("启动配置目录不可用")
    path = root / (hashlib.sha256(content.encode("utf-8")).hexdigest()[:32] + suffix)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise UnsupportedCapabilityError("启动配置文件已变化，请重试")
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    return path


def union(existing, names) -> list[str]:
    """字符串列表去重并追加（保持原有顺序）。"""
    result = [item for item in (existing or ()) if isinstance(item, str)]
    for name in names:
        if name not in result:
            result.append(name)
    return result


__all__ = ["union", "write_overlay"]
