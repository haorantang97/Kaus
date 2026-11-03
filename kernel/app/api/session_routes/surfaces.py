"""Surface handoff: Card ⇄ external terminal (Phase 4 / v1.0 §8.6–8.7).

``GET /surface`` and the preview / focus endpoints are always registered
(without a coordinator they answer ``supported: false``); the handoff
endpoints exist only when the host has a Terminal Launcher.
"""

from __future__ import annotations

from typing import Any

from app.projects.retirement import require_open_project
from drivers.base import DriverNotRegisteredError
from runtime.surface_handoff import SurfaceCoordinator, SurfaceError, launch_to_wire
from app.api.session_routes.common import RouteSpec, SessionApiError, surface_error
from app.api.session_routes.context import SessionRouteContext


def build_surface_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Surface description and Card ⇄ external CLI handoff endpoints."""
    from fastapi import Body, Query

    repositories = ctx.repositories
    surface_coordinator = ctx.surface_coordinator

    def _require_coordinator() -> SurfaceCoordinator:
        if surface_coordinator is None:  # pragma: no cover - 端点根本不会被注册
            raise SessionApiError(
                501,
                "surface_handoff_unavailable",
                "本次装配没有 Terminal Launcher，站内外交接不可用",
            )
        return surface_coordinator

    @ctx.endpoint
    async def open_external_surface(
        conversation_id: str, force: bool = Query(False), body: dict | None = Body(None)
    ) -> Any:
        """``POST /surface/external``：把写权交给外部终端。

        ``?force=1`` = 强制接管（v1.0 §8.6 要求二次确认并说明风险——确认在前端，
        风险在这条 query 上）。
        """
        coordinator = _require_coordinator()
        conversation = await ctx.load_conversation(conversation_id)
        await require_open_project(repositories, conversation.project_id)
        draft = (body or {}).get("draft")
        if body is not None and (set(body) - {"draft"} or not isinstance(draft, (str, type(None)))):
            raise SessionApiError(400, "invalid_terminal_draft", "草稿格式无效")
        try:
            handoff = await coordinator.to_external(conversation, force=force, draft=draft)
        except SurfaceError as exc:
            raise surface_error(exc) from exc
        except DriverNotRegisteredError as exc:
            raise ctx.driver_not_registered(
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
            "draftPrefilled": handoff.draft_prefilled,
        }

    @ctx.endpoint
    async def return_to_card_surface(
        conversation_id: str, force: bool = Query(False)
    ) -> Any:
        """``POST /surface/card``：收回站内写权并校准原生历史。"""
        coordinator = _require_coordinator()
        conversation = await ctx.load_conversation(conversation_id)
        try:
            handoff = await coordinator.to_card(conversation, force=force)
        except SurfaceError as exc:
            raise surface_error(exc) from exc
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
            "lastEntryId": handoff.last_entry_id,
            "complete": handoff.complete,
            "gaps": list(handoff.gaps),
        }

    @ctx.endpoint
    async def focus_surface(conversation_id: str) -> Any:
        """``POST /surface/focus``：把持有这条会话的外部终端切到前台，不发送任何输入。"""
        coordinator = _require_coordinator()
        conversation = await ctx.load_conversation(conversation_id)
        return {"conversationId": conversation_id, "focused": await coordinator.focus(conversation)}

    @ctx.endpoint
    async def get_surface_preview(conversation_id: str) -> Any:
        """``GET /surface/preview``：外部终端期间的新消息预览（只读，不入事件）。"""
        conversation = await ctx.load_conversation(conversation_id)
        if surface_coordinator is None:
            return {"conversationId": conversation_id, "active": False, "supported": False, "entries": []}
        return {"conversationId": conversation_id, **await surface_coordinator.preview(conversation)}

    @ctx.endpoint
    async def get_surface(conversation_id: str) -> Any:
        """``GET /surface``：现在谁在写、上一次终端启动是什么。

        批次十五第 3 件：**没装 Terminal Launcher 时也回 200**，形状是
        ``{surface:"card", supported:false, lease:null, launch:null}``。
        原来这条路在这种装配下压根没注册，前端只能把「这台机器没有终端交接」
        当成一次请求失败来处理，会话页因此挂着一个报错。而事实一句话就说得清：
        这里只有站内卡片，交接那条路不存在——``supported`` 就是这句话，
        ``surface`` 仍然如实回 ``card``（那正是此刻在写的界面）。
        """
        conversation = await ctx.load_conversation(conversation_id)
        if surface_coordinator is None:
            return {
                "conversationId": conversation.id,
                "surface": "card",
                "supported": False,
                "lease": None,
                "launch": None,
            }
        return await surface_coordinator.describe(conversation)

    @ctx.endpoint
    async def list_launches(conversation_id: str) -> Any:
        """``GET /launches``：终端启动历史（「上次在终端做了什么」）。"""
        coordinator = _require_coordinator()
        await ctx.load_conversation(conversation_id)
        launches = await coordinator.list_launches(conversation_id)
        return {
            "conversationId": conversation_id,
            "launches": [launch_to_wire(launch) for launch in launches],
            "count": len(launches),
        }

    return [
        RouteSpec('/conversations/{conversation_id}/surface', get_surface, ["GET"], 'session_surface'),
        RouteSpec('/conversations/{conversation_id}/surface/preview', get_surface_preview, ["GET"], 'session_surface_preview'),
        RouteSpec('/conversations/{conversation_id}/surface/focus', focus_surface, ["POST"], 'session_surface_focus'),
        RouteSpec('/conversations/{conversation_id}/surface/external', open_external_surface, ["POST"], 'session_surface_external', when="coordinator"),
        RouteSpec('/conversations/{conversation_id}/surface/card', return_to_card_surface, ["POST"], 'session_surface_card', when="coordinator"),
        RouteSpec('/conversations/{conversation_id}/launches', list_launches, ["GET"], 'session_launches', when="coordinator"),
    ]


__all__ = ["build_surface_routes"]
