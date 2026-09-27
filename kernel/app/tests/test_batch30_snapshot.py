"""批次三十第 1 件：SSE 到不了的时候，用非流式快照把同一批事件拿回来。

真机现象（`docs/quality/verify-batch28-29-p1-p7.md` P1）：测试员经 Cloudflare quick
tunnel + 自己的代理看页面，会话的 SSE **一个字节都没到**——17 秒里既没有重放也没有
心跳——而同一份数据在 localhost 渲染完全正常。批次二十八已经把该加的头、填充帧、
心跳都加齐了，仍旧不到；那就不是我们这一侧还能修的东西。

所以换一条不需要长连接的路：``GET /api/conversations/{id}/events/snapshot``。
这里验的四件事，就是前端敢把它当 SSE 的替身所依赖的全部前提：

1. **形状**：``{conversationId, events[], lastSequence, runState}``，且 ``events``
   与 SSE 重放出来的是**同一批信封**（逐条比对，不是比个数）；
2. **增量**：``?since=`` 与 SSE 的 ``?after=`` 同语义（开区间），只回后面那一段；
3. **鉴权**：和别的会话端点同一把锁——没有 token 就是 401，**不**像 SSE 那样接受
   ``?token=``（它不是 EventSource，没有「带不了头」的理由）；
4. **上限**：一次最多 :data:`SNAPSHOT_MAX_EVENTS` 条，超了 ``truncated=true``
   ——静默截断会让页面永远差最后一段而自己不知道。

组级流那条快照同理（``GET /api/groups/events/snapshot``）。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from app.api.session_router import SNAPSHOT_MAX_EVENTS  # noqa: E402
from app.tests.test_batch22_groups import _seeded as _seeded_group  # noqa: E402
from app.tests.test_session_router import (  # noqa: E402
    TEST_TOKEN,
    SseProbe,
    _conversation_with_a_run,
    _seeded,
)


def _client(app, *, token: str | None = TEST_TOKEN) -> "httpx.AsyncClient":
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
    )


async def test_snapshot_returns_the_same_envelopes_as_the_sse_replay(tmp_path):
    """第一件：形状 + 「同一批信封」。

    逐条比对而不是比个数——如果快照走了另一条读法（比如只读 EventStore 而漏掉
    重建那一步），个数常常还是对的，错的是内容。
    """
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=4)
        async with SseProbe(
            built.app, f"/api/conversations/{conversation_id}/events"
        ) as probe:
            streamed = await probe.next_events(4)

        async with _client(built.app) as client:
            response = await client.get(
                f"/api/conversations/{conversation_id}/events/snapshot"
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload) >= {
            "conversationId",
            "events",
            "lastSequence",
            "runState",
        }
        assert payload["conversationId"] == conversation_id
        assert payload["events"][:4] == streamed
        assert payload["lastSequence"] == payload["events"][-1]["sequence"]
        assert payload["truncated"] is False
        # runState 是内核镜像，不是本端点自己算的一档：只要它是那几个值之一。
        assert isinstance(payload["runState"], str)
    finally:
        await built.aclose()


async def test_since_is_the_same_open_interval_as_the_sse_after(tmp_path):
    """第二件：``?since=`` 只回它后面的那一段，和 ``?after=`` 一个语义。"""
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=4)
        async with _client(built.app) as client:
            full = (
                await client.get(
                    f"/api/conversations/{conversation_id}/events/snapshot"
                )
            ).json()
            incremental = (
                await client.get(
                    f"/api/conversations/{conversation_id}/events/snapshot",
                    params={"since": 1},
                )
            ).json()
            nothing_new = (
                await client.get(
                    f"/api/conversations/{conversation_id}/events/snapshot",
                    params={"since": full["lastSequence"]},
                )
            ).json()

        assert [event["sequence"] for event in incremental["events"]] == [
            event["sequence"] for event in full["events"] if event["sequence"] > 1
        ]
        assert incremental["events"] == [
            event for event in full["events"] if event["sequence"] > 1
        ]
        # 一条新的都没有时**不能**把游标退回去：那会让前端把整条会话再收一遍。
        assert nothing_new["events"] == []
        assert nothing_new["lastSequence"] == full["lastSequence"]
    finally:
        await built.aclose()


async def test_snapshot_requires_the_same_bearer_as_every_other_endpoint(tmp_path):
    """第三件：鉴权。没头是 401，``?token=`` 也不算数（那是 SSE 的特权）。"""
    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=2)
        path = f"/api/conversations/{conversation_id}/events/snapshot"
        async with _client(built.app, token=None) as anonymous:
            assert (await anonymous.get(path)).status_code == 401
            assert (
                await anonymous.get(path, params={"token": TEST_TOKEN})
            ).status_code == 401
        async with _client(built.app, token="wrong-token-0123456789") as wrong:
            assert (await wrong.get(path)).status_code == 401
    finally:
        await built.aclose()


async def test_snapshot_caps_the_page_and_says_so(tmp_path):
    """第四件：上限。超了就 ``truncated=true`` + 游标停在这一页最后一条。"""
    import uuid

    built = await _seeded(tmp_path, keepalive=0.05)
    try:
        conversation_id = await _conversation_with_a_run(built, at_least=2)
        # 直接往 Event Store 里灌：这条用例验的是分页，不是驱动。拿现成的一条
        # 复制着改（信封的必填键很多，手搓一条只会把用例绑在信封的形状上）。
        store = built.host.event_store
        seed = (await store.replay(conversation_id))[0]
        latest = await store.latest_sequence(conversation_id) or 0
        await store.append_many(
            [
                seed.model_copy(
                    update={
                        "event_id": str(uuid.uuid4()),
                        "sequence": latest + 1 + offset,
                    }
                )
                for offset in range(SNAPSHOT_MAX_EVENTS + 5)
            ]
        )
        async with _client(built.app) as client:
            first = (
                await client.get(
                    f"/api/conversations/{conversation_id}/events/snapshot"
                )
            ).json()
        assert len(first["events"]) == SNAPSHOT_MAX_EVENTS
        assert first["truncated"] is True
        assert first["lastSequence"] == first["events"][-1]["sequence"]

        async with _client(built.app) as client:
            second = (
                await client.get(
                    f"/api/conversations/{conversation_id}/events/snapshot",
                    params={"since": first["lastSequence"]},
                )
            ).json()
        assert second["truncated"] is False
        assert second["events"], "第二页应当把剩下的那几条给出来"
        assert second["events"][0]["sequence"] == first["lastSequence"] + 1
    finally:
        await built.aclose()


async def test_group_snapshot_replays_the_same_frames_as_the_group_stream(tmp_path):
    """组级快照：同一份环形缓冲，``?since=`` 补帧；不带游标只给当前位置。"""
    built = await _seeded_group(tmp_path)
    try:
        async with _client(built.app) as client:
            here = (await client.get("/api/groups/events/snapshot")).json()
            assert here["events"] == []
            start = here["lastSequence"]

            created = await client.post("/api/groups", json={"title": "批次三十"})
            assert created.status_code == 201, created.text

            caught_up = (
                await client.get(
                    "/api/groups/events/snapshot", params={"since": start}
                )
            ).json()
        assert [frame["data"]["change"] for frame in caught_up["events"]] == [
            "group_created"
        ]
        assert caught_up["lastSequence"] == caught_up["events"][-1]["sequence"]
        assert caught_up["replayTruncated"] is False
        assert json.dumps(caught_up["events"])  # 帧必须是可序列化的纯数据
    finally:
        await built.aclose()
