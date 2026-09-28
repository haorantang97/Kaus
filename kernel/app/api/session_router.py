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

为什么 web 框架是**函数内**导入
--------------------------------
同 :mod:`app.api.router`：``app/`` 是公共领域层，纯净性扫描会 import 它的每个
子模块；把 fastapi 的 import 关进 :func:`build_session_router`，模块本身就只依赖
标准库与 pydantic。
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.conversation_files import ConversationFiles, MAX_UPLOAD_JSON_BYTES
from app.api.event_wait import (
    DEFAULT_RUN_ID_TIMEOUT,
    STREAM_END as _STREAM_END,
    STREAM_IDLE as _STREAM_IDLE,
    first_run_id as _first_run_id,
    next_or_none as _next_or_none,
)
from app.api.group_router import group_labels_for_conversations
from app.api.session_views import (
    KEEPALIVE_FRAME,
    SSE_HEADERS,
    SSE_PADDING_FRAME,
    advisory_to_wire,
    conversation_index_entry_to_wire,
    conversation_list_to_wire,
    conversation_status,
    conversation_surface,
    conversation_to_wire,
    debug_shape,
    effective_settings_to_wire,
    envelope_to_wire,
    model_catalog_to_wire,
    native_history_to_wire,
    runtime_view,
    session_projection_to_wire,
    sse_data_line,
    timeline_summary,
)
from app.api.session_auth import (
    BOOTSTRAP_ROUTE_PATH,
    SessionAuthPolicy,
    auth_route_class,
    facts_from_request,
    require_session_auth,
)
from app.api.views import (
    effective_capability_entry_to_wire,
    normalize_backend_key_id,
    normalize_project_id,
)
from app.capabilities.models import (
    GENERIC_CAPABILITY_TYPES,
    ProjectCapability,
    parse_capability_type,
)
from app.capabilities.effective import resolve_effective_for_project
from app.errors import CapabilityTypeError
from app.conversations.models import Conversation
from app.conversations.repository import DEFAULT_RECENT_LIMIT, MAX_RECENT_LIMIT
from app.persistence.base import RepositorySet
from app.projects.binding_snapshot import register_binding_snapshot
from drivers.base import (
    AGENT_SPAWN_FAILED,
    AgentSpawnError,
    AuthRequiredError,
    DriverError,
    DriverNotRegisteredError,
    FailureHint,
    InteractionNotFoundError,
    InteractionResponse,
    MessageInput,
    ModelRejectedError,
    RuntimeNotFoundError,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    plain_text,
)
from runtime.event_envelope import AgentError, RunFailed
from runtime.event_reducer import TimelineState
from runtime.session_host import (
    BindingNotFoundError,
    ExternalSurfaceActiveError,
    RuntimeNotActiveError,
    SessionHost,
    StaleRunError,
)
from runtime.surface_handoff import (
    SurfaceConflictError,
    SurfaceCoordinator,
    SurfaceError,
    SurfaceUnsupportedError,
    launch_to_wire,
)

#: SSE 心跳周期（秒）。服务端每这么久发一行 ``: keepalive``。批次二十八从 25s
#: 收到 15s：Cloudflare quick tunnel 一类的中间层对静默连接更没耐心，而心跳本身
#: 是一行注释，代价可以忽略。心跳只有这一套机制，别再叠第二套。
DEFAULT_KEEPALIVE_INTERVAL: float = 15.0

#: 非流式快照一次最多回多少条事件（批次三十第 1 件）。
#:
#: 为什么要有这条端点：真机测试员经 Cloudflare quick tunnel + 自己的代理看页面，
#: SSE 的**字节一个都不到**（17 秒内既没有重放也没有心跳），而同一份数据在
#: localhost 渲染完全正常（`docs/quality/retest-forensics-2026-09-05.md`）。别人的
#: 网络我们修不了，但可以让产品在没有长连接的情况下照样能用——同样的信封，改用
#: 普通的 `GET` 拿。
#:
#: 上限不是性能考虑，是**不让一次响应无界增长**：超过就把 `truncated` 置真，客户端
#: 带着新的 `since` 立刻再取一次。静默截断会让页面永远差最后那一段而自己不知道。
SNAPSHOT_MAX_EVENTS: int = 2000

#: ``POST /messages`` 为了在 202 里带上真实 ``runId`` 而等待 ``run.*`` 首帧的上限。
#: 超时**不编造** id：返回 ``runId: null`` + ``runIdPending: true``（N §13.1）。
#: 定义住在 :mod:`app.api.event_wait`（Group 的投递要用同一个预算）。


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


class SessionApiError(ApiError):
    """带稳定 code 的会话接口错误。序列化成 ``{"error": {...}}``。

    形状与行为都在 :class:`~app.api.api_errors.ApiError` 里——批次八第 5 件之后
    只读领域端点用的是同一个基类。这里只留一个名字，方便 ``except`` 时按
    「会话层的错误」窄化。
    """


def _not_found(code: str, message: str) -> SessionApiError:
    return SessionApiError(404, code, message)


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class ArchiveConversationBody(_Body):
    archived: bool


class CreateConversationBody(_Body):
    """``POST /projects/{id}/conversations``。

    ``native_session_ref`` 是**可选**的收养入口（v1.0 §8.7 / §4.4.1）：给了就把
    这条 Conversation 显式绑到一个已存在的原生 Session；不给就留空，等 Driver
    在首轮里创建（N §9 允许懒创建）。公共层不解释这个串的形状——它由 Driver 解释。
    """

    binding_id: str
    title: str | None = None
    native_session_ref: str | None = None


class PatchConversationBody(_Body):
    """``PATCH /api/conversations/{id}``：会话标题 / 会话级模型 / 推理强度。

    三项都是**可选**的：只给 ``modelId`` 就只改模型。``modelId`` /
    ``reasoningMode`` 给了空串视同没给（前端的空下拉不该被解释成「清空快照」
    ——清空是另一个动作，还没有需求）。

    ``title``（批次五十二第 5 件）：会话自己的名字。它此前**没有任何界面入口**
    ——仪表盘侧栏那个 ⋯ 菜单里的「改名」改的是**项目的显示名**，不是会话标题；
    而会话标题正是组里显示名回退链的第二档（``roleLabel > 会话标题 > id 尾段``），
    所以真机上测试员「找不到改名入口」这件事是真的。与模型那两项放在同一条
    PATCH，而不是新开一条改名端点：改标题是改这条会话的一个字段。
    """

    title: str | None = None
    model_id: str | None = None
    reasoning_mode: str | None = None
    approval_mode: str | None = None
    execution_mode: str | None = Field(default=None, max_length=256)


class PutCapabilityBody(_Body):
    """``PUT /api/projects/{id}/capabilities/{type}/{capId}`` 的 body（AD-144）。

    两项都可选，但不能都不给：``value`` 是本项目层的赋值内容（一个 JSON 对象，
    公共层不解释里面的键——那是各能力类型自己的 schema），``blocked`` 是 AD-45
    的禁止开关（``true`` 写禁止、``false`` 解除）。
    """

    value: dict[str, Any] | None = None
    blocked: bool | None = None


class SendMessageBody(_Body):
    """``POST /conversations/{id}/messages`` 的 body = 公共 ``MessageInput``。

    ``clientRef`` 是可选的对账编号（AD-94）：页面在发送时生成一个，服务端**原样**
    放进 ``kaus/user.message`` 事件的 ``data.clientRef``，页面据此把本地占位换成
    正式条目——按编号换，不按文本匹配。服务端不生成、不校验唯一性、不做任何解释，
    只限长度（64 字符）以免有人把整段正文塞进来。空串视同没给。
    """

    text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    metadata: dict[str, Any] = Field(default_factory=dict)
    client_ref: str | None = Field(default=None, max_length=64)

    def to_message_input(self) -> MessageInput:
        return MessageInput(
            text=self.text,
            attachments=tuple(self.attachments),  # type: ignore[arg-type]
            metadata=dict(self.metadata),
            client_ref=(self.client_ref or "").strip() or None,
        )


class InteractionResponseBody(_Body):
    """``POST /conversations/{id}/interactions/{interaction_id}`` 的 body。"""

    kind: str
    option_id: str | None = None
    option_ids: tuple[str, ...] = ()
    text: str | None = None
    cancelled: bool = False

    def to_response(self) -> InteractionResponse:
        return InteractionResponse(
            kind=self.kind,  # type: ignore[arg-type]
            option_id=self.option_id,
            option_ids=self.option_ids,
            text=self.text,
            cancelled=self.cancelled,
        )


class InterruptBody(_Body):
    expected_run_id: str | None = Field(default=None, max_length=512)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


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
    from fastapi import APIRouter, Body, Depends, Query, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())
    host = session_host
    files = ConversationFiles(attachment_root)
    _auth = Depends(require_session_auth(auth_policy))
    _auth_sse = Depends(require_session_auth(auth_policy, allow_query_token=True))

    # --- 通用助手 ---------------------------------------------------------- #

    # 把 :class:`ApiError`（含 :class:`SessionApiError`）变成统一形状的 JSON。
    # 用包装器而不是 app 级 exception handler：router 要能被任何宿主 include，
    # 不能要求宿主先装钩子（`server.py` 只允许多一个 attach 调用）。
    _endpoint = json_error_endpoint(JSONResponse)

    _configured = frozenset(
        normalize_backend_key_id(value) for value in configured_backend_ids
    )

    def _driver_not_registered(backend_id: str) -> SessionApiError:
        """「这个 Backend 没有 Driver」→ 一句人话 + 怎么修（批次十一第 3 件）。

        原来这里回的是 ``str(exc)``，也就是一句 ``未注册的 backend：'backend:xxx'``
        —— 这句话对的，但它只说了「坏了」，没说「怎么修」，用户在界面上看到的
        就是一条内部报错。现在 ``message`` 说人话，``detail`` 里放机器要的三样：
        ``backendId`` / 当前**已注册**的 id 列表 / 一句 ``hint``。

        两种 hint 对应两种根因，靠「配置里有没有这条 id」区分：配置里压根没有，
        那是少写了一段配置；配置里有却没注册上，那是这条配置本身没生效
        （``base_url`` 没填、网关没起）。detail 里只有 id，不含任何配置内容。
        """
        normalized = normalize_backend_key_id(backend_id)
        if normalized in _configured:
            failure = FailureHint(
                code="backend_configured_but_not_registered",
                message=f"这台机器上还没有可用的「{normalized}」引擎，所以这条会话发不出去。",
                hint="该引擎的网关没在运行或 base_url 未配置",
            )
        else:
            failure = FailureHint(
                code="backend_not_configured",
                message=f"这台机器上还没有可用的「{normalized}」引擎，所以这条会话发不出去。",
                hint=(
                    f"在 dashboard-config.json 的 backends 里加一条 id 为 {normalized}、"
                    "driver 为 native-http 的配置（见 docs/ops/backends.md）"
                ),
            )
        return SessionApiError(
            503,
            "driver_not_registered",
            failure.message,
            detail={
                "backendId": normalized,
                "registered": list(registry.backend_ids()),
                # 批次十六第 1 件：`hint` 的形状与 `runtime_start_failed` 统一，
                # 都来自 :class:`drivers.base.FailureHint`（AD-108 的口径推广）。
                **failure.to_detail(),
            },
        )

    def _driver_message(exc: BaseException) -> str:
        """Driver 异常 → 一句**纯文本人话**（批次二十七第 2 件）。

        认得出根因就用 :class:`~drivers.base.FailureHint` 的文案；认不出来才退回
        异常原文，且照样经 :func:`~drivers.base.plain_text` 剥掉类名与标记。
        """
        failure = getattr(exc, "failure", None)
        if isinstance(failure, FailureHint):
            return failure.message
        return plain_text(str(exc)) or "这一句没有发出去"

    def _attach_client_ref(exc: SessionApiError, client_ref: str | None) -> None:
        """把 AD-94 的对账编号塞进错误体的 ``detail``（R6）。

        ``detail`` 可能还没有（不是每条错误都带 hint），所以这里按需建一个；
        已有的键一个都不动。
        """
        if not client_ref:
            return
        detail = exc.extra.get("detail")
        if not isinstance(detail, dict):
            detail = {}
        detail["clientRef"] = client_ref
        exc.extra["detail"] = detail

    def _hint(exc: BaseException) -> dict[str, Any] | None:
        failure = getattr(exc, "failure", None)
        return failure.to_detail() if isinstance(failure, FailureHint) else None

    def _auth_required(
        exc: AuthRequiredError, extra: dict[str, Any] | None = None
    ) -> SessionApiError:
        """「先去登录」→ 400 ``auth_required``（AD-157）。

        为什么是 400 而不是 503：503 说的是「这台机器上的引擎坏了/没起来」，
        用户对它无能为力；这一条说的是「这次请求在当前登录状态下不能被接受」，
        而修法在用户自己手上。前端据这个 code 在输入区显示一行带命令的提示。

        ``authMethods`` 只是引擎自报的登录方式 **id**，用来把「去登哪一种」说清楚；
        它不是凭据，也从不携带任何值（AD-156b）。
        """
        failure = exc.failure
        detail: dict[str, Any] = (
            failure.to_detail() if isinstance(failure, FailureHint) else {}
        )
        if exc.auth_methods:
            detail["authMethods"] = list(exc.auth_methods)
        message = (
            failure.message
            if isinstance(failure, FailureHint)
            else plain_text(str(exc)) or "这台引擎还没有登录"
        )
        return SessionApiError(
            400, "auth_required", message, detail=detail or None, **(extra or {})
        )

    def _spawn_failed(
        exc: AgentSpawnError, extra: dict[str, Any] | None = None
    ) -> SessionApiError:
        """「引擎进程没能拉起来」→ 503 ``agent_spawn_failed``（AD-159）。

        为什么不是 500、也不是笼统的 ``runtime_start_failed``：500 说的是「我们
        自己坏了」，而这一条的根因与修法都在用户这台机器上；``runtime_start_failed``
        则把「网关没在跑」「进程执行失败」两种完全不同的修法混成一个码，前端也就
        只能显示同一句废话。真机上这个混淆的代价是一整夜——见 AD-159。
        """
        failure = exc.failure
        message = (
            failure.message
            if isinstance(failure, FailureHint)
            else plain_text(str(exc)) or "引擎进程没能拉起来"
        )
        detail = failure.to_detail() if isinstance(failure, FailureHint) else {}
        return SessionApiError(
            503,
            AGENT_SPAWN_FAILED,
            message,
            detail=detail or None,
            **(extra or {}),
        )

    async def _emit_send_failed(
        conversation: Conversation, exc: AuthRequiredError
    ) -> None:
        """把「这一句没发出去」写进会话流，让时间线**看得见**这次失败。

        用已有的 ``run.failed`` 信封（v1.1 冻结期内不新造事件类型），``run_id``
        留空——这一轮压根没开始。合成失败不能反过来把这条请求变成 500，所以整个
        包在 suppress 里：用户要看到的是那句 400 的错误，不是我们记事件时摔的跤。
        """
        failure = exc.failure
        message = (
            failure.describe()
            if isinstance(failure, FailureHint)
            else plain_text(str(exc)) or "这台引擎还没有登录"
        )
        with contextlib.suppress(Exception):
            await host.emit_conversation_event(
                conversation,
                RunFailed(
                    run_id=None,
                    error=AgentError(
                        code="auth_required", message=message, retriable=True
                    ),
                ),
            )

    def _unsupported(exc: UnsupportedCapabilityError) -> SessionApiError:
        """「引擎不支持这件事」→ 501（批次十七第 3 件）。

        Driver 在异常上带了 :class:`~drivers.base.FailureHint` 时用它的 ``code``
        与人话文案（例如 ``conversation_model_unsupported``），并把修法放进
        ``detail``；没带就退回通用的 ``unsupported_capability`` + 异常原文。

        为什么不能让它漏成 500：这是**已知的**、可解释的拒绝，用户还有一条明确的
        路可走（改 Binding 默认模型）。一条三位数字什么都没说。
        """
        failure = getattr(exc, "failure", None)
        if isinstance(failure, FailureHint):
            return SessionApiError(
                501, failure.code, failure.message, detail=failure.to_detail()
            )
        return SessionApiError(501, "unsupported_capability", str(exc))

    async def _load_conversation(conversation_id: str) -> Conversation:
        conversation = await repositories.conversations.get(conversation_id)
        if conversation is None:
            raise _not_found(
                "conversation_not_found", f"未知 Conversation：{conversation_id}"
            )
        return conversation

    async def _prepare_driver(conversation: Conversation) -> Any:
        """AD-58：Driver 侧的 Binding 缓存由接入层在开 Runtime 前灌一次。

        Binding 仓库属上层，Driver 不主动查库；有 ``register_binding`` 的 Driver
        在这里拿到快照，没有的（Mock）什么都不做。

        AD-175：灌进去之前先用**项目的**工作目录把 Binding 上空着的那一格补上
        （:func:`~app.projects.binding_snapshot.register_binding_snapshot`），
        这样「起会话的 cwd」和「投影写文件的目标」用的是同一把尺子。
        """
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise _not_found(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(binding.backend_id) from exc
        await register_binding_snapshot(driver, binding, repositories.projects)
        return binding

    async def _ensure_runtime(conversation: Conversation) -> Any:
        binding = await _prepare_driver(conversation)
        try:
            return await host.ensure_runtime(conversation)
        except BindingNotFoundError as exc:
            raise _not_found("binding_not_found", str(exc)) from exc
        except DriverNotRegisteredError as exc:
            # 上面 `_prepare_driver` 已经挡过一次，这里是 Session Host 自己再查
            # 一遍注册表的兜底路径（比如两次查询之间 Driver 被摘掉了）。
            raise _driver_not_registered(binding.backend_id) from exc
        except ExternalSurfaceActiveError as exc:
            # AD-122：外部终端持有写权时站内**不写**。前端按这个 code 显示
            # 「正在外部终端运行」，并给一条「收回站内」（`/surface/card`）的路。
            raise SessionApiError(
                409,
                "surface_external_active",
                str(exc),
                detail={
                    "owner": exc.lease.owner_type,
                    "ownerId": exc.lease.owner_id,
                    "acquiredAt": exc.lease.acquired_at.isoformat(),
                    "launchId": exc.lease.metadata.get("launchId"),
                },
            ) from exc
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc

    async def _empty_conversation_extra(conversation: Conversation) -> dict[str, Any]:
        """这条会话到现在为止是不是「白建的」（批次十一第 2 件）。

        判据两条，都要满足：``EventStore`` 里一条事件都没有，且还没有绑上原生
        Session。满足就说明它从建出来到现在什么都没发生过——首条消息又发不出去，
        留着就是一条永远空着的会话。

        服务端**不自动删**：删除是显式动作（有单独的 ``DELETE`` 端点），这里只
        给前端一个「删了也不会丢东西」的凭据，删不删由用户那一侧决定。
        """
        if conversation.native_session_id is not None:
            return {}
        if await host.event_store.latest_sequence(conversation.id) is not None:
            return {}
        return {"emptyConversation": True}

    async def _surfaces_of(
        conversations: Sequence[Conversation],
    ) -> dict[str, str]:
        """批次十五第 2 件：一批会话各自在哪个 Surface 上写。

        lease 一次性全取（``describe_all``）：会话索引是侧栏每 30s 拉一次的东西，
        一条会话一次点查会让 50 条列表变成 50 次查库——与 backendId 那里同一个
        理由。取不到 lease 表就退回每条会话自己的 ``preferred_surface``：
        没有写权记录时，那正是「上次在哪个界面」的正确答案。
        """
        leases = {
            description.conversation_id: description
            for description in await host.leases.describe_all()
        }
        return {
            conversation.id: conversation_surface(
                conversation, lease=leases.get(conversation.id)
            )
            for conversation in conversations
        }

    async def _timeline_of(conversation_id: str) -> TimelineState | None:
        """活跃 runtime 直接取 Reducer 的状态；否则从缓冲重算一次。

        重算而不是「返回 null」：停掉 runtime 之后卡片仍要能显示上一轮的样子，
        而 Event Store 里的事件本来就是为重放存在的（D-16）。缓冲已过期清空时
        才真的是 ``None``。

        批次十七：重算这件事下沉到 Session Host（自愈要用同一份判断，两处各写
        一遍迟早会算出两个答案）。这里只留一个名字。
        """
        return await host.timeline_from_store(conversation_id)

    # --- Conversation 列表 / 新建 ------------------------------------------ #

    @_endpoint
    async def list_conversations(
        project_id: str, include_group_only: bool = Query(False), archived: bool = Query(False)
    ) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        conversations = list(
            await repositories.conversations.list_for_project(
                normalized, include_group_only=include_group_only, archived=archived
            )
        )
        return conversation_list_to_wire(
            conversations,
            project_id=normalized,
            surfaces=await _surfaces_of(conversations),
            # AD-159：列表里的旧字段 `state` 也按 runState 收敛，免得侧栏与会话页
            # 对同一条会话说两句话。
            run_states={
                c.id: await host.run_state_of(c) for c in conversations
            },
        )

    @_endpoint
    async def create_conversation(
        project_id: str, body: CreateConversationBody = Body(...)
    ) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        binding = await repositories.bindings.get(body.binding_id)
        if binding is None:
            raise _not_found("binding_not_found", f"未知 Binding：{body.binding_id}")
        if binding.project_id != normalized:
            raise SessionApiError(
                400,
                "binding_project_mismatch",
                f"Binding {binding.id} 不属于 Project {normalized}（D-06：不在中途换绑）",
            )
        conversation = Conversation.create(
            project_id=normalized,
            agent_binding_id=binding.id,
            title=(body.title or "").strip() or binding.display_name,
            native_session_id=body.native_session_ref,
        )
        saved = await repositories.conversations.save(conversation)
        return JSONResponse(status_code=201, content=conversation_to_wire(saved))

    # --- 跨项目会话索引（批次八第 1 件） ------------------------------------ #

    @_endpoint
    async def list_recent_conversations(
        updated_after: str | None = Query(None, alias="updated_after"),
        limit: int = Query(DEFAULT_RECENT_LIMIT, ge=1, le=MAX_RECENT_LIMIT),
        project: str | None = Query(None),
        include_group_only: bool = Query(False),
        archived: bool = Query(False),
    ) -> Any:
        """``GET /api/conversations``：跨项目的最近会话，按 ``updatedAt`` 倒序。

        侧栏此前是 `GET /api/projects` + 每个项目一次 `/conversations`，
        项目一多就是 30s 一次的 N+1。这里一次查完。

        ``?project=`` 把同一个端点收窄成单项目视图（接受 ``project:<slug>``
        与裸 slug，与其它端点同一口径）；``?updated_after=`` 是增量游标
        （ISO-8601，严格大于）。
        """
        since = _parse_timestamp(updated_after)
        project_id: str | None = None
        if project:
            project_id = normalize_project_id(project)
            if await repositories.projects.get(project_id) is None:
                raise _not_found("project_not_found", f"未知 Project：{project}")
        conversations = list(
            await repositories.conversations.list_recent(
                updated_after=since,
                project_id=project_id,
                limit=limit,
                include_group_only=include_group_only,
                archived=archived,
            )
        )
        # Binding 按 id 去重后再取：一条会话一次查库会让 50 条列表变成 50 次
        # 点查，而同一个项目下的会话通常共用少数几条 Binding。
        backend_by_binding: dict[str, str | None] = {}
        for binding_id in {c.agent_binding_id for c in conversations}:
            binding = await repositories.bindings.get(binding_id)
            backend_by_binding[binding_id] = binding.backend_id if binding else None

        surfaces = await _surfaces_of(conversations)
        # 批次二十二第 2 件：侧栏每行要标「这条会话在哪个组里」。按组查一次，
        # 不是一条会话一次点查——理由与上面 Binding 去重同一条。
        groups = await group_labels_for_conversations(
            repositories, [c.id for c in conversations]
        )
        entries = []
        for conversation in conversations:
            timeline = host.timeline(conversation.id)
            entries.append(
                conversation_index_entry_to_wire(
                    conversation,
                    backend_id=backend_by_binding.get(conversation.agent_binding_id),
                    status=conversation_status(
                        conversation,
                        live_run_state=(
                            timeline.run_state if timeline is not None else None
                        ),
                    ),
                    last_sequence=await host.event_store.latest_sequence(
                        conversation.id
                    ),
                    surface=surfaces.get(conversation.id, "card"),
                    group_id=(groups.get(conversation.id) or {}).get("id"),
                    group_title=(groups.get(conversation.id) or {}).get("title"),
                )
            )
        return {
            "conversations": entries,
            "count": len(entries),
            # 下一次增量刷新的游标：本页最大的 updatedAt。空页时回 null，
            # 由客户端沿用它手上那个（不要退回「从头再来」）。
            "nextUpdatedAfter": entries[0]["updatedAt"] if entries else None,
        }

    # --- 单条 Conversation -------------------------------------------------- #

    @_endpoint
    async def get_conversation(conversation_id: str) -> Any:
        """会话 + 时间线摘要 + runtime 状态 + ``runState``（批次十七第 1/5 件）。

        取会话是页面进来的第一件事，所以自愈也放在这里（AD-136 的第二处）：
        「页面显示运行中、后端其实早就没了」这种失配在这一刻就收敛掉，用户不必
        先点一次停止才发现对不上。
        """
        conversation = await _load_conversation(conversation_id)
        await host.reconcile_conversation(conversation)
        # AD-143：缓冲空了就从原生历史重建（v1.0 §11.3）。放在自愈之后：自愈只
        # 收敛「跑没跑完」，重建管的是「内容还在不在」，两件事互不替代。
        await host.restore_from_native_history(conversation)
        conversation = await _load_conversation(conversation_id)
        handle = host.handle_for(conversation_id)
        owner = await host.describe_runtime_owner(conversation_id)
        # AD-159：一份 runState，两个字段共用。旧的 `conversation.state` 从此是它的
        # 投影，不再是另一份可能过期的快照——真机上这两个字段曾经在同一个响应里
        # 各说各话（VERIFY-BATCH-31/34）。
        run_state = await host.run_state_of(conversation)
        runtime = runtime_view(active=host.is_active(conversation_id), handle=handle, owner=owner)
        runtime["workspaceRoot"] = await _conversation_workspace(conversation)
        return {
            "conversation": conversation_to_wire(conversation, run_state=run_state),
            "runtime": runtime,
            # 页头以它为准，而不是自己从时间线推断（批次十七第 5 件）。
            "runState": run_state,
            "timeline": timeline_summary(await _timeline_of(conversation_id)),
            # R-04：软提示与「能不能发」无关，UI 照常允许发送。
            "advisory": advisory_to_wire(host.advisory_for(conversation_id)),
            # 批次四十三：这条会话建起来时**真的送进引擎**的项目能力，只有名字
            # （没有 command / url / env——那些一个字都不上 wire）。null 的含义是
            # 「这条路没走」：没装能力投影器、这台引擎没有随会话送能力这条路、
            # 或者这是一条续接上来的会话（协议不允许续接时换挂载）。
            "projection": session_projection_to_wire(
                host.session_projection(conversation_id)
            ),
        }

    @_endpoint
    async def archive_conversation(conversation_id: str, body: ArchiveConversationBody = Body(...)) -> Any:
        try:
            async with host.conversation_control(conversation_id):
                conversation = await _load_conversation(conversation_id)
                owner = await host.describe_runtime_owner(conversation_id)
                if body.archived and owner and owner.owner_type == "external-cli":
                    raise SessionApiError(409, "surface_external_active", "请先结束终端中的对话。")
                if body.archived:
                    await host.stop_runtime(conversation_id)
                    conversation = await _load_conversation(conversation_id)
                saved = await repositories.conversations.save(conversation.evolve(
                    archived_at=(conversation.archived_at or datetime.now(timezone.utc)) if body.archived else None,
                    updated_at=datetime.now(timezone.utc),
                ))
                return {"conversation": conversation_to_wire(saved)}
        except TurnAlreadyRunningError as exc:
            raise SessionApiError(409, "turn_already_running", "本轮结束后可归档。") from exc

    @_endpoint
    async def delete_conversation(conversation_id: str) -> Any:
        """删一条 Conversation（v1.0 §16.6）。

        三步，顺序不能换：**先**停 Runtime（还在跑的话，停的时候还要写回会话
        状态，领域行得在），**再**删领域记录，**最后**清事件缓冲并给在线的 SSE
        订阅者播一条 ``kaus/conversation.deleted`` 后收流。

        **原生 Session 不删**：那是 Backend 的账本（D-16 / §16.6），仪表盘只删
        自己这一侧的映射与缓冲。删过之后 ``GET /api/conversations`` 与
        ``/projects/{id}/conversations`` 自然不再返回它——不需要额外的「已删除」
        标记，因为这里做的是真删除，不是软删除。
        """
        conversation = await _load_conversation(conversation_id)
        await host.stop_runtime(conversation_id)
        await repositories.conversations.delete(conversation_id)
        await host.purge_conversation(conversation)
        return {"conversationId": conversation_id, "deleted": True}

    @_endpoint
    async def patch_conversation(
        conversation_id: str, body: PatchConversationBody = Body(...)
    ) -> Any:
        if body.model_id or body.reasoning_mode or body.approval_mode:
            try:
                async with host.conversation_control(conversation_id):
                    return await _patch_conversation_locked(conversation_id, body)
            except TurnAlreadyRunningError as exc:
                raise SessionApiError(409, "turn_already_running", "本轮结束后可修改设置。") from exc
        return await _patch_conversation_locked(conversation_id, body)

    async def _patch_conversation_locked(conversation_id: str, body: PatchConversationBody) -> Any:
        """会话级改模型 / 推理强度（批次十六第 4 件，落实 AD-114 的后半句）。

        在此之前，界面上的模型下拉改的是 **Binding 的默认模型**——也就是「改一条
        会话的模型」会顺带改掉这个项目下所有新会话的默认值，所以前端得先弹一次
        确认。这个端点给出会话级的那条路。

        三条口径：

        1. **快照即快照（AD-12）**：写的是这条 Conversation 从此刻起用什么，
           不回溯已经发生的回合（那由原生历史回答，D-16），也不动 Binding。
        2. **引擎认不认要先问**：``models.conversation_scoped`` 不是「支持」的
           引擎回 501 ``conversation_model_unsupported``——静默接受一个引擎不会
           照做的快照，等于在界面上撒谎——真机上就有引擎是这一档：它发起回合的
           请求里根本没有模型字段。
        3. **校验按目录来，目录为空就不校验**：模型必须在该 Binding 的 Catalog 里，
           推理强度必须在该模型的 ``reasoning_levels`` 里。目录是空的（引擎没报
           可用模型，批次十三改判规则 4b）时不拦——那时我们并不知道什么是合法的，
           拦下去只会把用户堵死。
        """
        conversation = await _load_conversation(conversation_id)
        model_id = (body.model_id or "").strip() or None
        reasoning_mode = (body.reasoning_mode or "").strip() or None
        approval_mode = (body.approval_mode or "").strip() or None
        execution_mode = (body.execution_mode or "").strip() or None
        if execution_mode and (model_id or reasoning_mode or approval_mode):
            raise SessionApiError(400, "mixed_settings_patch", "运行模式与其他设置请分别修改。")
        if approval_mode and (model_id or reasoning_mode):
            raise SessionApiError(400, "mixed_settings_patch", "权限与模型设置请分别修改。")
        if model_id is None and reasoning_mode is None and approval_mode is None and execution_mode is None and body.title is None:
            raise SessionApiError(
                400,
                "empty_patch",
                "请求里没有任何要改的项（title / modelId / reasoningMode 至少给一个）",
            )

        # 批次五十二第 5 件：改标题**先落，且不过引擎那一关**。
        #
        # 下面那几步（取 Binding → 取 Driver → 问能力表）是给「会话级模型」准备的，
        # 而给一条会话改个名字与引擎毫无关系——把它塞在那后面，会让一台不支持会话级
        # 模型的引擎上「改名」吃一个 501 `conversation_model_unsupported`。
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise SessionApiError(
                    400,
                    "empty_title",
                    "会话标题不能为空（清空标题没有意义：列表里那一行总要有字）",
                    detail={"conversationId": conversation.id},
                )
            conversation = await repositories.conversations.save(
                conversation.evolve(title=title)
            )
            # 活跃 runtime 手上那份也换一下（同下面模型快照那一步的理由）。
            host.update_conversation(conversation)
            if model_id is None and reasoning_mode is None and approval_mode is None and execution_mode is None:
                return {
                    "conversation": conversation_to_wire(
                        conversation, run_state=await host.run_state_of(conversation)
                    ),
                    # 标题不下发给引擎，所以这一格恒为 false（它说的是模型那件事）。
                    "appliedToRuntime": False,
                }
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise _not_found(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(binding.backend_id) from exc
        await register_binding_snapshot(driver, binding, repositories.projects)

        def _unsupported_model_switch(scoped: str | None = None) -> SessionApiError:
            return SessionApiError(
                501,
                "conversation_model_unsupported",
                "这台引擎不支持按会话指定模型或推理强度：它只按 Binding 的默认值跑。",
                detail={
                    "backendId": binding.backend_id,
                    "bindingId": binding.id,
                    "conversationScoped": scoped,
                    "hint": "改这条 Binding 的默认模型，或新建一条会话",
                },
            )

        capabilities = await driver.get_capabilities()
        if (model_id or reasoning_mode) and not capabilities.models.conversation_scoped.is_supported:
            raise _unsupported_model_switch(
                capabilities.models.conversation_scoped.value
            )

        provider_id: str | None = None
        try:
            catalog = await driver.get_model_catalog(binding)
        except UnsupportedCapabilityError:
            catalog = None
        controls_reader = getattr(driver, "conversation_controls", None)
        controls = controls_reader(binding) if callable(controls_reader) else {}
        if reasoning_mode and not controls.get("reasoning", False):
            raise SessionApiError(501, "conversation_reasoning_unsupported", "当前引擎不支持会话推理设置。")
        if approval_mode and approval_mode not in controls.get("approvalModes", []):
            raise SessionApiError(501, "conversation_approval_unsupported", "当前引擎未提供这个会话权限选项。")
        if execution_mode and execution_mode not in [option['id'] for option in controls.get('executionModes', [])]:
            raise SessionApiError(501, "conversation_execution_unsupported", "当前引擎未提供这个运行模式。")
        if catalog is not None and catalog.models:
            descriptor = next(
                (m for m in catalog.models if m.model_id == (model_id or conversation.model_id or catalog.default_model_id)),
                None,
            )
            if model_id is not None and descriptor is None:
                raise SessionApiError(
                    400,
                    "model_not_in_catalog",
                    f"模型 {model_id!r} 不在这条 Binding 的目录里",
                    detail={
                        "modelId": model_id,
                        "available": [m.model_id for m in catalog.models],
                    },
                )
            if descriptor is not None:
                provider_id = descriptor.provider_id
                if (
                    (reasoning_mode or (conversation.reasoning_mode if model_id else None)) is not None
                    and descriptor.reasoning_levels
                    and (reasoning_mode or conversation.reasoning_mode) not in descriptor.reasoning_levels
                ):
                    raise SessionApiError(
                        400,
                        "reasoning_mode_not_supported",
                        f"模型 {descriptor.model_id!r} 不支持当前推理强度，请先选择兼容的档位。",
                        detail={
                            "modelId": descriptor.model_id,
                            "available": list(descriptor.reasoning_levels),
                        },
                    )

        # 批次二十六第 5 件⑤：快照之外，还得**真的告诉引擎**。
        #
        # 此前这条端点只写 Kaus 自己的快照：能力表说这台引擎支持会话级模型，
        # 界面于是给了下拉，可 agent 那边一无所知，下一轮仍按它自己的默认模型跑
        # ——「改了没生效」正是这么来的。有下发面（``set_conversation_model``）
        # 且此刻有活跃 Runtime 时就发一次；发不出去就**不写快照**，让 400 与事实
        # 一致：这次没改成。
        #
        # 没有活跃 Runtime 时只存快照，不算失败——下一次 ``session/new`` 会带上
        # 它。这不是偷懒：一条没起来的会话本来就没有「当前模型」可改。
        updated = conversation.snapshot_model(
            model_id=model_id,
            provider_id=provider_id if model_id is not None else None,
            reasoning_mode=reasoning_mode,
        )
        if approval_mode:
            updated = updated.evolve(approval_mode=approval_mode)
        if execution_mode:
            updated = updated.evolve(execution_mode=execution_mode)
        if conversation.preferred_surface != "card":
            raise SessionApiError(409, "external_settings", "请先返回站内对话再修改设置。")
        applied_to_runtime = False
        apply_model = getattr(driver, "set_conversation_model", None)
        apply_selection = getattr(driver, "set_conversation_selection", None)
        handle = host.handle_for(conversation.id)
        if handle is not None and (callable(apply_selection) or (model_id is not None and callable(apply_model))):
            try:
                if callable(apply_selection):
                    applied_selection = await apply_selection(handle, updated)
                    if isinstance(applied_selection, Conversation):
                        updated = updated.evolve(reasoning_mode=applied_selection.reasoning_mode)
                else:
                    await apply_model(handle, model_id)
            except TurnAlreadyRunningError as exc:
                raise SessionApiError(409, "turn_already_running", "上一轮尚未结束，请结束后再切换模型。") from exc
            except UnsupportedCapabilityError as exc:
                # 能力表说支持、下发面说不支持：以实测为准，但这不是 500。
                if approval_mode or reasoning_mode or execution_mode:
                    raise SessionApiError(501, "conversation_setting_unsupported", str(exc)) from None
                raise _unsupported_model_switch() from None
            except ModelRejectedError as exc:
                raise SessionApiError(
                    400,
                    "model_rejected",
                    f"设置未生效：{exc.reason or exc}",
                    detail={
                        "modelId": model_id,
                        "bindingId": binding.id,
                        "backendId": binding.backend_id,
                        # agent 自己那句话，原样给——用户据它换一个模型。
                        "agentReason": exc.reason,
                    },
                ) from exc
            applied_to_runtime = True

        saved = await repositories.conversations.save(updated)
        # 活跃 runtime 手上那份也要换，否则这一轮还按旧快照发（AD-12 的反面）。
        host.update_conversation(saved)
        return {
            "conversation": conversation_to_wire(
                saved, run_state=await host.run_state_of(saved)
            ),
            # 这次改动到底有没有下发给正在跑的那条 Runtime。false = 只存了快照，
            # 下一次 `session/new` 之后才生效——界面据此决定要不要说一句。
            "appliedToRuntime": applied_to_runtime,
        }

    # --- 发消息 ------------------------------------------------------------- #

    async def _conversation_workspace(conversation: Conversation) -> str | None:
        handle = host.handle_for(conversation.id)
        if handle is not None:
            value = handle.metadata.get("workspaceRoot")
            return value if isinstance(value, str) and value else None
        # A project may have changed directories since this conversation ran.
        # Only a runtime-observed snapshot can authorize historical file access.
        events = await host.event_store.replay(conversation.id)
        for envelope in reversed(events):
            event = envelope.event
            if event.type == "extension.event" and event.namespace == "kaus" and event.name == "runtime.workspace":
                value = event.data.get("workspaceRoot") if isinstance(event.data, dict) else None
                return value if isinstance(value, str) and value else None
        return None

    async def upload_attachment(conversation_id, request):
        await _load_conversation(conversation_id)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_UPLOAD_JSON_BYTES:
                raise SessionApiError(413, "attachment_too_large", "单个附件不能超过 10 MB。")
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise SessionApiError(400, "invalid_attachment", "附件请求需要有效的 JSON。") from None
        if not isinstance(body, dict) or set(body) - {"name", "mimeType", "data"}:
            raise SessionApiError(400, "invalid_attachment", "附件请求字段无效。")
        attachment = files.upload(conversation_id, name=body.get("name"), data=body.get("data"), mime_type=body.get("mimeType"))
        return JSONResponse(status_code=201, content=attachment.model_dump(mode="json", by_alias=True, exclude_none=True))

    upload_attachment.__annotations__ = {"conversation_id": str, "request": Request, "return": Any}
    upload_attachment = _endpoint(upload_attachment)

    @_endpoint
    async def get_file(conversation_id: str, path: str = Query(..., max_length=4096), download: bool = False) -> Any:
        conversation = await _load_conversation(conversation_id)
        file = files.read(conversation_id, await _conversation_workspace(conversation), path)
        disposition = "attachment" if download or not (file.mime_type.startswith("image/") or file.mime_type in {"text/plain", "application/pdf"}) else "inline"
        return Response(
            file.content, media_type=file.mime_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file.name, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'; form-action 'none'; frame-ancestors 'self'",
                "Cache-Control": "no-store",
            },
        )

    @_endpoint
    async def send_message(
        conversation_id: str, body: SendMessageBody = Body(...)
    ) -> Any:
        conversation = await _load_conversation(conversation_id)
        if conversation.archived_at:
            raise SessionApiError(409, "conversation_archived", "请先恢复此对话。")
        if not body.text.strip() and not body.attachments:
            raise SessionApiError(400, "empty_message", "请输入消息或添加附件。")
        prepared = files.prepare(conversation_id, await _conversation_workspace(conversation), body.attachments)
        message = MessageInput(text=body.text, attachments=prepared, metadata=dict(body.metadata), client_ref=(body.client_ref or "").strip() or None)
        # 批次十一第 2 件：Runtime 起不来 = **一个字都没发出去**。这类失败要顺带
        # 告诉前端「这条会话至今是空的」，它才敢紧接着调 DELETE 把空壳收掉。
        # 起来之后再失败就不带这个标记了——那时用户那句话已经在时间线上。
        try:
            handle = await _ensure_runtime(conversation)
        except SessionApiError as exc:
            exc.extra.update(await _empty_conversation_extra(conversation))
            raise
        except AuthRequiredError as exc:
            # AD-157：引擎说「你还没登录」。这不是 500，也不是「引擎没能起来」——
            # 用户有一条明确的路可走（去终端登一次），所以 400 + 稳定 code +
            # 修法，并且**在时间线上留一条 run.failed**：真机上这一句失败得毫无
            # 痕迹，页面只写「历史没有到达」，用户根本不知道发生过什么。
            # 先算「这条会话是不是白建的」**再**合成事件：我们自己补的那条
            # run.failed 不该让「删了也不丢东西」变成假。
            extra = await _empty_conversation_extra(conversation)
            await _emit_send_failed(conversation, exc)
            raise _auth_required(exc, extra) from exc
        except AgentSpawnError as exc:
            # AD-159：引擎的**进程**没能拉起来。与「网关不可达」不是同一件事，
            # 所以不共用 `runtime_start_failed` 那个码：用户对这一条有一个两秒钟
            # 的动作（把同一条命令贴进终端跑一次），前端要据 code 把那句修法显示
            # 出来。仍然是 503（这台机器上的引擎坏了），仍然带「这条会话是空的」。
            raise _spawn_failed(exc, await _empty_conversation_extra(conversation)) from exc
        except Exception as exc:  # noqa: BLE001 - 网关不可达之类必须是显式状态
            # 走查 F5：这条错误此前只说「坏了」，没说「怎么修」。Driver 认得出
            # 根因时（网关没在跑 / key 不匹配）会在 `DriverError.failure` 上带一份
            # 人话 + 修法，这里原样接进 `detail`；认不出来才退回类型名 + 原文。
            failure = getattr(exc, "failure", None)
            if isinstance(failure, FailureHint):
                message = f"引擎没能起来，这条消息一个字都没发出去：{failure.message}"
                detail = failure.to_detail()
            else:
                message = (
                    "引擎没能起来，这条消息一个字都没发出去："
                    f"{type(exc).__name__}: {exc}"
                )
                detail = {}
            raise SessionApiError(
                503,
                "runtime_start_failed",
                message,
                detail=detail or None,
                **await _empty_conversation_extra(conversation),
            ) from exc
        # 先挂订阅再发：这样「发出去到 run.started」之间没有窗口期。
        if prepared:
            binding = await repositories.bindings.get(conversation.agent_binding_id)
            driver = registry.get(binding.backend_id)
            capabilities = await driver.get_capabilities()
            support = capabilities.card.attachments.value
            if support not in {"files", "images"} or (support == "images" and any(not (a.mime_type or "").startswith("image/") for a in prepared)):
                raise SessionApiError(400, "attachments_unsupported", "当前引擎尚未声明支持这些附件；请更换引擎或移除附件后发送。")
        after = await host.event_store.latest_sequence(conversation_id)
        subscription = host.subscribe(conversation_id, after_sequence=after)
        try:
            try:
                await host.send_message(conversation_id, message)
                run_id = await _first_run_id(subscription, timeout=run_id_timeout)
            except RuntimeNotActiveError as exc:
                raise SessionApiError(409, "runtime_not_active", str(exc)) from exc
            except UnsupportedCapabilityError as exc:
                # 批次十七第 3 件：Driver 说得出「不支持哪一项」时按那个 code 回，
                # 而不是一律 `unsupported_capability`——前端要按 code 分支决定
                # 是「改 Binding 默认」还是「这台引擎没这个能力」。
                raise _unsupported(exc) from exc
            except TurnAlreadyRunningError as exc:
                # 批次二十七第 2 件：上一轮还在跑不是**我们**坏了，是一个可等待的
                # 状态。此前它是一个没人接的 `DriverError`，于是从这条端点漏成 500
                # ——真机 C3 的 `HTTP 500` 就有这一份。
                raise SessionApiError(
                    409, "turn_already_running", _driver_message(exc), detail=_hint(exc)
                ) from exc
            except DriverError as exc:
                # 引擎明确拒绝了这一句（网关回了非 2xx）。它的状态码是**它的**，不是
                # 我们的：一律 502，并把网关自己那句人话带上去，不把 `HTTP 500` 原样
                # 上 wire。
                raise SessionApiError(
                    502, "message_rejected", _driver_message(exc), detail=_hint(exc)
                ) from exc
        except SessionApiError as exc:
            # R6（批次三十七）：这一段的失败**都发生在用户那句话已经落库之后**，
            # 所以错误体要能被对回那条消息——把 AD-94 的对账编号原样放进
            # `detail.clientRef`。前端据它把本地那条置灰，而不必去猜是哪一句
            # （同一条会话上可能连发了好几句）。没给编号就不放这个键，不放 null。
            _attach_client_ref(exc, body.client_ref)
            raise
        finally:
            subscription.close()
        return JSONResponse(
            status_code=202,
            content={
                "conversationId": conversation_id,
                "runtimeId": handle.runtime_id,
                "runId": run_id,
                # N §13.1：等不到就说等不到，不合成一个假的 runId。
                "runIdPending": run_id is None,
                "acceptedAfterSequence": after,
            },
        )

    # --- 事件流（SSE） ------------------------------------------------------ #

    @_endpoint
    async def stream_events(
        conversation_id: str, after: int | None = Query(None, ge=0)
    ) -> Any:
        conversation = await _load_conversation(conversation_id)
        # AD-136 的第三处：附着事件流时先收敛一次失配，合成的 `run.failed` 因此
        # 会**排在这次订阅之前**入库，客户端带 `?after=` 重连时照样重放得到。
        await host.reconcile_conversation(conversation)
        # AD-143：同理，重建也必须发生在 subscribe 之前——晚一步，这次不带
        # `after` 的全量重放就正好错过它，页面还是空的。
        # 只在**首次附着**（不带 `?after=`）时做：带游标的是断线续传，那时页面
        # 手上已经有历史了，再补一遍等于把整条会话重放两次。
        if after is None:
            await host.restore_from_native_history(conversation)

        async def frames():
            subscription = host.subscribe(conversation_id, after_sequence=after)
            try:
                # 批次二十八：先把重放那一批一次性放完，再推 2KB 填充注释，
                # 然后才开始等——隧道下「重放完了就静默」正是页面看起来空的
                # 那一刻（真机：后端重放 50+ 条，页面仍显示历史没到达）。
                for envelope in await subscription.drain_replay():
                    yield sse_data_line(
                        envelope_to_wire(envelope), serializer=_dumps
                    )
                yield SSE_PADDING_FRAME
                while True:
                    envelope = await _next_or_none(
                        subscription, timeout=keepalive_interval
                    )
                    if envelope is _STREAM_END:
                        return
                    if envelope is _STREAM_IDLE:
                        # AD-62：Backend 不发心跳，服务端自己发注释帧。
                        yield KEEPALIVE_FRAME
                        continue
                    yield sse_data_line(
                        envelope_to_wire(envelope), serializer=_dumps
                    )
            finally:
                # AD-61：客户端断开只关订阅，Runtime 照跑；恢复靠 ?after= 重放。
                subscription.close()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            # 反向代理 / 隧道下 SSE 最常见的坏法就是被缓冲住（批次二十八）。
            headers=dict(SSE_HEADERS),
        )

    @_endpoint
    async def events_snapshot(
        conversation_id: str, since: int | None = Query(None, ge=0)
    ) -> Any:
        """``GET /api/conversations/{id}/events/snapshot?since=<sequence>``。

        **和 SSE 同一批信封，只是不用长连接**（批次三十第 1 件）。上面那条
        ``stream_events`` 的前两步（收敛失配、必要时从原生历史重建）在这里一字不差
        地重做一遍，然后用同一个 :class:`EventSubscription` 的 ``drain_replay()``
        取重放批——两条路径出来的事件必须是同一份，否则「换成轮询」就变成了「换一
        个会漏事件的模式」。

        ``since`` 是增量游标，语义与 SSE 的 ``?after=`` 完全一致（开区间）。
        ``lastSequence`` 是这一批的最后一条，客户端下一次拿它当 ``since``。
        """
        conversation = await _load_conversation(conversation_id)
        await host.reconcile_conversation(conversation)
        # 与 SSE 同一条口径：只有**首次**（不带游标）才重建，带游标的是续传。
        if since is None:
            await host.restore_from_native_history(conversation)
        subscription = host.subscribe(conversation_id, after_sequence=since)
        try:
            envelopes = await subscription.drain_replay()
        finally:
            # 订阅只为借这一次重放；实时推送对轮询没有意义。
            subscription.close()
        page = envelopes[:SNAPSHOT_MAX_EVENTS]
        if page:
            last_sequence = page[-1].sequence
        elif since is not None:
            last_sequence = since
        else:
            last_sequence = await host.event_store.latest_sequence(conversation_id) or 0
        return {
            "conversationId": conversation_id,
            "events": [envelope_to_wire(envelope) for envelope in page],
            "lastSequence": last_sequence,
            # 截断了就说截断了：客户端据此立刻再取一次，而不是等下一个轮询周期。
            "truncated": len(envelopes) > len(page),
            "runState": await host.run_state_of(conversation),
        }

    # --- 中断 / 停止 / 答交互 ----------------------------------------------- #

    @_endpoint
    async def interrupt(conversation_id: str, body: InterruptBody | None = Body(None)) -> Any:
        """中断当前一轮；**没有活跃 runtime 也不算失败**（批次十七第 2 件）。

        走查 ② 的原文是「Stop failed: 该 Conversation 当前没有活跃 runtime」——
        用户点停止，看到的是一句内部报错，而页面仍旧停在运行中。可这一刻的事实
        很朴素：没有在跑的东西可停，而页面之所以还显示运行中，是因为那一轮丢了
        终态。所以这里回 200 并顺手做一次自愈（AD-136 的第三处），把时间线收敛掉
        ——「没什么可停的」不是错误，是一个如实的答案。
        """
        conversation = await _load_conversation(conversation_id)
        if not host.is_active(conversation_id):
            synthesized = await host.reconcile_conversation(conversation)
            return {
                "conversationId": conversation_id,
                "interrupted": False,
                "active": False,
                # 这条路径**一定**跑过一次一致性收敛；有没有真的补出一条终态
                # 事件看 `synthesizedRunFailed`（本来就已经是终态时为 false）。
                "reconciled": True,
                "synthesizedRunFailed": synthesized,
            }
        try:
            await host.interrupt(conversation_id, expected_run_id=body.expected_run_id if body else None)
        except StaleRunError as exc:
            raise SessionApiError(409, "stale_run", str(exc)) from exc
        except RuntimeNotActiveError as exc:
            # 上面查过一次仍可能在这两次之间被回收；同一口径按 200 处理。
            synthesized = await host.reconcile_conversation(conversation)
            del exc
            return {
                "conversationId": conversation_id,
                "interrupted": False,
                "active": False,
                "reconciled": True,
                "synthesizedRunFailed": synthesized,
            }
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        return {
            "conversationId": conversation_id,
            "interrupted": True,
            "active": host.is_active(conversation_id),
            "reconciled": False,
        }

    @_endpoint
    async def resolve_interaction(
        conversation_id: str,
        interaction_id: str,
        body: InteractionResponseBody = Body(...),
    ) -> Any:
        await _load_conversation(conversation_id)
        try:
            response = body.to_response()
        except Exception as exc:  # noqa: BLE001 - 交互种类是封闭集合
            raise SessionApiError(
                400, "invalid_interaction_response", f"交互响应不合法：{exc}"
            ) from exc
        try:
            await host.resolve_interaction(conversation_id, interaction_id, response)
        except RuntimeNotActiveError as exc:
            raise SessionApiError(409, "runtime_not_active", str(exc)) from exc
        except InteractionNotFoundError as exc:
            raise _not_found("interaction_not_found", str(exc)) from exc
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        return {
            "conversationId": conversation_id,
            "interactionId": interaction_id,
            "resolved": True,
        }

    @_endpoint
    async def stop_runtime(conversation_id: str) -> Any:
        await _load_conversation(conversation_id)
        was_active = host.is_active(conversation_id)
        # v1.0 §9.4：停 Runtime 保留 Conversation 与原生 Session 映射。
        await host.stop_runtime(conversation_id)
        return {
            "conversationId": conversation_id,
            "stopped": was_active,
            "active": host.is_active(conversation_id),
        }

    # --- 原生历史 ----------------------------------------------------------- #

    @_endpoint
    async def get_history(conversation_id: str) -> Any:
        conversation = await _load_conversation(conversation_id)
        if conversation.native_session_id is None:
            raise SessionApiError(
                409,
                "native_session_not_bound",
                "这条 Conversation 还没有绑定原生 Session（N §9 允许懒创建）",
            )
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise _not_found(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(binding.backend_id) from exc
        try:
            history = await driver.load_native_history(
                binding, conversation.native_session_id
            )
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        except (RuntimeNotFoundError, KeyError) as exc:
            raise _not_found("native_session_not_found", str(exc)) from exc
        return native_history_to_wire(history)

    # --- Surface 交接（Phase 4 / v1.0 §8.6–8.7） ----------------------------- #

    def _require_coordinator() -> SurfaceCoordinator:
        if surface_coordinator is None:  # pragma: no cover - 端点根本不会被注册
            raise SessionApiError(
                501,
                "surface_handoff_unavailable",
                "本次装配没有 Terminal Launcher，站内外交接不可用",
            )
        return surface_coordinator

    def _surface_error(exc: SurfaceError) -> SessionApiError:
        """把编排层的错误翻成 HTTP。code 原样保留——前端按 code 分支。"""
        if isinstance(exc, SurfaceConflictError):
            status = 409
        elif isinstance(exc, SurfaceUnsupportedError):
            status = 501
        else:
            status = 404 if exc.code.endswith("_not_found") else 400
        return SessionApiError(
            status, exc.code, str(exc), detail=exc.detail or None
        )

    @_endpoint
    async def open_external_surface(
        conversation_id: str, force: bool = Query(False)
    ) -> Any:
        """``POST /surface/external``：把写权交给外部终端。

        ``?force=1`` = 强制接管（v1.0 §8.6 要求二次确认并说明风险——确认在前端，
        风险在这条 query 上）。
        """
        coordinator = _require_coordinator()
        conversation = await _load_conversation(conversation_id)
        try:
            handoff = await coordinator.to_external(conversation, force=force)
        except SurfaceError as exc:
            raise _surface_error(exc) from exc
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(
                (await repositories.bindings.get(conversation.agent_binding_id)).backend_id
            ) from exc
        return {
            "conversationId": conversation_id,
            "surface": "external-cli" if handoff.launched else "card",
            "launch": launch_to_wire(handoff.launch),
            "lease": (await coordinator.describe(handoff.conversation))["lease"],
            "commandSummary": handoff.command_summary,
            # 降级时这是唯一能用的东西：让用户自己粘。
            "launched": handoff.launched,
            "reason": handoff.reason,
        }

    @_endpoint
    async def return_to_card_surface(
        conversation_id: str, force: bool = Query(False)
    ) -> Any:
        """``POST /surface/card``：收回站内写权并校准原生历史。"""
        coordinator = _require_coordinator()
        conversation = await _load_conversation(conversation_id)
        try:
            handoff = await coordinator.to_card(conversation, force=force)
        except SurfaceError as exc:
            raise _surface_error(exc) from exc
        return {
            "conversationId": conversation_id,
            "surface": "card",
            "reconciled": handoff.reconciled,
            # 批次十五第 4 件：这里回**完整的** entries，与
            # `kaus/history.reconciled` 事件的 data 逐字同形。原来只回一个条数，
            # 前端要拿到校准结果就只能等 SSE——而收回站内这一刻恰恰是 SSE 最容易
            # 断的时候（runtime 刚停）。同一份东西走两条路送达，谁先到用谁。
            "entries": [dict(entry) for entry in handoff.entries],
            "entryCount": len(handoff.entries),
            "lastEntryId": (
                handoff.entries[-1]["entryId"] if handoff.entries else None
            ),
            "complete": handoff.complete,
            "gaps": list(handoff.gaps),
        }

    @_endpoint
    async def get_surface(conversation_id: str) -> Any:
        """``GET /surface``：现在谁在写、上一次终端启动是什么。

        批次十五第 3 件：**没装 Terminal Launcher 时也回 200**，形状是
        ``{surface:"card", supported:false, lease:null, launch:null}``。
        原来这条路在这种装配下压根没注册，前端只能把「这台机器没有终端交接」
        当成一次请求失败来处理，会话页因此挂着一个报错。而事实一句话就说得清：
        这里只有站内卡片，交接那条路不存在——``supported`` 就是这句话，
        ``surface`` 仍然如实回 ``card``（那正是此刻在写的界面）。
        """
        conversation = await _load_conversation(conversation_id)
        if surface_coordinator is None:
            return {
                "conversationId": conversation.id,
                "surface": "card",
                "supported": False,
                "lease": None,
                "launch": None,
            }
        return await surface_coordinator.describe(conversation)

    @_endpoint
    async def list_launches(conversation_id: str) -> Any:
        """``GET /launches``：终端启动历史（「上次在终端做了什么」）。"""
        coordinator = _require_coordinator()
        await _load_conversation(conversation_id)
        launches = await coordinator.list_launches(conversation_id)
        return {
            "conversationId": conversation_id,
            "launches": [launch_to_wire(launch) for launch in launches],
            "count": len(launches),
        }

    # --- 能力写端点（Phase 5 第一步 / AD-144） ------------------------------- #

    async def _known_capability_type(capability_type: str) -> str:
        """校验 ``type`` 落在**已知**类型集合内，并回归一化后的写法。

        两档，对应 R-01 的两种形态：

        - 通用能力：必须在 :data:`GENERIC_CAPABILITY_TYPES` 里。它不是「随便什么
          标识符都行」的开放集合——写端点是**产生**赋值行的地方，把打错的
          ``skils`` 收下来，只会在 Resolver 输出里长出一条谁也认不出的能力；
        - backend-scoped（``<backend-key>:<name>``）：backend 必须是这台机器上
          **有记录**的那些。公共层不硬编码任何 backend 名字（N §3），所以「已知」
          的定义就是领域库里的 ``backends`` 行。
        """
        try:
            ref = parse_capability_type(capability_type)
        except CapabilityTypeError as exc:
            raise SessionApiError(
                400, "unknown_capability_type", str(exc)
            ) from exc
        if ref.backend_key is None:
            if ref.name not in GENERIC_CAPABILITY_TYPES:
                raise SessionApiError(
                    400,
                    "unknown_capability_type",
                    f"未知的通用能力类型：{ref.name!r}",
                    detail={"known": sorted(GENERIC_CAPABILITY_TYPES)},
                )
            return ref.name
        backend_id = normalize_backend_key_id(ref.backend_key)
        if await repositories.backends.get(backend_id) is None:
            raise SessionApiError(
                400,
                "unknown_capability_type",
                f"能力类型里的 backend 不存在：{ref.backend_key!r}",
                detail={"backendId": backend_id},
            )
        return str(ref)

    async def _effective_entry(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        """写完之后这条能力**最终**是什么——走与读端点同一个 Resolver。

        写端点回「写进去的那一行」是不够的：AD-45 的禁止对子树生效、祖先的赋值
        是否被这次覆盖、这次删除之后回到哪一层继承——这些只有把整条祖先链算一遍
        才知道。算两遍不同的账迟早会给出两个答案，所以这里**不另写**一套。
        """
        ancestry = [p.id for p in await repositories.projects.ancestry(project_id)]
        effective = await resolve_effective_for_project(repositories, project_id)
        return {
            "projectId": project_id,
            "capabilityType": capability_type,
            "capabilityId": capability_id,
            "ancestry": ancestry,
            # 没有这一条时是 null：既没赋值也没继承来，是一个如实的「没有」。
            "entry": effective_capability_entry_to_wire(
                effective,
                capability_type=capability_type,
                capability_id=capability_id,
            ),
        }

    async def _load_project(project_id: str) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        return project

    async def _existing_assignment(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        for assignment in await repositories.capabilities.list_for_project(project_id):
            if (
                assignment.capability_type == capability_type
                and assignment.capability_id == capability_id
            ):
                return assignment
        return None

    @_endpoint
    async def put_capability(
        project_id: str,
        capability_type: str,
        capability_id: str,
        body: PutCapabilityBody = Body(...),
    ) -> Any:
        """``PUT /projects/{id}/capabilities/{type}/{capId}``：赋值 / 禁止 / 解除。

        一个端点三件事，因为它们写的是**同一行**（v1.0 §5.2：同一
        ``(project, type, capabilityId)`` 只应有一条赋值）：

        - ``blocked=true`` → 写一条 ``assignment_mode="block"`` 的行。AD-45：
          禁止对**子树**生效，所以它不是「本节点不显示」而是「从这里往下都没有」；
          block 行不带 config（领域模型直接拒绝），因此同时给 ``value`` 是矛盾的
          请求，回 400 而不是悄悄丢掉其中一半；
        - ``blocked=false`` → 解除禁止。这里只删**本项目层**那条 block 行；解除
          之后这条能力回到继承（可能仍被更上面的祖先挡着——那是另一个节点的事，
          不该由这个端点越过它去改）；
        - 给了 ``value`` → 写本项目层的赋值（``assignment_mode="local"``）。

        Projector 不物化（AD-07）：这里只改领域库那一行，不去动任何 Backend 的
        原生配置——落盘是 Phase 5 后续的事。
        """
        project = await _load_project(project_id)
        normalized_type = await _known_capability_type(capability_type)
        if not capability_id.strip():
            raise SessionApiError(
                400, "invalid_capability_id", "capabilityId 不能为空"
            )
        if body.blocked is None and body.value is None:
            raise SessionApiError(
                400,
                "empty_capability_patch",
                "请求里没有任何要写的项（value / blocked 至少给一个）",
            )
        if body.blocked and body.value is not None:
            raise SessionApiError(
                400,
                "blocked_with_value",
                "禁止（blocked=true）与赋值（value）是互斥的：block 行不携带配置"
                "（v1.0 §5.2）。要改值就先解除禁止。",
            )
        existing = await _existing_assignment(
            project.id, normalized_type, capability_id
        )
        if body.blocked is False:
            # 解除：本项目层有 block 行就删掉它；本来就没有就是无操作，
            # 不报错——「解除一个没有的禁止」的结果与「解除成功」一模一样。
            if existing is not None and existing.assignment_mode == "block":
                await repositories.capabilities.delete(existing.id)
            return await _effective_entry(project.id, normalized_type, capability_id)

        mode = "block" if body.blocked else "local"
        config = dict(body.value or {}) if mode == "local" else {}
        if existing is not None:
            await repositories.capabilities.delete(existing.id)
        await repositories.capabilities.save(
            ProjectCapability.create(
                project_id=project.id,
                capability_type=normalized_type,
                capability_id=capability_id,
                assignment_mode=mode,  # type: ignore[arg-type]
                config=config,
            )
        )
        return await _effective_entry(project.id, normalized_type, capability_id)

    @_endpoint
    async def delete_capability(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        """删掉**本项目层**的那条赋值 → 这条能力回到继承。

        与 ``blocked=false`` 的区别只在删的是哪一种行：这里删的是任意一条本地行
        （local 或 block），语义是「这个节点不再对它有意见」。祖先写的东西一行
        不动——那是别人的节点。
        """
        project = await _load_project(project_id)
        normalized_type = await _known_capability_type(capability_type)
        existing = await _existing_assignment(
            project.id, normalized_type, capability_id
        )
        if existing is not None:
            await repositories.capabilities.delete(existing.id)
        payload = await _effective_entry(project.id, normalized_type, capability_id)
        payload["deleted"] = existing is not None
        return payload

    @_endpoint
    async def capabilities_meta(project_id: str) -> Any:
        """``GET /projects/{id}/capabilities/_meta``（批次二十第 2 件）。

        前端要回答的问题只有一个：**这个后端认不认能力写端点**（AD-144 之前的版本
        不认）。此前它只能拿 ``PUT`` 一条不存在的能力去探，而 405 与 404 在真机上
        既可能是「没这条路由」，也可能是 CORS 中间件先答了——探不准，还留下一次
        写请求。这里给一条只读的自述：路由挂上了，``writable`` 就是 true。

        ``_meta`` 与 ``{capability_type}`` 走的是同一层路径，因此这条路由**必须**
        排在参数化路由之前注册（下面的注册顺序已经保证）。能力类型不允许以下划线
        开头，不会撞名。
        """
        project = await _load_project(project_id)
        return {
            "projectId": project.id,
            "writable": True,
            "methods": ["PUT", "DELETE"],
            "path": "/api/projects/{project_id}/capabilities/{capability_type}/{capability_id}",
        }

    # --- Model Catalog ------------------------------------------------------ #

    @_endpoint
    async def get_models(backend_id: str, binding: str | None = Query(None)) -> Any:
        normalized = normalize_backend_key_id(backend_id)
        if not binding:
            raise SessionApiError(
                400,
                "binding_required",
                "Model Catalog 按 Binding 取（N §10.1：每个 Binding 一份，互不污染）——"
                "请带上 ?binding=<binding_id>",
            )
        record = await repositories.bindings.get(binding)
        if record is None:
            raise _not_found("binding_not_found", f"未知 Binding：{binding}")
        if record.backend_id != normalized:
            raise SessionApiError(
                400,
                "binding_backend_mismatch",
                f"Binding {record.id} 属于 {record.backend_id}，不是 {normalized}",
            )
        try:
            driver = registry.get(normalized)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(normalized) from exc
        await register_binding_snapshot(driver, record, repositories.projects)
        try:
            catalog = await driver.get_model_catalog(record)
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        return model_catalog_to_wire(catalog)

    # --- 有效设置（批次十三第 1 件） ----------------------------------------- #

    @_endpoint
    async def get_effective_settings(binding_id: str) -> Any:
        """``GET /bindings/{id}/effective-settings``：工具栏那四栏显示什么、谁定的。

        为什么这条 GET 住在会话 router 而不是只读领域 router：它要问 Driver
        （引擎自己的配置 + Model Catalog），而 Driver Registry 只在这一层有。
        另一半理由与写端点相同——它要鉴权，只读领域 router 按 AD-72 不鉴权。

        **取不到就当没有。** Driver 报错、Backend 没注册、引擎没配置文件，都只是
        让解析链少一层来源，不是这条请求失败：工具栏总得渲染出来。
        """
        binding = await repositories.bindings.get(binding_id)
        if binding is None:
            raise _not_found("binding_not_found", f"未知 Binding：{binding_id}")
        project = await repositories.projects.get(binding.project_id)

        engine = None
        catalog = None
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError:
            driver = None
        if driver is not None:
            await register_binding_snapshot(driver, binding, repositories.projects)
            reader = getattr(driver, "read_engine_settings", None)
            if reader is not None:
                try:
                    engine = await reader(binding)
                except Exception:  # noqa: BLE001 - 少一层来源，不是这条请求失败
                    engine = None
            try:
                catalog = await driver.get_model_catalog(binding)
            except Exception:  # noqa: BLE001 - 同上
                catalog = None
        payload = effective_settings_to_wire(
            binding=binding, project=project, engine=engine, catalog=catalog
        )
        controls_reader = getattr(driver, "conversation_controls", None)
        payload["conversationControls"] = controls_reader(binding) if callable(controls_reader) else {"reasoning": False, "approvalModes": []}
        return payload

    # --- 取证（只在 DASH_DEBUG=1 时挂载） ------------------------------------ #

    @_endpoint
    async def debug_last_history(
        backend_id: str, conversation: str | None = Query(None)
    ) -> Any:
        """``GET /backends/{id}/debug/last-history?conversation=<id>``（批次十七第 4 件）。

        回答的是一个很具体的问题：**这台真机的 ``GET /api/sessions/{id}/messages``
        响应到底长什么样、``tool_calls`` 在哪一层**。工具输出补不出来时（走查 ①
        的 CALL 区仍是 ``{"preview": …}``），这是唯一不需要用户去碰密钥的取证方式
        ——让他去敲一条带 ``Authorization: Bearer $(cat …)`` 的 curl，等于让他把
        key 打在终端里。

        输出**只有形状**：键名 + 值类型 + 截断 80 字，像凭据的键名连截断值都不给
        （见 :func:`~app.api.session_views.debug_shape`）。

        没有捕获到响应时如实回 ``captured: false`` + 下一步该做什么，不编一份
        假的形状出来（N §13.1）。
        """
        normalized = normalize_backend_key_id(backend_id)
        try:
            driver = registry.get(normalized)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(normalized) from exc
        reader = getattr(driver, "debug_last_history_payload", None)
        if reader is None:
            raise SessionApiError(
                501,
                "debug_unavailable",
                f"{normalized} 的 Driver 没有这条取证面",
            )
        native_session_id: str | None = None
        if conversation:
            record = await repositories.conversations.get(conversation)
            if record is None:
                raise _not_found(
                    "conversation_not_found", f"未知 Conversation：{conversation}"
                )
            native_session_id = record.native_session_id
        payload = reader(native_session_id)
        return {
            "backendId": normalized,
            "conversationId": conversation,
            "nativeSessionId": native_session_id,
            "captured": payload is not None,
            "shape": debug_shape(payload) if payload is not None else None,
            "hint": (
                None
                if payload is not None
                else "还没有取到过原生历史：先在这条会话里发一条会用到工具的消息，"
                "等这一轮结束后再取一次"
            ),
        }

    @_endpoint
    async def debug_last_stream(
        backend_id: str, conversation: str | None = Query(None)
    ) -> Any:
        """``GET /backends/{id}/debug/last-stream?conversation=<id>``（批次二十第 3 件）。

        回答的是 AD-146 那条真机阻断留下的问题：**这一轮网关到底往 SSE 上发了哪些
        事件、各几条**。事件缓冲里看到的是我们翻译之后的公共事件；这条端点看到的
        是翻译之前的原始类型序列，两边一对，「是网关没发」还是「我们没收/没翻」
        一眼就分得开。

        输出**只有类型名与计数**：正文片段、工具 preview、入参、输出一个字都不带
        ——那是 Driver 侧 ``debug_last_stream`` 的约定，本层只负责把它原样转出去。
        与 ``last-history`` 一样只在 ``DASH_DEBUG=1`` 时挂载，且照旧要鉴权。

        没有记录时如实回 ``captured: false`` + 下一步，不编（N §13.1）。
        """
        normalized = normalize_backend_key_id(backend_id)
        try:
            driver = registry.get(normalized)
        except DriverNotRegisteredError as exc:
            raise _driver_not_registered(normalized) from exc
        reader = getattr(driver, "debug_last_stream", None)
        if reader is None:
            raise SessionApiError(
                501,
                "debug_unavailable",
                f"{normalized} 的 Driver 没有这条取证面",
            )
        if not conversation:
            raise SessionApiError(
                400,
                "conversation_required",
                "这条端点按会话取证：请带上 ?conversation=<conversation id>",
            )
        record = await repositories.conversations.get(conversation)
        if record is None:
            raise _not_found(
                "conversation_not_found", f"未知 Conversation：{conversation}"
            )
        stream = reader(conversation)
        return {
            "backendId": normalized,
            "conversationId": conversation,
            "captured": stream is not None,
            "stream": dict(stream) if stream is not None else None,
            "hint": (
                None
                if stream is not None
                else "本进程还没有跑完过这条会话的回合：先发一条消息、等这一轮收尾，"
                "再取一次"
            ),
        }

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

    for path, endpoint, methods, name, dependencies in (
        (
            # 必须排在 `/conversations/{conversation_id}` **之前**：Starlette 按
            # 注册顺序匹配，而字面量路径与参数路径长度不同、不会互相吃掉，
            # 但保持「更具体的在前」是这张表一直以来的读法。
            "/conversations",
            list_recent_conversations,
            ["GET"],
            "session_list_recent_conversations",
            [_auth],
        ),
        (
            "/projects/{project_id}/conversations",
            list_conversations,
            ["GET"],
            "session_list_conversations",
            [_auth],
        ),
        (
            "/projects/{project_id}/conversations",
            create_conversation,
            ["POST"],
            "session_create_conversation",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}",
            get_conversation,
            ["GET"],
            "session_get_conversation",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/archive",
            archive_conversation,
            ["PATCH"],
            "session_archive_conversation",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}",
            delete_conversation,
            ["DELETE"],
            "session_delete_conversation",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}",
            patch_conversation,
            ["PATCH"],
            "session_patch_conversation",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/messages",
            send_message,
            ["POST"],
            "session_send_message",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/attachments", upload_attachment,
            ["POST"], "session_upload_attachment", [_auth],
        ),
        (
            "/conversations/{conversation_id}/files", get_file,
            ["GET"], "session_get_file", [_auth],
        ),
        (
            # 批次三十：字面量段先注册先赢（同 `capabilities/_meta` 的做法）。
            # 它走普通 Bearer 鉴权：不是 EventSource，没有「带不了头」的问题。
            "/conversations/{conversation_id}/events/snapshot",
            events_snapshot,
            ["GET"],
            "session_events_snapshot",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/events",
            stream_events,
            ["GET"],
            "session_stream_events",
            # SSE 是唯一接受 ``?token=`` 的端点（``EventSource`` 带不了头）。
            [_auth_sse],
        ),
        (
            "/conversations/{conversation_id}/interrupt",
            interrupt,
            ["POST"],
            "session_interrupt",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/interactions/{interaction_id}",
            resolve_interaction,
            ["POST"],
            "session_resolve_interaction",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/stop",
            stop_runtime,
            ["POST"],
            "session_stop",
            [_auth],
        ),
        (
            "/conversations/{conversation_id}/history",
            get_history,
            ["GET"],
            "session_history",
            [_auth],
        ),
        (
            # 批次十五第 3 件：没有 Terminal Launcher 也注册它，回 supported:false。
            "/conversations/{conversation_id}/surface",
            get_surface,
            ["GET"],
            "session_surface",
            [_auth],
        ),
        (
            # 必须排在 `{capability_type}` 之前：同一层的字面量段先注册先匹配。
            "/projects/{project_id}/capabilities/_meta",
            capabilities_meta,
            ["GET"],
            "session_capabilities_meta",
            [_auth],
        ),
        (
            "/projects/{project_id}/capabilities/{capability_type}/{capability_id}",
            put_capability,
            ["PUT"],
            "session_put_capability",
            [_auth],
        ),
        (
            "/projects/{project_id}/capabilities/{capability_type}/{capability_id}",
            delete_capability,
            ["DELETE"],
            "session_delete_capability",
            [_auth],
        ),
        (
            "/backends/{backend_id}/models",
            get_models,
            ["GET"],
            "session_model_catalog",
            [_auth],
        ),
        (
            "/bindings/{binding_id}/effective-settings",
            get_effective_settings,
            ["GET"],
            "session_effective_settings",
            [_auth],
        ),
        (
            BOOTSTRAP_ROUTE_PATH,
            bootstrap_token,
            ["GET"],
            "session_auth_bootstrap",
            [],
        ),
    ):
        router.add_api_route(
            path, endpoint, methods=methods, name=name, dependencies=dependencies
        )
    if debug_endpoints:
        # 只在 `DASH_DEBUG=1` 时存在。鉴权照旧（`_auth`）：脱敏归脱敏，
        # 取证面也不该对未鉴权的请求开着。
        router.add_api_route(
            "/backends/{backend_id}/debug/last-history",
            debug_last_history,
            methods=["GET"],
            name="session_debug_last_history",
            dependencies=[_auth],
        )
        router.add_api_route(
            "/backends/{backend_id}/debug/last-stream",
            debug_last_stream,
            methods=["GET"],
            name="session_debug_last_stream",
            dependencies=[_auth],
        )
    if surface_coordinator is not None:
        for path, endpoint, methods, name, dependencies in (
            (
                # 字面量段排在 `/conversations/{id}` 之后没关系（路径长度不同），
                # 但 `/surface/external` 必须排在 `/surface` 之前的常规读法保持。
                "/conversations/{conversation_id}/surface/external",
                open_external_surface,
                ["POST"],
                "session_surface_external",
                [_auth],
            ),
            (
                "/conversations/{conversation_id}/surface/card",
                return_to_card_surface,
                ["POST"],
                "session_surface_card",
                [_auth],
            ),
            (
                "/conversations/{conversation_id}/launches",
                list_launches,
                ["GET"],
                "session_launches",
                [_auth],
            ),
        ):
            router.add_api_route(
                path, endpoint, methods=methods, name=name, dependencies=dependencies
            )
    return router


def _parse_timestamp(raw: str | None) -> datetime | None:
    """把 ``?updated_after=`` 解析成带时区的 ``datetime``。

    只接受 ISO-8601；结尾的 ``Z`` 手工换成 ``+00:00``（``fromisoformat`` 在
    3.11 才认 Z，而我们回给客户端的正是 Z 结尾的串，收不回来就太荒唐了）。
    不带时区的一律当 UTC——这个库里的时间戳全是 UTC，猜本地时区只会错。
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SessionApiError(
            400,
            "invalid_timestamp",
            f"updated_after 必须是 ISO-8601 时间戳，拿到 {raw!r}",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 订阅辅助
# --------------------------------------------------------------------------- #
#
# 批次二十七：这一组下沉到 :mod:`app.api.event_wait`，Group 的投递要走同一份
# 「先挂订阅再发、等本轮第一个 runId」。这里保留旧名字，本模块内部照旧用。


def _dumps(payload: Any) -> str:
    # 事件里有中文；SSE 是 UTF-8 传输，不必转义成 \\uXXXX。
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "DEFAULT_KEEPALIVE_INTERVAL",
    "DEFAULT_RUN_ID_TIMEOUT",
    "CreateConversationBody",
    "InteractionResponseBody",
    "SendMessageBody",
    "SessionApiError",
    "build_session_router",
]
