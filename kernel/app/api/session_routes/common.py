"""Shared vocabulary of the session endpoint groups.

The error type with its stable codes, the request bodies, the route
description consumed by :func:`~app.api.session_router.build_session_router`
and the pure translations from Driver exceptions to
:class:`SessionApiError`. Nothing here needs the Session Host or the web
framework; the modules that register endpoints import from here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.api_errors import ApiError
from app.api.proxy_actions import ProxyActor
from app.conversations.configuration import ConversationConfig
from app.conversations.models import Conversation
from drivers.base import (
    AGENT_SPAWN_FAILED,
    AgentSpawnError,
    AuthRequiredError,
    FailureHint,
    InteractionResponse,
    MessageInput,
    UnsupportedCapabilityError,
    plain_text,
)
from runtime.surface_handoff import SurfaceConflictError, SurfaceError, SurfaceUnsupportedError


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


class MoveConversationBody(_Body):
    """``POST /conversations/{id}/move``: rescue an archived conversation into another Project."""

    project_id: str = Field(min_length=1, max_length=256)
    binding_id: str = Field(min_length=1, max_length=256)
    confirm_workspace_change: bool = False


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
    provider_id: str | None = None
    reasoning_mode: str | None = None
    approval_mode: str | None = None
    execution_mode: str | None = Field(default=None, max_length=256)
    config_options: ConversationConfig | None = None
    #: Set by the Kaus control MCP when a main agent changes this for the user.
    proxy: ProxyActor | None = None


class PutCapabilityBody(_Body):
    """``PUT /api/projects/{id}/capabilities/{type}/{capId}`` 的 body（AD-144）。

    两项都可选，但不能都不给：``value`` 是本项目层的赋值内容（一个 JSON 对象，
    公共层不解释里面的键——那是各能力类型自己的 schema），``blocked`` 是 AD-45
    的禁止开关（``true`` 写禁止、``false`` 解除）。
    """

    value: dict[str, Any] | None = None
    blocked: bool | None = None


class BookmarkBody(_Body):
    message_id: str = Field(min_length=1, max_length=256)
    saved: bool


class ViewedBody(_Body):
    """``PUT /conversations/{id}/viewed``：会话页看到的最后一个事件序号。"""

    sequence: int = Field(ge=0)
    #: 界面语言，系统通知的文案跟着它。
    locale: Literal["zh", "en"] | None = None


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
    #: Set by the Kaus control MCP: only an offered choice is accepted, and the
    #: answer is recorded in the timeline.
    proxy: ProxyActor | None = None

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


class RouteSpec(NamedTuple):
    """One endpoint to register: path, handler, methods and route name.

    ``auth`` picks the dependency: ``bearer`` (``Authorization: Bearer``),
    ``sse`` (also accepts ``?token=``) or ``none`` (the bootstrap endpoint).
    ``when`` says whether the route is always registered, only with a
    ``surface_coordinator`` or only with ``debug_endpoints``.
    """

    path: str
    endpoint: Callable[..., Any]
    methods: list[str]
    name: str
    auth: Literal["bearer", "sse", "none"] = "bearer"
    when: Literal["always", "coordinator", "debug"] = "always"


# --------------------------------------------------------------------------- #
# Driver 异常 → SessionApiError（纯函数）
# --------------------------------------------------------------------------- #


def driver_message(exc: BaseException) -> str:
    """Driver 异常 → 一句**纯文本人话**（批次二十七第 2 件）。

    认得出根因就用 :class:`~drivers.base.FailureHint` 的文案；认不出来才退回
    异常原文，且照样经 :func:`~drivers.base.plain_text` 剥掉类名与标记。
    """
    failure = getattr(exc, "failure", None)
    if isinstance(failure, FailureHint):
        return failure.message
    return plain_text(str(exc)) or "发送失败"


def attach_client_ref(exc: SessionApiError, client_ref: str | None) -> None:
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


def hint_detail(exc: BaseException) -> dict[str, Any] | None:
    failure = getattr(exc, "failure", None)
    return failure.to_detail() if isinstance(failure, FailureHint) else None


def auth_required_error(
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


def spawn_failed_error(
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


def start_failed_error(exc: BaseException, extra: dict[str, Any]) -> SessionApiError:
    """「引擎没能起来」→ 503 ``runtime_start_failed``，认得出根因就带修法。"""
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
    return SessionApiError(
        503, "runtime_start_failed", message, detail=detail or None, **extra
    )


def unsupported_error(exc: UnsupportedCapabilityError) -> SessionApiError:
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


def surface_error(exc: SurfaceError) -> SessionApiError:
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


def _dumps(payload: Any) -> str:
    # 事件里有中文；SSE 是 UTF-8 传输，不必转义成 \\uXXXX。
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ArchiveConversationBody",
    "BookmarkBody",
    "CreateConversationBody",
    "DEFAULT_KEEPALIVE_INTERVAL",
    "InteractionResponseBody",
    "InterruptBody",
    "MoveConversationBody",
    "PatchConversationBody",
    "PutCapabilityBody",
    "RouteSpec",
    "SNAPSHOT_MAX_EVENTS",
    "SendMessageBody",
    "SessionApiError",
    "attach_client_ref",
    "auth_required_error",
    "driver_message",
    "hint_detail",
    "spawn_failed_error",
    "start_failed_error",
    "surface_error",
    "unsupported_error",
]
