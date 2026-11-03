"""Session Host 的 HTTP 接入层（Phase 3B / v1.0 §12.1 的会话段）。

提供的端点（前缀 ``/api``）
---------------------------
=================================================================  ===========================================
``GET    /conversations``                                          跨项目最近会话（侧栏索引）
``GET    /projects/{project_id}/conversations``                    该 Project 的 Conversation 列表
``POST   /projects/{project_id}/conversations``                    新建 Conversation（不启动 Runtime）
``GET    /conversations/{id}``                                     会话 + timeline 摘要 + runtime 状态 + 软提示
``PATCH  /conversations/{id}``                                      会话级模型 / 推理强度快照（AD-12 / AD-114）
``DELETE /conversations/{id}``                                     删会话（停 Runtime + 清事件；**不删**原生 Session）
``POST   /conversations/{id}/messages``                            发消息；Runtime 未起则 ``ensure_runtime`` → 202
``GET    /conversations/{id}/events``                              SSE 事件流，``?after=<sequence>`` 续传
``GET    /conversations/{id}/events/snapshot``                     同一批信封的**非流式**快照，``?since=`` 增量
``POST   /conversations/{id}/interrupt``                           中断当前一轮
``POST   /conversations/{id}/interactions/{interaction_id}``       答审批 / 答提问 / 答认证
``POST   /conversations/{id}/stop``                                停 Runtime（保留会话与原生映射）
``GET    /conversations/{id}/history``                             原生历史（只读，R-02 的权威账本）
``POST   /conversations/{id}/surface/external``                    交给外部终端（``?force=1`` 强制接管）
``POST   /conversations/{id}/surface/card``                        收回站内 + 校准（``?force=1`` 抢回）
``GET    /conversations/{id}/surface``                             当前 Surface / lease / 最近一次启动
``GET    /conversations/{id}/launches``                            终端启动历史（「上次在终端做了什么」）
``PUT    /projects/{id}/capabilities/{type}/{capId}``               能力赋值 / 禁止（AD-45）/ 解除（AD-144）
``DELETE /projects/{id}/capabilities/{type}/{capId}``               删本项目层的赋值 → 回到继承（AD-144）
``GET    /backends/{id}/models?binding=<binding_id>``               该 Binding 的 Model Catalog
``GET    /bindings/{id}/effective-settings``                       模型/推理强度/审批档/工作目录 + 各自来源
``GET    /session-auth/bootstrap``                                 同源浏览器换取本地 token（D-17）
=================================================================  ===========================================

Group（临时协作组）的那一组端点住在 :mod:`app.api.group_router`（前缀同为
``/api``，路径以 ``/groups`` 开头）：它们编排的是「谁在组里」，与这里的会话
编排是两件事。两者唯一的交叉是 ``GET /conversations`` 索引行上的 ``groupId``
（批次二十二第 2 件），取数走
:func:`~app.api.group_router.group_labels_for_conversations`。

四条口径
--------
1. **本层不是第二个 Session Host。** 每个端点都只做「解析参数 → 取领域对象 →
   调 :class:`~runtime.session_host.SessionHost` → 序列化」。编排语义
   （一条会话一条事件流、sequence 重排、崩溃收敛）全在 Session Host 里，
   HTTP 层不复制，也不绕过。
2. **错误统一形状** ``{"error": {"code", "message"}}``。code 是**稳定的机器可读串**
   （前端按 code 分支，不按文案），message 给人看。形状与包装器住在
   :mod:`app.api.api_errors`（只读领域端点用的是同一份），而不是依赖 app 级的
   exception handler——router 要能被任何宿主 include，不能要求宿主装钩子。
3. **SSE 断线只影响订阅（AD-61 / AD-62）。** 客户端断开时只关掉
   :class:`~runtime.session_host.EventSubscription`，Runtime 照跑；恢复手段就是
   带 ``?after=<sequence>`` 重连，由 Event Store 重放补齐。服务端每
   ``keepalive_interval`` 秒发一行 ``: keepalive`` 注释帧——Backend 侧没有心跳
   （AD-62 实测最大空闲 ≈ 60s 无字节），我们自己发，代理与浏览器才不会把
   长时间静默的连接判死。
4. **公共层纯度。** 本模块只认识 ``app`` / ``runtime`` / ``drivers.base`` 的类型，
   不认识任何一家 Backend 的私有概念（N §3 核心约束）。哪个 Driver 怎么注册、
   命令行长什么样，全在接入层的 bootstrap 里，不在这里。
5. **鉴权是路由的一部分，不是宿主的中间件（D-17 / AD-66）。** 这里的每个端点都
   带上 :func:`~app.api.session_auth.require_session_auth`，
   ``auth_policy`` 是**必填参数**——想挂这套路由，就必须先给出一份准入口径，
   没有「忘了配就等于不鉴权」这条路。SSE 端点额外接受 ``?token=``（``EventSource``
   带不了头），其余端点只认 ``Authorization: Bearer``。


端点按关注点分组在 :mod:`app.api.session_routes` 包里（会话记录、设置、发消息、
事件流、交互、Surface、能力写、取证）；本模块负责装配：建上下文、收集各组的
路由描述、按原有顺序注册并挂鉴权依赖。

为什么 web 框架是**函数内**导入
--------------------------------
同 :mod:`app.api.router`：``app/`` 是公共领域层，纯净性扫描会 import 它的每个
子模块；把 fastapi 的 import 关进 :func:`build_session_router`，模块本身就只依赖
标准库与 pydantic。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from app.api.api_errors import json_error_endpoint
from app.api.conversation_files import ConversationFiles
from app.api.event_wait import DEFAULT_RUN_ID_TIMEOUT
from app.api.session_auth import (
    BOOTSTRAP_ROUTE_PATH,
    SessionAuthPolicy,
    auth_route_class,
    facts_from_request,
    require_session_auth,
)
from app.api.session_routes.capabilities import build_capability_routes
from app.api.session_routes.common import (  # noqa: F401 - 对外保留的名字
    DEFAULT_KEEPALIVE_INTERVAL,
    SNAPSHOT_MAX_EVENTS,
    ArchiveConversationBody,
    BookmarkBody,
    CreateConversationBody,
    InteractionResponseBody,
    InterruptBody,
    MoveConversationBody,
    PatchConversationBody,
    PutCapabilityBody,
    RouteSpec,
    SendMessageBody,
    SessionApiError,
    _dumps,
    _not_found,
    _parse_timestamp,
)
from app.api.session_routes.context import SessionRouteContext
from app.api.session_routes.conversations import build_conversation_routes
from app.api.session_routes.debug import build_debug_routes
from app.api.session_routes.events import build_event_routes
from app.api.session_routes.interactions import build_interaction_routes
from app.api.session_routes.messages import build_message_routes
from app.api.session_routes.settings import build_settings_routes
from app.api.session_routes.surfaces import build_surface_routes
from app.persistence.base import RepositorySet
from runtime.session_host import SessionHost
from runtime.surface_handoff import SurfaceCoordinator


def build_session_router(
    *,
    session_host: SessionHost,
    repositories: RepositorySet,
    registry: Any,
    auth_policy: SessionAuthPolicy,
    prefix: str = "/api",
    tag: str = "session",
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
    run_id_timeout: float = DEFAULT_RUN_ID_TIMEOUT,
    configured_backend_ids: Sequence[str] = (),
    surface_coordinator: SurfaceCoordinator | None = None,
    debug_endpoints: bool = False,
    attachment_root: str | Path | None = None,
    module_conversation_ids: Callable[[], set[str]] | None = None,
) -> Any:
    """装配会话路由。返回一个框架的 ``APIRouter``，由调用方 include。

    ``auth_policy`` 没有默认值：D-17 之后「挂上这套路由」与「这套路由被鉴权」是
    同一件事，不给口径就装不起来。

    ``surface_coordinator`` 是 Phase 4 的 Card ⇄ External CLI 交接编排。不给就**不**
    注册那四个 surface / launches 端点——没有 Terminal Launcher 的宿主（测试、
    只跑卡片的部署）不该凭空多出一组会开终端的路由。

    ``debug_endpoints`` 为真时**额外**挂一条取证路由
    （``GET /backends/{id}/debug/last-history``）。宿主只在 ``DASH_DEBUG=1`` 时
    传真——取证端点不该在普通部署里存在，哪怕它的输出已经脱敏。

    ``configured_backend_ids`` 是 ``dashboard-config.json`` 的 ``backends[]`` 里
    **写了的** backend id（不管注册成没成功）。它只用来给「Driver 没注册」这条
    错误挑一句对的修法：配置里压根没有 → 让用户去加一条；配置里有但没注册上 →
    那是网关/base_url 的事。不给就一律按「压根没有」说——多说一句「去加配置」
    比说错方向好。
    """
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())
    ctx = SessionRouteContext(
        session_host=session_host,
        repositories=repositories,
        registry=registry,
        files=ConversationFiles(attachment_root),
        # 把 :class:`ApiError`（含 :class:`SessionApiError`）变成统一形状的 JSON。
        # 用包装器而不是 app 级 exception handler：router 要能被任何宿主 include，
        # 不能要求宿主先装钩子（`server.py` 只允许多一个 attach 调用）。
        endpoint=json_error_endpoint(JSONResponse),
        auth=Depends(require_session_auth(auth_policy)),
        auth_sse=Depends(require_session_auth(auth_policy, allow_query_token=True)),
        configured_backend_ids=configured_backend_ids,
        surface_coordinator=surface_coordinator,
        keepalive_interval=keepalive_interval,
        run_id_timeout=run_id_timeout,
        module_conversation_ids=module_conversation_ids,
        attachment_root=attachment_root,
    )

    # --- token 分发（D-17） -------------------------------------------------- #

    async def bootstrap_token(request):  # 注解见下（同 session_auth 的理由）
        """同源浏览器在这里拿 token；跨站 Origin 403。

        这个端点**自己**不能要 token（前端还没有 token 才来问），所以它的准入靠
        Origin 白名单 + ``Sec-Fetch-Site``。本机的非浏览器进程照样能取到——那是
        本设计的既定边界：token 文件本来就 0600，同用户的进程本来就读得到，
        这里不再假装能挡住它。
        """
        return {"token": auth_policy.issue_token(facts_from_request(request))}

    # 模块开了 `from __future__ import annotations`，而 `Request` 是本函数体内的
    # 局部名字；注解留成字符串会被 FastAPI 当查询参数解析。塞真类对象绕开它。
    bootstrap_token.__annotations__ = {"request": Request, "return": Any}

    # --- 注册 --------------------------------------------------------------- #
    #
    # 各组按原有顺序收集；同一层的字面量段（`/conversations`、`/events/snapshot`、
    # `capabilities/_meta`）在各自组内排在参数段之前，Starlette 按注册顺序匹配。
    specs: list[RouteSpec] = [
        *build_conversation_routes(ctx),
        *build_message_routes(ctx),
        *build_event_routes(ctx),
        *build_interaction_routes(ctx),
        *build_surface_routes(ctx),
        *build_capability_routes(ctx),
        *build_settings_routes(ctx),
        RouteSpec(BOOTSTRAP_ROUTE_PATH, bootstrap_token, ["GET"], "session_auth_bootstrap", auth="none"),
        # 只在 `DASH_DEBUG=1` 时存在。鉴权照旧：脱敏归脱敏，取证面也不该对
        # 未鉴权的请求开着。
        *build_debug_routes(ctx),
    ]
    dependencies = {"bearer": [ctx.auth], "sse": [ctx.auth_sse], "none": []}
    for when in ("always", "debug", "coordinator"):
        if when == "debug" and not debug_endpoints:
            continue
        if when == "coordinator" and surface_coordinator is None:
            continue
        for item in specs:
            if item.when != when:
                continue
            router.add_api_route(
                item.path, item.endpoint, methods=item.methods, name=item.name,
                dependencies=dependencies[item.auth],
            )
    return router


__all__ = [
    "DEFAULT_KEEPALIVE_INTERVAL",
    "DEFAULT_RUN_ID_TIMEOUT",
    "CreateConversationBody",
    "InteractionResponseBody",
    "SendMessageBody",
    "SessionApiError",
    "build_session_router",
]
