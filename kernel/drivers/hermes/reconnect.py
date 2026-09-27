"""SSE 断线重连策略（规格 §3.6）。

事实（文档）
------------
    Unconsumed event buffers expire after five minutes so a detached client cannot
    grow memory indefinitely. This expires transport state only: a run that is
    still executing remains visible to status polling, approval, stop control, and
    concurrency accounting.

且：**没有 ``Last-Event-ID``，没有 ``?after=`` 语义**。所以重连能不能续上，取决于
服务端重连后是「从头重放」还是「只发新事件」——这一条 ``[未验证]``（§8-④）。

状态机
------
``STREAMING → RECONNECTING → POLLING → RECONCILING → DONE``

- ``RECONNECTING``：退避 0.5→1→2→4，**总预算 240s**（严格小于 5 分钟缓冲期，
  否则退避退到一半缓冲就过期了，重连回来只剩 404）；
- 判别服务端行为：比对重连后的第一个事件与本 run 已投递的第一个事件
  （``nativeEventId`` 序号 0 的载荷）是否**逐字节相等**——相等即「从头重放」
  （靠确定性 eventId 幂等去重，无缝续上），不等即「只发新事件」（有事件空洞，
  必须发 warn 并在终态后走 RECONCILING）；
- ``POLLING``：每 1s ``GET /v1/runs/{run_id}`` 读 status，与 §5 的检测器共用同一个
  tick。期间**不产出 message.delta**（无来源，禁止伪造）。

``[未验证]`` 有没有 keepalive 心跳（§8-⑤）：run3 抓的是一个约 3 秒完成的短回合，
没有观察到空闲期。定案前用「120s 无任何字节」判定疑似断线，并且 HTTP client 上
**禁用读超时**（否则空闲期会被误判成断线）。
"""

from __future__ import annotations

from enum import Enum

#: 文档给的事件缓冲过期时间。
EVENT_BUFFER_SECONDS = 300
#: 重连总预算，必须严格小于上一行。
RECONNECT_BUDGET_SECONDS = 240
BACKOFF_SCHEDULE: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
#: §8-⑤ 定案前的断线判据。
IDLE_DISCONNECT_SECONDS = 120.0
#: POLLING 的 tick，与 AD-20 的检测器共用。
POLL_INTERVAL_SECONDS = 1.0


class StreamPhase(str, Enum):
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    POLLING = "polling"
    RECONCILING = "reconciling"
    DONE = "done"


class ReplayBehaviour(str, Enum):
    """§8-④ 的三选一。默认 ``UNKNOWN``——**不能假定**服务端会重放。"""

    UNKNOWN = "unknown"
    #: (a) 从头重放：靠 eventId 幂等去重，无缝续上。
    FULL_REPLAY = "full-replay"
    #: (b) 只发新事件：存在事件空洞，终态后必须 RECONCILING 补账。
    NEW_ONLY = "new-only"
    #: (c) 404/410：缓冲已过期，直接进 POLLING。
    EXPIRED = "expired"


def backoff_for(attempt: int) -> float:
    index = min(max(attempt, 0), len(BACKOFF_SCHEDULE) - 1)
    return BACKOFF_SCHEDULE[index]


def exhausted(elapsed: float) -> bool:
    return elapsed >= RECONNECT_BUDGET_SECONDS


def classify_replay(first_seen: str | None, first_after_reconnect: str | None) -> ReplayBehaviour:
    """规格 §3.6 的判别方法：比对两个「第一个事件」的原始载荷。

    ``first_seen`` 是本 run 已投递的第一个事件的原始 ``data`` 文本；
    ``first_after_reconnect`` 是重连后收到的第一个。逐字节相等即 (a)。
    """
    if first_seen is None or first_after_reconnect is None:
        return ReplayBehaviour.UNKNOWN
    return (
        ReplayBehaviour.FULL_REPLAY
        if first_seen == first_after_reconnect
        else ReplayBehaviour.NEW_ONLY
    )


def needs_reconciliation(behaviour: ReplayBehaviour) -> bool:
    """规格 §3.6：任何非 (a) 的路径都必须在终态后走 RECONCILING 补账。"""
    return behaviour is not ReplayBehaviour.FULL_REPLAY


__all__ = [
    "BACKOFF_SCHEDULE",
    "EVENT_BUFFER_SECONDS",
    "IDLE_DISCONNECT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "RECONNECT_BUDGET_SECONDS",
    "ReplayBehaviour",
    "StreamPhase",
    "backoff_for",
    "classify_replay",
    "exhausted",
    "needs_reconciliation",
]
