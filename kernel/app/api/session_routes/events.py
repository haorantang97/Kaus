"""Event delivery: the SSE stream and its non-streaming snapshot.

Both endpoints reconcile the conversation first and, on a first attach,
restore from native history, then read the same replay batch from one
:class:`~runtime.session_host.EventSubscription` (AD-61 / AD-62, batch 28
and 30).
"""

from __future__ import annotations

from typing import Any

from app.api.event_wait import (
    STREAM_END as _STREAM_END,
    STREAM_IDLE as _STREAM_IDLE,
    next_or_none as _next_or_none,
)
from app.api.session_views import (
    KEEPALIVE_FRAME,
    SSE_HEADERS,
    SSE_PADDING_FRAME,
    envelope_to_wire,
    sse_data_line,
)
from app.api.session_routes.common import RouteSpec, SNAPSHOT_MAX_EVENTS, _dumps
from app.api.session_routes.context import SessionRouteContext


def build_event_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """SSE stream and snapshot endpoints."""
    from fastapi import Query
    from fastapi.responses import StreamingResponse

    host = ctx.host
    keepalive_interval = ctx.keepalive_interval

    @ctx.endpoint
    async def stream_events(
        conversation_id: str, after: int | None = Query(None, ge=-1)
    ) -> Any:
        conversation = await ctx.load_conversation(conversation_id)
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

    @ctx.endpoint
    async def events_snapshot(
        conversation_id: str, since: int | None = Query(None, ge=-1)
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
        conversation = await ctx.load_conversation(conversation_id)
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
            last_sequence = -1  # No delivered event: sequence 0 has not been observed.
        return {
            "conversationId": conversation_id,
            "events": [envelope_to_wire(envelope) for envelope in page],
            "lastSequence": last_sequence,
            # 截断了就说截断了：客户端据此立刻再取一次，而不是等下一个轮询周期。
            "truncated": len(envelopes) > len(page),
            "runState": await host.run_state_of(conversation),
        }

    return [
        RouteSpec('/conversations/{conversation_id}/events/snapshot', events_snapshot, ["GET"], 'session_events_snapshot'),
        RouteSpec('/conversations/{conversation_id}/events', stream_events, ["GET"], 'session_stream_events', auth="sse"),
    ]


__all__ = ["build_event_routes"]
