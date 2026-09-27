"""网关失败的「人话 + 修法」表（批次十六第 1 件 / 走查 F5）。

为什么在 Driver 里而不是接入层
------------------------------
修法里必然出现引擎自己的名字与命令（``hermes gateway run``、``API_SERVER_PORT``、
``key_ref`` 指向的 profile）。公共层（``app/`` 与 ``runtime/``）明令不得出现这些
私有词汇（N §3，纯净性扫描机械保障），所以文案只能住在这里。公共层拿到的是
:class:`drivers.base.FailureHint` 这个**形状**，不是这些字。

覆盖的两种根因（真机上占绝大多数）
----------------------------------
=====================  ===================================================
连接被拒 / 连不上       网关根本没在跑，或 base_url 的端口与网关实际监听的
                        ``API_SERVER_PORT`` 不是一个。
401 / 403               网关在跑，但 key 不对——最常见的是 key_ref 指向了
                        另一个 profile 的 ``.env``。
=====================  ===================================================

**只对认得出的根因给修法。** 认不出来就返回 ``None``，让上层照旧报原始（已脱敏
的）错误——编一句「请检查配置」等于没说，还会盖住真正的原因。
"""

from __future__ import annotations

from drivers.base import (
    TURN_ALREADY_RUNNING as base_turn_already_running,
    FailureHint,
    turn_already_running_hint as base_turn_already_running_hint,
)

#: 连不上网关。``code`` 是稳定串，前端可以按它分支（例如给一个「怎么启动网关」的链接）。
GATEWAY_UNREACHABLE = "gateway_unreachable"
#: 网关在，但拒绝了我们的 key。
GATEWAY_KEY_MISMATCH = "gateway_key_mismatch"

_UNREACHABLE_HINT = (
    "Hermes 网关没在运行：在终端执行 `hermes gateway run`，"
    "或检查 base_url 的端口是否与 API_SERVER_PORT 一致"
)
_KEY_MISMATCH_HINT = "网关 key 不匹配：核对 key_ref 指向的 profile"


def gateway_unreachable(base_url: str | None = None) -> FailureHint:
    """``/health`` 都连不上：网关没在跑，或端口不对。"""
    where = f"（{base_url}）" if base_url else ""
    return FailureHint(
        code=GATEWAY_UNREACHABLE,
        message=f"连不上 Hermes 网关{where}",
        hint=_UNREACHABLE_HINT,
    )


def gateway_key_mismatch() -> FailureHint:
    """``/v1/capabilities`` 回 401/403：key 无效或指向了别的 profile。"""
    return FailureHint(
        code=GATEWAY_KEY_MISMATCH,
        message="Hermes 网关拒绝了这把 API key",
        hint=_KEY_MISMATCH_HINT,
    )


#: 上一轮还在跑，这一句没发出去（批次二十七第 2 件）。
#: 批次三十七（R7）起，取值与文案由公共层持有——ACP 也走同一份，见
#: :func:`drivers.base.turn_already_running_hint`。这里保留这个名字是为了本包内
#: 的引用不必全改。
TURN_ALREADY_RUNNING = base_turn_already_running
#: 网关明确拒绝了这次提交，但拒绝的理由我们认不出来。
RUN_SUBMIT_REJECTED = "run_submit_rejected"

#: 网关说「忙」的说法（实测 0.21.0 在同一条会话上并发提交时回 500，body 的
#: ``error.code`` / ``error.message`` 里带这些词之一）。**只认词，不认状态码**：
#: 把所有 500 都当成「忙」会把真正的故障说成「等一等」。
_BUSY_MARKERS: tuple[str, ...] = (
    "already running",
    "already_running",
    "run_in_progress",
    "run in progress",
    "session_busy",
    "session is busy",
    "busy",
    "concurrent run",
    "active run",
)


def turn_already_running(where: str = "站内") -> FailureHint:
    """这条会话上一轮还没结束。``where`` 只用于措辞，不上 wire 之外的地方。

    R7（批次三十七）：文案改由公共层持有，Hermes 与 ACP 共用同一份——同一个状态
    在两台引擎上说两句不同的话，用户会以为自己碰到的是两件事。
    """
    del where
    return base_turn_already_running_hint()


def run_submit_rejected(message: str) -> FailureHint:
    """网关拒绝了 ``POST /v1/runs``，理由认不出来：把**它自己那句话**带上去。

    不带状态码：``HTTP 500`` 对用户不是信息，它只会出现在截图里让人以为是我们
    崩了。网关自己写了什么就说什么，写得不清楚是网关的事，我们不替它编。
    """
    return FailureHint(
        code=RUN_SUBMIT_REJECTED,
        message=f"引擎没有接下这一句：{message}".strip("："),
        hint="稍后重试；一直如此就看一眼网关日志",
    )


def looks_busy(code: str | None, message: str | None) -> bool:
    """网关这次拒绝说的是不是「上一轮还在跑」。认不出来就是 ``False``。"""
    haystack = " ".join(part for part in (code, message) if part).lower()
    return any(marker in haystack for marker in _BUSY_MARKERS)


def classify_status(status: int | None) -> FailureHint | None:
    """按 HTTP 状态码认根因；认不出来返回 ``None``（不编）。"""
    if status in (401, 403):
        return gateway_key_mismatch()
    return None


__all__ = [
    "GATEWAY_KEY_MISMATCH",
    "GATEWAY_UNREACHABLE",
    "RUN_SUBMIT_REJECTED",
    "TURN_ALREADY_RUNNING",
    "classify_status",
    "gateway_key_mismatch",
    "gateway_unreachable",
    "looks_busy",
    "run_submit_rejected",
    "turn_already_running",
]
