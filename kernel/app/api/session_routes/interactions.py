"""Run control: interrupt, answer an interaction, stop the runtime.

Interactions (approvals, questions, authentication) are resolved exactly
once by the Driver; an answer for a request that is no longer pending is
``interaction_not_found``. Proxied answers from the control MCP are checked
against what the card offered and recorded in the timeline.
"""

from __future__ import annotations

from typing import Any

from app.api import proxy_actions
from app.conversations.models import Conversation
from drivers.base import InteractionNotFoundError, UnsupportedCapabilityError
from runtime.session_host import RuntimeNotActiveError, StaleRunError
from app.api.session_routes.common import (
    InteractionResponseBody,
    InterruptBody,
    RouteSpec,
    SessionApiError,
    _not_found,
)
from app.api.session_routes.context import SessionRouteContext


def build_interaction_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Interrupt / interaction / stop endpoints."""
    from fastapi import Body

    host = ctx.host

    @ctx.endpoint
    async def interrupt(conversation_id: str, body: InterruptBody | None = Body(None)) -> Any:
        """中断当前一轮；**没有活跃 runtime 也不算失败**（批次十七第 2 件）。

        走查 ② 的原文是「Stop failed: 该 Conversation 当前没有活跃 runtime」——
        用户点停止，看到的是一句内部报错，而页面仍旧停在运行中。可这一刻的事实
        很朴素：没有在跑的东西可停，而页面之所以还显示运行中，是因为那一轮丢了
        终态。所以这里回 200 并顺手做一次自愈（AD-136 的第三处），把时间线收敛掉
        ——「没什么可停的」不是错误，是一个如实的答案。
        """
        conversation = await ctx.load_conversation(conversation_id)
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

    @ctx.endpoint
    async def resolve_interaction(
        conversation_id: str,
        interaction_id: str,
        body: InteractionResponseBody = Body(...),
    ) -> Any:
        conversation = await ctx.load_conversation(conversation_id)
        try:
            response = body.to_response()
        except (ValueError, TypeError) as exc:  # 交互种类是封闭集合：pydantic 校验错是 ValueError
            raise SessionApiError(
                400, "invalid_interaction_response", f"交互响应不合法：{exc}"
            ) from exc
        proxied = None
        if body.proxy is not None:
            # A proxied answer picks only what the card offers (the same rules the
            # conversation card renders); the host and driver checks still follow.
            if not host.is_active(conversation_id):
                raise SessionApiError(409, "runtime_not_active", "这条会话当前没有运行中的 Agent。")
            item = proxy_actions.pending_item(host.timeline(conversation_id), interaction_id)
            if item is None:
                raise _not_found("interaction_not_found", f"没有待处理的请求：{interaction_id}")
            try:
                action, choice = proxy_actions.offered_choice(item, response)
            except ValueError as exc:
                raise SessionApiError(400, "invalid_answer", "回答不符合要求，请检查后重试",
                                      detail={"reason": str(exc)}) from exc
            proxied = (item, action, choice)
        try:
            await host.resolve_interaction(conversation_id, interaction_id, response)
        except RuntimeNotActiveError as exc:
            raise SessionApiError(409, "runtime_not_active", str(exc)) from exc
        except InteractionNotFoundError as exc:
            raise _not_found("interaction_not_found", str(exc)) from exc
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        except ValueError as exc:
            # The driver rejected the answer itself (an option not offered, a non-numeric
            # value for a number field). The card stays pending, so say how to fix it.
            raise SessionApiError(400, "invalid_answer", "回答不符合要求，请检查后重试",
                                  detail={"reason": str(exc)}) from exc
        result: dict[str, Any] = {
            "conversationId": conversation_id,
            "interactionId": interaction_id,
            "resolved": True,
        }
        if proxied is not None:
            item, action, choice = proxied
            result["recorded"] = await ctx.record_proxy(conversation, proxy_actions.interaction_event(
                item, response, action=action, choice=choice, actor=body.proxy), run_id=item.run_id)
            result["action"] = action
        return result

    @ctx.endpoint
    async def stop_runtime(conversation_id: str) -> Any:
        await ctx.load_conversation(conversation_id)
        was_active = host.is_active(conversation_id)
        # v1.0 §9.4：停 Runtime 保留 Conversation 与原生 Session 映射。
        await host.stop_runtime(conversation_id)
        return {
            "conversationId": conversation_id,
            "stopped": was_active,
            "active": host.is_active(conversation_id),
        }

    return [
        RouteSpec('/conversations/{conversation_id}/interrupt', interrupt, ["POST"], 'session_interrupt'),
        RouteSpec('/conversations/{conversation_id}/interactions/{interaction_id}', resolve_interaction, ["POST"], 'session_resolve_interaction'),
        RouteSpec('/conversations/{conversation_id}/stop', stop_runtime, ["POST"], 'session_stop'),
    ]


__all__ = ["build_interaction_routes"]
