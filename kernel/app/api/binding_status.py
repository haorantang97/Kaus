"""一条 Binding 的「现在怎么样」：登录状态 + 原生会话数 + 探测态（批次十九第 3 件）。

为什么单独一个模块
------------------
同一份事实有两个读者：``GET /api/bindings/{id}/status``（引擎卡与会话页页头都读
它，AD-82 / AD-93）与 ``GET /api/projects/{id}/bindings`` 的每一行。两个读者各写
一遍取数逻辑，早晚会出现「卡片说已登录、页头说离线」这种自相矛盾的界面——所以
取数只有 :func:`read_binding_status` 这一处，两个端点都调它。

四条口径
--------
1. **问不出来就说问不出来。** 取不到 Driver → ``auth`` 是 ``None``、
   ``nativeSessionCount`` 是 ``None``；不编 ``signed_out``，也不编 ``0``。
   ``0`` 会被界面渲染成「0 条会话」，那是一句谎话（N §13.1）。
2. **不上凭据。** ``auth`` 只可能带 :class:`~drivers.base.AuthState` 的那几个字段，
   而那个类型的判定路径本身就不读凭据的值；``account`` 只接受已脱敏形态。
3. **不触发写。** 这里全是读：探测态直接读 Backend 行（也就是
   ``GET /api/backends/{id}`` 读的同一份），不为了回答这个问题去 force 一次探测。
4. **一条 Binding 出错不拖垮整页。** 列表侧逐条独立求值，某条抛异常就退化成
   「这条没有 auth / 计数」，其余照常返回。

延迟绑定的取数面（:func:`set_status_provider`）
----------------------------------------------
只读领域路由（:mod:`app.api.router`）装配时手上**没有** Driver Registry——它比会话
运行时先起来，两者由宿主分别装配。为了让 ``/projects/{id}/bindings`` 也能带上这
两个字段，这里留一个进程级的取数面：会话运行时装配好之后把它登记进来，只读路由
在**每次请求时**去问一次。没登记 = 每行照旧不带这两个字段（前端按缺字段静默不
渲染，AD-71），而不是报错。

它是进程级可变状态，因此纪律写死两条：只有装配代码能调 :func:`set_status_provider`，
且必须能被 :func:`clear_status_provider` 干净地摘掉（测试靠这一条相互隔离）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

#: 取数面：给一条 Binding，返回它的 ``{auth, nativeSessionCount}``（或 ``None``）。
StatusProvider = Callable[[Any], "Awaitable[Mapping[str, Any] | None]"]

_PROVIDER: StatusProvider | None = None


def set_status_provider(provider: StatusProvider) -> None:
    """登记取数面（装配期调用一次）。后登记的覆盖先登记的。"""
    global _PROVIDER
    _PROVIDER = provider


def clear_status_provider() -> None:
    """摘掉取数面。测试用；生产上只有进程退出会走到这一步。"""
    global _PROVIDER
    _PROVIDER = None


def status_provider() -> StatusProvider | None:
    return _PROVIDER


def auth_state_to_wire(auth: Any) -> dict[str, Any] | None:
    """:class:`~drivers.base.AuthState` → wire dict。

    ``provider`` / ``account`` / ``hint`` 为空时**不出现这个键**（不是 null）：
    和能力表的 ``note``、``FailureHint.to_detail`` 的 ``hint`` 一个口径——前端按
    「有没有这个键」分支，就不会渲染出一个空的账号栏。
    """
    if auth is None:
        return None
    payload: dict[str, Any] = {"state": auth.state, "model": auth.model}
    if auth.checked_at is not None:
        payload["checkedAt"] = auth.checked_at.isoformat()
    for key, value in (
        ("provider", auth.provider),
        ("account", auth.account),
        ("hint", auth.hint),
    ):
        if value:
            payload[key] = value
    return payload


async def read_binding_status(
    binding: Any,
    *,
    registry: Any,
    backend: Any = None,
) -> dict[str, Any]:
    """一条 Binding 的完整状态行。

    ``backend`` 是领域库里的 Backend 行（给探测态用）；不给就不带
    ``probeState`` / ``probeMessage``——那两项的真源是那一行，这里不去猜。
    """
    auth: Any = None
    count: int | None = None
    driver = None
    if registry is not None:
        try:
            driver = registry.get(binding.backend_id)
        except Exception:  # noqa: BLE001 - 没注册这个 backend ≠ 这次请求失败
            driver = None
    if driver is not None:
        auth = await _safe(driver, "read_auth_state", binding)
        count = await _safe(driver, "count_native_sessions", binding)
        if not isinstance(count, int):
            count = None

    payload: dict[str, Any] = {
        "bindingId": binding.id,
        "auth": auth_state_to_wire(auth),
        "nativeSessionCount": count,
    }
    if backend is not None:
        payload["probeState"] = backend.probe_state
        payload["probeMessage"] = backend.probe_message
    return payload


async def _safe(driver: Any, method: str, binding: Any) -> Any:
    """调一个 Driver 方法；它没有这个方法、或它抛了，都算「这次问不出来」。

    吞掉异常在这里是**对的**：调用方要的是一张状态卡，而「登录态读不出来」正是
    这张卡要表达的内容之一（``unknown`` / ``None``）。让它冒泡出去只会把一整页
    引擎卡换成一个 500。
    """
    handler = getattr(driver, method, None)
    if handler is None:
        return None
    try:
        return await handler(binding)
    except Exception:  # noqa: BLE001 - 见 docstring
        return None


__all__ = [
    "StatusProvider",
    "auth_state_to_wire",
    "clear_status_provider",
    "read_binding_status",
    "set_status_provider",
    "status_provider",
]
