"""订阅辅助：等一条事件、等本轮的第一个 ``runId``（批次二十七提出来的公共件）。

为什么单独一个模块
------------------
这几个函数原本住在 :mod:`app.api.session_router`。批次二十七让 Group 的投递
（``POST /groups/{id}/broadcast`` 与 ``/spawn`` 的首句）也走「先挂订阅再发、
等到本轮第一个 ``runId``」这条口径——**同一条口径必须是同一份代码**，否则两条
路径迟早会在超时、在「等不到算不算失败」上分叉。而 :mod:`app.api.session_router`
自己 import 了 :mod:`app.api.group_router`（索引行的 ``groupTitle``），反过来
再 import 就成了环，所以把这一小块下沉到两边都能用的地方。

与框架无关，可单测。
"""

from __future__ import annotations

import asyncio
from typing import Any

#: 等本轮 ``runId`` 的预算（秒）。整体一个预算，不是每条事件一个。
DEFAULT_RUN_ID_TIMEOUT: float = 2.0

STREAM_END = object()
"""订阅已关闭。"""

STREAM_IDLE = object()
"""本周期内没有新事件——该发心跳了。"""


async def next_envelope(subscription: Any) -> Any:
    """取下一条事件；订阅结束返回 :data:`STREAM_END`。

    单独包一层是因为 ``StopAsyncIteration`` 不能穿过
    :func:`asyncio.wait_for` 包出来的 Task（会被转成 RuntimeError）。
    """
    try:
        return await subscription.__anext__()
    except StopAsyncIteration:
        return STREAM_END


async def next_or_none(subscription: Any, *, timeout: float) -> Any:
    """带超时地取下一条事件；超时返回 :data:`STREAM_IDLE`。"""
    try:
        return await asyncio.wait_for(next_envelope(subscription), timeout=timeout)
    except asyncio.TimeoutError:
        return STREAM_IDLE


async def first_run_id(subscription: Any, *, timeout: float) -> str | None:
    """等本轮的第一个带 ``runId`` 的事件。

    整体一个预算（不是每条一个），因此最坏情况就是 ``timeout`` 秒。等不到返回
    ``None``——调用方据此把 ``runIdPending`` 置真，而不是编一个 id 出来。
    这里消费掉的事件对 SSE 客户端**没有影响**：客户端是按 sequence 从 Event
    Store 重放的，不共享这条订阅。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        envelope = await next_or_none(subscription, timeout=remaining)
        if envelope is STREAM_END or envelope is STREAM_IDLE:
            return None
        run_id = getattr(envelope, "run_id", None)
        if run_id:
            return run_id


__all__ = [
    "DEFAULT_RUN_ID_TIMEOUT",
    "STREAM_END",
    "STREAM_IDLE",
    "first_run_id",
    "next_envelope",
    "next_or_none",
]
