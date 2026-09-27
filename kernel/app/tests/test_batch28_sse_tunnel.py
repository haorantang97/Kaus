"""批次二十八第 1 件：SSE 在隧道/代理下也要能到岸。

真机现象（2026-09-05 复测取证）：测试员通过 Cloudflare quick tunnel 看页面，
后端每一条「没有回复」的会话其实都回了——重放里有 50+ 条事件——但页面显示
「历史没有随重放到达」。也就是说流本身没到浏览器：中间层要先攒够一段字节
才肯转发，而我们重放完之后就静默了。

所以这里验三件可断言的事实：

1. 每条 SSE 端点都带 ``Cache-Control: no-cache, no-transform`` +
   ``X-Accel-Buffering: no`` + ``Connection: keep-alive``；
2. 重放批之后、开始等待之前有一帧 ≥2KB 的注释填充（注释行不进 ``EventSource``
   的 message 回调，前端看不见它）；
3. 心跳仍然**只有一套**（AD-62 的 ``: keepalive``），周期收到 15s，且按周期
   反复发——测试用短周期覆盖，不 sleep 一个魔法常数。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.api.group_router import (  # noqa: E402
    DEFAULT_GROUP_KEEPALIVE_INTERVAL,
)
from app.api.session_router import DEFAULT_KEEPALIVE_INTERVAL  # noqa: E402
from app.api.session_views import (  # noqa: E402
    KEEPALIVE_FRAME,
    SSE_HEADERS,
    SSE_PADDING_BYTES,
    SSE_PADDING_FRAME,
)
from app.tests.test_batch22_groups import _seeded as _seeded_group  # noqa: E402
from app.tests.test_session_router import (  # noqa: E402
    SseProbe,
    _conversation_with_a_run,
    _seeded,
)

EXPECTED_HEADERS = {
    "cache-control": "no-cache, no-transform",
    "x-accel-buffering": "no",
    "connection": "keep-alive",
}


def _assert_tunnel_headers(headers: dict) -> None:
    for key, value in EXPECTED_HEADERS.items():
        assert headers.get(key) == value, (key, headers.get(key))


def test_the_shared_header_set_is_the_one_we_promise() -> None:
    assert {k.lower(): v for k, v in SSE_HEADERS.items()} == EXPECTED_HEADERS


def test_padding_is_a_comment_line_of_at_least_two_kilobytes() -> None:
    assert SSE_PADDING_BYTES >= 2048
    # 注释帧：以 ':' 开头、以空行结束。浏览器不会把它当成一条事件。
    assert SSE_PADDING_FRAME.startswith(": ")
    assert SSE_PADDING_FRAME.endswith("\n\n")
    assert len(SSE_PADDING_FRAME.encode()) >= 2048


def test_heartbeat_period_is_fifteen_seconds_on_both_streams() -> None:
    """两条流同一个周期；心跳机制只此一套，不要再叠第二套。"""
    assert DEFAULT_KEEPALIVE_INTERVAL == 15.0
    assert DEFAULT_GROUP_KEEPALIVE_INTERVAL == 15.0


async def test_conversation_stream_headers_and_padding_after_the_replay(tmp_path):
    """会话流：重放的 data 帧先来，紧跟一帧填充，然后才是等待。"""
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=3)
        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events"
        ) as probe:
            assert probe.status == 200
            _assert_tunnel_headers(probe.headers)
            seen: list[str] = []
            for _ in range(40):
                frame = await probe.next_frame()
                seen.append(frame)
                if frame == SSE_PADDING_FRAME:
                    break
        assert SSE_PADDING_FRAME in seen, "重放之后没有填充帧"
        # 填充**在重放之后**：它前面全是 data 帧，没有心跳夹在中间。
        head = seen[: seen.index(SSE_PADDING_FRAME)]
        assert head, "重放批是空的，这条用例没测到东西"
        assert all(frame.startswith("data: ") for frame in head)
    finally:
        await built.aclose()


async def test_empty_conversation_still_gets_padding_before_the_first_heartbeat(
    tmp_path,
):
    """没有历史的会话也要先见字节——否则隧道下首屏永远停在「历史没到达」。"""
    from app.conversations.models import Conversation

    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation = await built.repositories.conversations.save(
            Conversation.create(
                project_id=built.project_id,
                agent_binding_id=built.binding.id,
                title="空会话",
            )
        )
        async with SseProbe(
            built.app, f"/api/conversations/{conversation.id}/events"
        ) as probe:
            first = await probe.next_frame()
            second = await probe.next_frame()
        assert first == SSE_PADDING_FRAME
        assert second == KEEPALIVE_FRAME
    finally:
        await built.aclose()


async def test_heartbeat_keeps_coming_at_the_configured_cadence(tmp_path):
    """心跳按周期反复发；周期在测试里被覆写成短值，不靠 sleep 猜。"""
    from app.conversations.models import Conversation

    interval = 0.05
    built = await _seeded(tmp_path, keepalive=interval)
    try:
        conversation = await built.repositories.conversations.save(
            Conversation.create(
                project_id=built.project_id,
                agent_binding_id=built.binding.id,
                title="空转",
            )
        )
        loop = asyncio.get_running_loop()
        async with SseProbe(
            built.app, f"/api/conversations/{conversation.id}/events"
        ) as probe:
            assert await probe.next_frame() == SSE_PADDING_FRAME
            started = loop.time()
            beats = [await probe.next_frame() for _ in range(3)]
            elapsed = loop.time() - started
        assert beats == [KEEPALIVE_FRAME] * 3
        # 三拍至少要花两个周期（第一拍从建流起算）；上限给得松，只为排除
        # 「一次性刷了三条」这种退化。
        assert 2 * interval <= elapsed < 3.0
    finally:
        await built.aclose()


async def test_group_stream_headers_and_padding(tmp_path):
    """组级流：同一套响应头，补帧之后同样有填充。"""
    built = await _seeded_group(tmp_path)
    try:
        async with SseProbe(built.app, "/api/groups/events") as probe:
            assert probe.status == 200
            _assert_tunnel_headers(probe.headers)
            assert await probe.next_frame() == SSE_PADDING_FRAME
    finally:
        await built.aclose()
