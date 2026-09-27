"""Hermes Driver 的测试夹具（假 API server + 契约夹具）。

**只被测试与冒烟脚本导入**，不属于 Driver 的运行路径。放在 Driver 目录下而不是
``drivers/mock/`` 里，是因为它模仿的是一家具体后端的报文形状——那正是
`drivers/hermes/` 这个目录存在的理由。
"""

from __future__ import annotations

__all__: list[str] = []
