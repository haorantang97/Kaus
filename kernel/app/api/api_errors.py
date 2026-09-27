"""接口错误的统一形状：``{"error": {"code", "message"}}``。

为什么单独一个模块
------------------
这个形状原先只活在 :mod:`app.api.session_router` 里，于是同一个宿主上出现了
两种错误体：会话端点回 ``{"error": {...}}``，只读领域端点回 web 框架默认的
``{"detail": "..."}``。前端为此写了两条解析分支——那不是前端该背的债，而是
后端没有一套口径。批次八第 5 件把领域端点也搬到这个形状上，两边共用的定义
因此下沉到这里。

两条口径
--------
1. **``code`` 是稳定的机器可读串，``message`` 给人看。** 前端按 code 分支，
   绝不按文案匹配；改文案不算破坏性变更，改 code 才算。
2. **不依赖 app 级 exception handler。** router 要能被任何宿主 include，不能
   要求宿主先装钩子（`server.py` 只允许多一个 attach 调用）。因此错误靠
   :func:`json_error_endpoint` 就地包装每个处理器，而不是靠全局钩子。

本模块只依赖标准库——公共层纯净性扫描会 import ``app`` 下每个子模块，装不装
web 框架都得能过。
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger(__name__)

#: 兜底 500 的稳定 code（批次十七第 3 件）。前端按它显示「服务端出错」。
INTERNAL_ERROR_CODE = "internal_error"


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """统一错误体（v1.0 §12：错误必须是结构化的，不是一行字符串）。"""
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return {"error": payload}


class ApiError(Exception):
    """带稳定 code 的接口错误。序列化成 ``{"error": {...}}``。"""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def to_body(self) -> dict[str, Any]:
        return error_body(self.code, self.message, **self.extra)


def json_error_endpoint(json_response: Any) -> Callable[..., Any]:
    """造一个把 :class:`ApiError` 变成统一 JSON 的处理器包装器。

    ``json_response`` 由调用方传进来（通常是 ``fastapi.responses.JSONResponse``），
    这样本模块自己不必 import 任何 web 框架。``functools.wraps`` 保留签名，
    框架照常解析参数与类型注解。

    **兜底 500（批次十七第 3 件）。** 走查里出现过一次「消息旁 Not sent + 页面
    只有 HTTP 500」：处理器里漏出了一个我们没预料到的异常，宿主的默认处理器把它
    渲染成一段 HTML/``{"detail":…}``，前端两条解析分支都对不上，于是用户看到的
    只有三位数。现在漏出来的异常一律变成同一个形状
    ``{"error":{"code":"internal_error","message":"服务端出错：<异常类名>",
    "requestId"}}``：

    - **只报类型名**，不把异常文本上 wire——异常文本里可能有路径、URL、甚至
      凭据片段（AD-48：明文凭据不进响应）；
    - ``requestId`` 是这次失败的编号，同一个编号在**日志**里对应一条完整堆栈，
      用户把这串编号报给我们就能定位，而不必让他去翻日志；
    - 带 ``status_code`` 属性的异常（框架自己的 ``HTTPException``）原样放行——
      那是宿主已经会处理的东西，替它做主只会把 404 变成 500。
    """

    def decorate(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
        @functools.wraps(handler)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(*args, **kwargs)
            except ApiError as exc:
                return json_response(status_code=exc.status_code, content=exc.to_body())
            except Exception as exc:  # noqa: BLE001 - 兜底：任何漏网异常都要有形状
                if isinstance(getattr(exc, "status_code", None), int):
                    raise
                request_id = uuid.uuid4().hex[:12]
                LOGGER.exception(
                    "接口未捕获异常（requestId=%s，handler=%s）",
                    request_id,
                    getattr(handler, "__name__", "?"),
                )
                return json_response(
                    status_code=500,
                    content=error_body(
                        INTERNAL_ERROR_CODE,
                        f"服务端出错：{type(exc).__name__}",
                        requestId=request_id,
                    ),
                )

        return wrapper

    return decorate


__all__ = [
    "INTERNAL_ERROR_CODE",
    "ApiError",
    "error_body",
    "json_error_endpoint",
]
