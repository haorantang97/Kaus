"""Forensic endpoints, mounted only with ``debug_endpoints`` (``DASH_DEBUG=1``).

They expose shapes and counts of what a Driver last saw, never payloads;
authentication applies as for every other session endpoint.
"""

from __future__ import annotations

from typing import Any

from app.api.session_views import debug_shape
from app.api.views import normalize_backend_key_id
from app.conversations.models import Conversation
from drivers.base import DriverNotRegisteredError
from app.api.session_routes.common import RouteSpec, SessionApiError, _not_found
from app.api.session_routes.context import SessionRouteContext


def build_debug_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Debug endpoints (shape-only forensics)."""
    from fastapi import Query

    repositories = ctx.repositories
    registry = ctx.registry

    @ctx.endpoint
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
            raise ctx.driver_not_registered(normalized) from exc
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

    @ctx.endpoint
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
            raise ctx.driver_not_registered(normalized) from exc
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

    return [
        RouteSpec('/backends/{backend_id}/debug/last-history', debug_last_history, ["GET"], 'session_debug_last_history', when="debug"),
        RouteSpec('/backends/{backend_id}/debug/last-stream', debug_last_stream, ["GET"], 'session_debug_last_stream', when="debug"),
    ]


__all__ = ["build_debug_routes"]
