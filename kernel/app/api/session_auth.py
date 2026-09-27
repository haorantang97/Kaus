"""会话类端点的本地鉴权（D-17 / AD-66）。

威胁模型
--------
会话 API 能建会话、发消息、答审批——也就是能**驱动一个有工具权限的 agent**。
本机上跑着的任何进程、以及被诱导访问恶意页面的浏览器，都不该能做这些事。所以两道闸
一起上，缺一不可：

=====================  ==========================================================
浏览器跨站请求          ``Origin`` 白名单（:mod:`app.api.origin_policy`）。恶意页面
                       改不了这个头，因此挡得住 CSRF/CSWSH 类劫持。
本机其它进程            本地 token。同机进程不带 ``Origin``，Origin 那关拦不住它们，
                       只有「读得到 token 文件」才算有权限——token 文件 0600，
                       等价于「和你同一个用户」。
=====================  ==========================================================

两关的顺序是**先 Origin 后 token**：跨站请求即使猜对了 token 也是 403，不是 401。
这样错误码本身不泄露「token 对不对」。

token 怎么到前端
----------------
``GET /api/session-auth/bootstrap`` 只对**同源浏览器请求**或**本机非浏览器请求**
返回 token；跨站 Origin 一律 403。拿到之后所有会话请求带
``Authorization: Bearer <token>``。

``EventSource`` 不能带自定义头，所以 SSE 端点——**只有 SSE 端点**——额外接受
``?token=``。作为交换，这个取值绝不允许出现在任何日志或异常文本里：本模块的错误
消息全是常量串，从不回显收到的 token（宿主访问日志的处置见接入层）。

本模块的纯度
------------
只依赖标准库：口径判断是纯函数，web 框架的胶水（``Depends`` / 自定义 ``APIRoute``）
关在函数体内按需 import，和 :mod:`app.api.session_router` 同一套做法。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.api.origin_policy import (
    DEFAULT_LOCAL_HOSTS,
    DEFAULT_LOCAL_PORTS,
    is_same_site_fetch,
    origin_allowed,
)

#: 稳定的机器可读错误码（前端按 code 分支，不按文案）。
UNAUTHORIZED_CODE = "unauthorized"
FORBIDDEN_ORIGIN_CODE = "forbidden_origin"

#: SSE 的唯一例外：``EventSource`` 带不了头，只能把 token 放查询串。
SSE_TOKEN_QUERY_PARAM = "token"

#: 换取 token 的端点（相对 router 前缀）。
BOOTSTRAP_ROUTE_PATH = "/session-auth/bootstrap"

#: 错误文案是**常量**：任何时候都不把收到的 token 拼进字符串（D-17：不进日志）。
_MISSING_TOKEN_MESSAGE = (
    "会话接口需要本地 token：请先 GET /api/session-auth/bootstrap 取回，"
    "再以 Authorization: Bearer <token> 发送（SSE 可用 ?token=）"
)
_BAD_TOKEN_MESSAGE = "本地 token 不正确"
_BAD_ORIGIN_MESSAGE = "该 Origin 不在本机白名单内，请求被拒绝"
_NO_TOKEN_CONFIGURED_MESSAGE = "服务端没有可用的本地 token，会话接口暂不可用"


class SessionAuthError(Exception):
    """鉴权失败。由自定义路由类序列化成 ``{"error": {"code", "message"}}``。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class RequestFacts:
    """一次请求里与鉴权有关的全部输入。与 web 框架无关，可直接单测。"""

    origin: str | None = None
    host: str | None = None
    authorization: str | None = None
    query_token: str | None = None
    sec_fetch_site: str | None = None


def bearer_token(authorization: str | None) -> str | None:
    """从 ``Authorization`` 里取出 Bearer 值。形状不对返回 ``None``。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class SessionAuthPolicy:
    """一份 token + 一张 Origin 白名单，构成会话接口的准入判断。

    token 从 ``token_provider`` 现取现用（每次请求调一次），而不是构造时抓一份
    快照：token 文件由接入层管理，这样它换了、或第一次被创建出来，都不需要重装
    路由。provider 返回空 → 所有会话请求 503（宁可整段不可用，也不要「无 token
    = 不鉴权」这种失败方式）。
    """

    def __init__(
        self,
        token_provider: Callable[[], str | None],
        *,
        local_hosts: Iterable[str] = DEFAULT_LOCAL_HOSTS,
        local_ports: Iterable[int] = DEFAULT_LOCAL_PORTS,
    ) -> None:
        self._token_provider = token_provider
        self._local_hosts = tuple(local_hosts)
        self._local_ports = tuple(local_ports)

    # --- 单关 ------------------------------------------------------------ #

    def current_token(self) -> str:
        token = self._token_provider() or ""
        if not token:
            raise SessionAuthError(503, "auth_unavailable", _NO_TOKEN_CONFIGURED_MESSAGE)
        return token

    def check_origin(self, facts: RequestFacts) -> None:
        if not origin_allowed(
            facts.origin,
            host=facts.host,
            local_hosts=self._local_hosts,
            local_ports=self._local_ports,
        ):
            raise SessionAuthError(403, FORBIDDEN_ORIGIN_CODE, _BAD_ORIGIN_MESSAGE)

    def check_token(self, facts: RequestFacts, *, allow_query_token: bool) -> None:
        expected = self.current_token()
        presented = bearer_token(facts.authorization)
        if presented is None and allow_query_token:
            presented = (facts.query_token or "").strip() or None
        if presented is None:
            raise SessionAuthError(401, UNAUTHORIZED_CODE, _MISSING_TOKEN_MESSAGE)
        if not hmac.compare_digest(presented, expected):
            raise SessionAuthError(401, UNAUTHORIZED_CODE, _BAD_TOKEN_MESSAGE)

    # --- 组合 ------------------------------------------------------------ #

    def authorize(self, facts: RequestFacts, *, allow_query_token: bool = False) -> None:
        """会话类端点的准入。先 Origin 后 token（跨站即使 token 对也是 403）。"""
        self.check_origin(facts)
        self.check_token(facts, allow_query_token=allow_query_token)

    def issue_token(self, facts: RequestFacts) -> str:
        """bootstrap 端点：只对同源浏览器请求 / 本机非浏览器请求发 token。"""
        self.check_origin(facts)
        if not is_same_site_fetch(facts.sec_fetch_site):
            raise SessionAuthError(403, FORBIDDEN_ORIGIN_CODE, _BAD_ORIGIN_MESSAGE)
        return self.current_token()


# --------------------------------------------------------------------------- #
# web 框架胶水（按需 import，保持模块本身只依赖标准库）
# --------------------------------------------------------------------------- #


def facts_from_request(request: Any) -> RequestFacts:
    """Starlette ``Request`` → :class:`RequestFacts`。"""
    headers = request.headers
    return RequestFacts(
        origin=headers.get("origin"),
        host=headers.get("host"),
        authorization=headers.get("authorization"),
        query_token=request.query_params.get(SSE_TOKEN_QUERY_PARAM),
        sec_fetch_site=headers.get("sec-fetch-site"),
    )


def require_session_auth(
    policy: SessionAuthPolicy, *, allow_query_token: bool = False
) -> Callable[..., Any]:
    """造一个 FastAPI dependency。

    用 dependency 而不是全局 middleware：middleware 会波及宿主里所有既有路由
    （包括 Phase 1 的只读端点与静态文件），而本次的作用范围只有会话类端点。
    """

    from fastapi import Request

    async def dependency(request):  # 注解在下面显式给（见注释）
        policy.authorize(facts_from_request(request), allow_query_token=allow_query_token)

    # 本模块开了 ``from __future__ import annotations``，注解会变成**字符串**，
    # 而 FastAPI 解析签名时是在**模块**命名空间里求值的——``Request`` 是函数体内
    # 的局部名字，那边看不见，结果会被当成一个名叫 request 的必填查询参数。
    # 直接塞真正的类对象，绕开这一步字符串求值。
    dependency.__annotations__ = {"request": Request, "return": None}
    return dependency


def build_bootstrap_router(
    policy: SessionAuthPolicy, *, prefix: str = "/api", tag: str = "session-auth"
) -> Any:
    """只带 ``GET /session-auth/bootstrap`` 一条路由的最小 router。

    为什么单独有这么一个东西（批次三十七第二轮）
    --------------------------------------------
    发 token 的这条路本来长在会话 router 上，而会话 router 整体挂在
    ``session_host_v1`` 这个 feature flag 后面。批次三十七给**所有** ``/api/`` 写
    接口加了闸（R5），闸是**无条件**装的——于是 flag 关着的时候出现了一个死结：
    写接口要 token，而**唯一**能拿到 token 的那条路没有被挂上。前端评审复现的
    正是这一格。

    修法不是「把闸也放到 flag 后面」——那等于让旧写接口的安全性取决于一个与它
    无关的新功能开关（R5 的裁决里已经写明这两件事不该耦合）。也不是把整个会话
    API 提前挂上——那会把 Session Host、Driver Registry、后台任务一并拖进来，
    flag 就失去意义了。**发 token 与会话编排本来就是两件事**：前者只依赖一份
    ``SessionAuthPolicy``（= 一个 token 文件 + 一张 Origin 白名单），后者依赖半个
    内核。所以把它拆成一条独立的、零依赖的 router。

    这条路由**自己不要 token**（前端正是因为没有 token 才来问），准入靠 Origin
    白名单 + ``Sec-Fetch-Site``——与它长在会话 router 上时**逐字相同**的判断，
    调的是同一个 :meth:`SessionAuthPolicy.issue_token`。
    """
    from typing import Any as _Any

    from fastapi import APIRouter, Request

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())

    async def bootstrap_token(request):  # 注解在下面显式给（同上面的理由）
        return {"token": policy.issue_token(facts_from_request(request))}

    bootstrap_token.__annotations__ = {"request": Request, "return": _Any}
    router.add_api_route(
        BOOTSTRAP_ROUTE_PATH,
        bootstrap_token,
        methods=["GET"],
        name="session_auth_bootstrap",
    )
    return router


def has_bootstrap_route(fastapi_app: Any, *, prefix: str = "/api") -> bool:
    """宿主上已经挂了发 token 的那条路由吗（避免重复注册）。

    按**路径**判断而不是按 router 对象：flag 开着时它是会话 router 带上来的，
    关着时是 :func:`build_bootstrap_router` 挂的，两条路走到的是同一个路径。
    """
    wanted = f"{prefix}{BOOTSTRAP_ROUTE_PATH}"
    return any(getattr(route, "path", None) == wanted for route in fastapi_app.routes)


def auth_route_class() -> Any:
    """返回一个把 :class:`SessionAuthError` 变成统一错误体的 ``APIRoute`` 子类。

    为什么不是 app 级 exception handler：router 要能被任何宿主 include，不能要求
    宿主先装钩子（接入层对宿主的唯一要求就是一句 ``include_router``）。
    dependency 抛出的异常在端点函数之外，端点自己的包装器接不住它，所以口子开在
    路由层——``get_route_handler`` 正好包住「解依赖 + 调端点」这整段。
    """

    from fastapi.responses import JSONResponse
    from fastapi.routing import APIRoute

    class SessionAuthRoute(APIRoute):
        def get_route_handler(self) -> Callable[..., Any]:
            inner = super().get_route_handler()

            async def handler(request: Any) -> Any:
                try:
                    return await inner(request)
                except SessionAuthError as exc:
                    return JSONResponse(status_code=exc.status_code, content=exc.to_body())

            return handler

    return SessionAuthRoute


__all__ = [
    "BOOTSTRAP_ROUTE_PATH",
    "FORBIDDEN_ORIGIN_CODE",
    "build_bootstrap_router",
    "has_bootstrap_route",
    "RequestFacts",
    "SSE_TOKEN_QUERY_PARAM",
    "SessionAuthError",
    "SessionAuthPolicy",
    "UNAUTHORIZED_CODE",
    "auth_route_class",
    "bearer_token",
    "facts_from_request",
    "require_session_auth",
]
