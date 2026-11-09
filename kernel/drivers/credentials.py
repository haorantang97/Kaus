"""用户在设置里填的 Agent API key：驱动启动进程时按预设声明的变量名注入（只在这一次进程里）。

值由宿主的加密存储提供（``app/agent_credentials.py``），这里只放一个取值函数的挂钩，
驱动不读文件、不落盘、不记日志。没有挂钩或没填 key 时什么也不做，Agent 照旧用自己的登录。
"""
from __future__ import annotations

from typing import Callable

_provider: Callable[[str], str | None] | None = None


def set_api_key_provider(provider: Callable[[str], str | None] | None) -> None:
    global _provider
    _provider = provider


def api_key_for(backend_id: str) -> str | None:
    if _provider is None:
        return None
    try:
        value = _provider(backend_id)
    except Exception:  # noqa: BLE001 - 取不到就按没填处理，不挡启动
        return None
    return value if isinstance(value, str) and value.strip() else None


__all__ = ["api_key_for", "set_api_key_provider"]
