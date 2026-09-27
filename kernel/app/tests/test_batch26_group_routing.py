"""批次二十六：Group 消息路由 + Context Packet v0 + 批次二十三提的补项。

覆盖（对应任务书「验证」一节）
------------------------------
1. Group 时间线（``collaboration_messages`` 首次落笔）：广播行、成员变更的系统
   行、倒序 + ``before=`` 游标；
2. 路由：广播 / 定向 / 跳过（paused、left）/ 失败（引擎没注册）/ 单目标糖衣；
3. 关组之后 ``/broadcast`` 一律 409 ``group_closed``；
4. Context Packet v0：摘要、无历史时不编内容、``markdown`` 与 ``members`` 同源；
5. 补项：索引行的 ``groupTitle``、``GET /groups`` 的 ``memberConversationIds``、
   组级流的 ``?since=`` 重放与断档提示。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据、不起任何真进程。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.api.group_router import GroupEventHub  # noqa: E402
from app.api.group_views import (  # noqa: E402
    DELIVERY_STATUSES,
    SUMMARY_LIMIT,
    truncate_summary,
)
from app.conversations.models import Conversation  # noqa: E402
from app.tests.test_batch22_groups import (  # noqa: E402
    GroupHarness,
    _api,
    _new_conversation,
    _new_group,
    _seeded,
)
from app.tests.test_session_router import SseProbe, TEST_TOKEN  # noqa: E402
from drivers.mock.fixtures import text_stream_script  # noqa: E402


async def _join(api, group_id: str, conversation_id: str) -> dict:
    response = await api.post(
        f"/api/groups/{group_id}/members", json={"conversationId": conversation_id}
    )
    assert response.status_code == 201, response.text
    return response.json()["member"]


async def _wait_for_deliveries(api, group_id: str, count: int, *, timeout: float = 5.0):
    """等**开这条线程的那一行**上的 ``deliveries`` 攒够 ``count`` 条。

    批次四十五 b：房间串行地一位一位投，所以这本账是随房间转下去一行行补上的
    （``_append_delivery`` 回填同一行）。轮询而不是 sleep 一个固定的数：假引擎的
    一轮是后台任务推的，快慢不该写进断言里。
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = (await api.get(f"/api/groups/{group_id}/messages")).json()["messages"]
        for row in rows:
            if row["kind"] in ("broadcast", "directed") and len(row["deliveries"]) >= count:
                return row["deliveries"]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"等不到 {count} 条投递；现在是 "
                f"{[(r['kind'], len(r['deliveries'])) for r in rows]}"
            )
        await asyncio.sleep(0.02)


async def _ghost_conversation(built: GroupHarness, title: str) -> Conversation:
    """一条绑在「没有 Driver 的 Binding」上的会话：投递必然失败，且不起任何进程。"""
    return await built.repositories.conversations.save(
        Conversation.create(
            project_id=built.project_id,
            agent_binding_id=built.ghost_binding.id,
            title=title,
        )
    )


# --------------------------------------------------------------------------- #
# 1. Group 时间线
# --------------------------------------------------------------------------- #


async def test_broadcast_writes_one_timeline_row(tmp_path) -> None:
    """第 1 件：一次广播 = 时间线上一行，带 kind / authorRole / deliveries。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "组员甲")
        async with _api(built) as api:
            group = await _new_group(api, "会说话的组")
            member = await _join(api, group["id"], conversation.id)

            posted = await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "各位早"}
            )
            assert posted.status_code == 200, posted.text
            body = posted.json()
            message = body["message"]
            assert message["kind"] == "broadcast"
            assert message["authorRole"] == "user"
            assert message["text"] == "各位早"
            assert message["targetMemberIds"] == []
            assert message["groupId"] == group["id"]
            assert [d["memberId"] for d in body["deliveries"]] == [member["id"]]
            assert body["deliveries"][0]["status"] == "sent"
            # 响应体里的 deliveries 与落在时间线上的那一行是同一份，不是两份。
            assert message["deliveries"] == body["deliveries"]

            timeline = (await api.get(f"/api/groups/{group['id']}/messages")).json()
            assert timeline["groupId"] == group["id"]
            kinds = [row["kind"] for row in timeline["messages"]]
            # 倒序：最新的广播在前，然后是「他是组长」（批次四十八：第一位 active
            # 成员进来就被指定），最后是成员加入那一行。
            assert kinds == ["broadcast", "system", "system"]
            assert timeline["messages"][1]["authorRole"] == "system"
            assert timeline["messages"][1]["text"] == "组员甲 是组长"
            assert "组员甲" in timeline["messages"][2]["text"]
    finally:
        await built.aclose()


async def test_member_changes_leave_a_system_row(tmp_path) -> None:
    """第 1 件：成员变更也各占一行——否则「A 是在广播之前还是之后进来的」查不到。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "来去自如")
        async with _api(built) as api:
            group = await _new_group(api)
            member = await _join(api, group["id"], conversation.id)
            await api.post(
                f"/api/groups/{group['id']}/members/{member['id']}/pause"
            )
            await api.post(
                f"/api/groups/{group['id']}/members/{member['id']}/resume"
            )
            await api.delete(f"/api/groups/{group['id']}/members/{member['id']}")

            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "messages"
            ]
            # 倒序，所以反过来读就是发生的顺序。
            changes = [row["deliveries"] for row in rows]
            assert all(delivery == [] for delivery in changes)
            assert [row["kind"] for row in rows] == ["system"] * 6
            texts = [row["text"] for row in reversed(rows)]
            assert "加入" in texts[0]
            # 批次四十八：他是组里第一位 active 成员，所以他就是组长。
            assert texts[1] == "来去自如 是组长"
            assert "暂停" in texts[2]
            # 暂停的是组长而组里没有别人 → 组长位空着，**不写**「（无）是组长」；
            # 他一恢复就又接上，于是又一行。
            assert "恢复" in texts[3]
            assert texts[4] == "来去自如 是组长"
            assert "移出" in texts[5]
    finally:
        await built.aclose()


async def test_timeline_paginates_backwards_with_before(tmp_path) -> None:
    """第 1 件：``?limit=`` + ``?before=`` 向前翻页，``nextBefore`` 是下一页的游标。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "话很多")
        async with _api(built) as api:
            group = await _new_group(api)
            member = await _join(api, group["id"], conversation.id)
            # This checks pagination, independently of asynchronous model turns
            # and the failure notices produced by sending while a model is busy.
            await api.post(f"/api/groups/{group['id']}/members/{member['id']}/pause")
            for index in range(5):
                await api.post(
                    f"/api/groups/{group['id']}/broadcast", json={"text": f"第 {index} 条"}
                )

            first = (
                await api.get(
                    f"/api/groups/{group['id']}/messages", params={"limit": 2}
                )
            ).json()
            assert [row["text"] for row in first["messages"]] == ["第 4 条", "第 3 条"]
            assert first["count"] == 2
            cursor = first["nextBefore"]
            assert cursor == first["messages"][-1]["sequence"]

            second = (
                await api.get(
                    f"/api/groups/{group['id']}/messages",
                    params={"limit": 2, "before": cursor},
                )
            ).json()
            assert [row["text"] for row in second["messages"]] == ["第 2 条", "第 1 条"]

            # 一直翻到头：最早那一行是成员加入的系统行，再往前就空了。
            tail = (
                await api.get(
                    f"/api/groups/{group['id']}/messages",
                    params={"limit": 50, "before": 0},
                )
            ).json()
            assert tail["messages"] == [] and tail["nextBefore"] is None
    finally:
        await built.aclose()


async def test_timeline_page_holding_sequence_zero_ends_the_cursor(tmp_path) -> None:
    """批次五十二第 3 件（真机 UI-04）：翻到含 sequence 0 的那一页就是到头。

    此前这里回的是 0（那一行的 sequence），前端于是还举着「加载更早」，用户得再
    点一次、拿回一页空的才看见它消失——而那一行正是这个组的第一行。
    """
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "话很多")
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], conversation.id)
            for index in range(5):
                await api.post(
                    f"/api/groups/{group['id']}/broadcast", json={"text": f"第 {index} 条"}
                )

            # 一页页往前翻，直到这一页里出现 sequence 0。
            cursor: int | None = None
            seen: list[int] = []
            for _ in range(20):
                params: dict[str, int] = {"limit": 2}
                if cursor is not None:
                    params["before"] = cursor
                page = (
                    await api.get(f"/api/groups/{group['id']}/messages", params=params)
                ).json()
                seen.extend(row["sequence"] for row in page["messages"])
                if 0 in seen:
                    # 到头的那一页**自己**就得把游标关掉，不必再点一次空页。
                    assert page["messages"][-1]["sequence"] == 0
                    assert page["nextBefore"] is None
                    break
                assert page["nextBefore"] is not None
                cursor = page["nextBefore"]
            else:  # pragma: no cover - 上面的循环必然在几页内翻到 0
                raise AssertionError(f"没翻到 sequence 0；看到的是 {seen}")

            # 整组一页装得下时也是同一条判据（那一页的最早行就是 0）。
            whole = (
                await api.get(
                    f"/api/groups/{group['id']}/messages", params={"limit": 200}
                )
            ).json()
            assert whole["messages"][-1]["sequence"] == 0
            assert whole["nextBefore"] is None
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 2. 路由：广播 / 定向 / 跳过 / 失败
# --------------------------------------------------------------------------- #


async def test_broadcast_reaches_every_active_member(tmp_path) -> None:
    """每个 active 成员最终都被投到——但批次四十五 b 起是**一位一位**轮着投。

    PRD §B4：一轮内按队列顺序串行，上一位说完下一位才收到（并行会让两个人同时
    回一个已经过时的房间）。所以这里看的是「投递这本账最后齐不齐」，而不是「一次
    响应体里有没有两行」。
    """
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "甲")
        second = await _new_conversation(built, "乙")
        # 队首那位得**答得完**这一轮，队尾才轮得到：默认剧本停在权限请求上等人
        # 回话，那是「一轮还没结束」，不是「房间不投给乙」。
        for conversation in (first, second):
            built.driver.set_script(conversation.id, text_stream_script())
        async with _api(built) as api:
            group = await _new_group(api)
            a = await _join(api, group["id"], first.id)
            b = await _join(api, group["id"], second.id)
            body = (
                await api.post(
                    f"/api/groups/{group['id']}/broadcast", json={"text": "全体注意"}
                )
            ).json()
            # 这一刻只有队首那位收到了。**队首不再是先加入的那个**（批次四十八）：
            # 先加入的那位是组长，而组长永远排队尾——它要读完这一轮所有人的话再说。
            assert [d["memberId"] for d in body["deliveries"]] == [b["id"]]
            assert body["deliveries"][0]["status"] == "sent"

            deliveries = await _wait_for_deliveries(api, group["id"], 2)
            assert {d["memberId"] for d in deliveries} == {a["id"], b["id"]}
            assert {d["status"] for d in deliveries} == {"sent"}
            assert {d["conversationId"] for d in deliveries} == {built.group_session_for(first.id), built.group_session_for(second.id)}
    finally:
        await built.aclose()


async def test_directed_message_only_reaches_the_named_members(tmp_path) -> None:
    """给了 ``targetMemberIds`` 就只发给这几个，时间线上那一行 kind=directed。"""
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "被点名的")
        second = await _new_conversation(built, "没被点名的")
        async with _api(built) as api:
            group = await _new_group(api)
            a = await _join(api, group["id"], first.id)
            await _join(api, group["id"], second.id)
            body = (
                await api.post(
                    f"/api/groups/{group['id']}/broadcast",
                    json={"text": "只给你", "targetMemberIds": [a["id"]]},
                )
            ).json()
            assert body["message"]["kind"] == "directed"
            assert body["message"]["targetMemberIds"] == [a["id"]]
            assert [d["memberId"] for d in body["deliveries"]] == [a["id"]]

            unknown = await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "给幽灵", "targetMemberIds": ["member:nobody"]},
            )
            assert unknown.status_code == 404
            assert unknown.json()["error"]["code"] == "member_not_found"
    finally:
        await built.aclose()


async def test_paused_and_left_members_are_skipped_not_failed(tmp_path) -> None:
    """任务书第 2 件：``paused`` / ``left`` 跳过并各记一个 ``skipped_*``。

    两者是**定向**时才会出现的行：广播的收件人集合本来就只有 ``active``——
    「暂停的人没收到」正是暂停这个动作的含义，不该在广播的回执里报一次异常。
    """
    built = await _seeded(tmp_path)
    try:
        paused_conversation = await _new_conversation(built, "暂停的")
        left_conversation = await _new_conversation(built, "走了的")
        async with _api(built) as api:
            group = await _new_group(api)
            paused = await _join(api, group["id"], paused_conversation.id)
            left = await _join(api, group["id"], left_conversation.id)
            await api.post(f"/api/groups/{group['id']}/members/{paused['id']}/pause")
            await api.delete(f"/api/groups/{group['id']}/members/{left['id']}")

            broadcast = (
                await api.post(
                    f"/api/groups/{group['id']}/broadcast", json={"text": "有人在吗"}
                )
            ).json()
            assert broadcast["deliveries"] == []

            directed = (
                await api.post(
                    f"/api/groups/{group['id']}/broadcast",
                    json={
                        "text": "点名问一下",
                        "targetMemberIds": [paused["id"], left["id"]],
                    },
                )
            ).json()
            statuses = {d["memberId"]: d["status"] for d in directed["deliveries"]}
            assert statuses[paused["id"]] == "skipped_paused"
            assert statuses[left["id"]] == "skipped_left"
            assert set(statuses.values()) <= DELIVERY_STATUSES
    finally:
        await built.aclose()


async def test_one_failed_delivery_does_not_stop_the_others(tmp_path) -> None:
    """第 2 件：某个成员发不出去记 ``failed``，其余照发——投递是尽力送到每个人。

    批次四十五 b 起这件事换了个形状但没换意思：投不出去的那一位**按「这一轮他没
    说话」算**，房间立刻推给下一位（等一个永远不会来的 ``run.completed`` 会把整个
    房间挂死）。所以引擎没注册的那位排在队首时，队尾那位照样收得到。
    """
    built = await _seeded(tmp_path)
    try:
        broken = await _ghost_conversation(built, "引擎没注册的")
        healthy = await _new_conversation(built, "正常的")
        async with _api(built) as api:
            group = await _new_group(api)
            # 正常的那位先加入 → 它是组长 → 它排队尾；引擎没注册的那位于是在队首，
            # 正是这条用例要的形状（批次四十八之前靠加入顺序，现在靠组长排最后）。
            good = await _join(api, group["id"], healthy.id)
            bad = await _join(api, group["id"], broken.id)
            body = (
                await api.post(
                    f"/api/groups/{group['id']}/broadcast", json={"text": "都听得到吗"}
                )
            ).json()
            statuses = {d["memberId"]: d for d in body["deliveries"]}
            assert statuses[good["id"]]["status"] == "sent"
            assert statuses[bad["id"]]["status"] == "failed"
            # 失败要说得出为什么，否则用户只知道「有人没收到」。批次二十七把
            # 「为什么」搬到了 `reason`（稳定 code），`detail` 只留人话。
            assert statuses[bad["id"]]["reason"] == "driver_not_registered"
            assert "backend:ghost" in statuses[bad["id"]]["detail"]
            # 而且这条账落在了时间线上，不只是这次响应体里。
            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()
            posted = [r for r in rows["messages"] if r["kind"] == "broadcast"][0]
            assert posted["deliveries"] == body["deliveries"]
    finally:
        await built.aclose()


async def test_send_to_member_is_the_same_path(tmp_path) -> None:
    """第 2 件：``/members/{id}/send`` 是单目标糖衣，落的行与定向消息一模一样。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "被单独找")
        async with _api(built) as api:
            group = await _new_group(api)
            member = await _join(api, group["id"], conversation.id)
            body = (
                await api.post(
                    f"/api/groups/{group['id']}/members/{member['id']}/send",
                    json={"text": "单独说一句"},
                )
            ).json()
            assert body["message"]["kind"] == "directed"
            assert body["message"]["targetMemberIds"] == [member["id"]]
            assert body["deliveries"][0]["status"] == "sent"

            missing = await api.post(
                f"/api/groups/{group['id']}/members/member:nobody/send",
                json={"text": "给幽灵"},
            )
            assert missing.status_code == 404
    finally:
        await built.aclose()


async def test_broadcasting_into_a_closed_group_is_409(tmp_path) -> None:
    """第 2 件末句：组关了就不能再往里发；时间线仍然读得到（那是账本）。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "关组前的成员")
        async with _api(built) as api:
            group = await _new_group(api)
            member = await _join(api, group["id"], conversation.id)
            await api.post(f"/api/groups/{group['id']}/close")

            blocked = await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "还在吗"}
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "group_closed"

            also_blocked = await api.post(
                f"/api/groups/{group['id']}/members/{member['id']}/send",
                json={"text": "还在吗"},
            )
            assert also_blocked.status_code == 409

            readable = await api.get(f"/api/groups/{group['id']}/messages")
            assert readable.status_code == 200
            assert readable.json()["count"] >= 1
    finally:
        await built.aclose()


async def test_message_posted_shows_up_on_the_group_stream(tmp_path) -> None:
    """第 1 件：``kaus/group.changed`` 多一个 ``change: message_posted``（带 messageId）。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "组员")
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], conversation.id)
            async with SseProbe(built.app, "/api/groups/events") as probe:
                posted = (
                    await api.post(
                        f"/api/groups/{group['id']}/broadcast", json={"text": "看得到吗"}
                    )
                ).json()
                frames = await probe.next_events(1)
        assert frames[0]["data"]["change"] == "message_posted"
        assert frames[0]["data"]["messageId"] == posted["message"]["id"]
        # 广播这条与成员无关，所以不带 memberId/conversationId 这两个键。
        assert "memberId" not in frames[0]["data"]
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 3. Context Packet v0
# --------------------------------------------------------------------------- #


async def test_context_packet_summarises_each_member(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "说过话的会话")
        async with _api(built) as api:
            group = await _new_group(api, "评审组")
            member = await _join(api, group["id"], conversation.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "先看一下这段代码"}
            )

            packet = (
                await api.get(f"/api/groups/{group['id']}/context-packet")
            ).json()
            assert packet["groupId"] == group["id"]
            assert packet["generatedAt"].endswith("Z")
            entry = packet["members"][0]
            assert entry["memberId"] == member["id"]
            assert entry["conversationId"] == member["conversationId"]
            assert entry["title"] == "说过话的会话"
            assert entry["bindingId"] == built.binding.id
            assert entry["backendId"] == built.binding.backend_id
            assert entry["participationState"] == "active"
            assert entry["runState"] in ("idle", "running")
            assert entry["lastUserMessage"] == "先看一下这段代码"
            # markdown 与 members 同源：内容一样，只是形状不同。
            assert "评审组 · Context Packet" in packet["markdown"]
            assert "说过话的会话" in packet["markdown"]
            assert "先看一下这段代码" in packet["markdown"]
    finally:
        await built.aclose()


async def test_context_packet_without_history_says_nothing_rather_than_inventing(
    tmp_path,
) -> None:
    """AD-154 / N §13.1：取不到摘要就**不放这个键**，不编一句「暂无内容」。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "一句话都还没说")
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], conversation.id)
            packet = (
                await api.get(f"/api/groups/{group['id']}/context-packet")
            ).json()
            entry = packet["members"][0]
            assert "lastUserMessage" not in entry
            assert "lastAssistantMessage" not in entry
            assert "lastCompletedAt" not in entry
            assert "还没有可摘要的对话内容" in packet["markdown"]
    finally:
        await built.aclose()


async def test_context_packet_is_read_only_and_injects_nothing(tmp_path) -> None:
    """AD-154：取一次 Context Packet **不会**往任何成员会话里写一个字。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "不该被打扰")
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], conversation.id)
            before = await built.host.event_store.latest_sequence(conversation.id)
            timeline_before = (
                await api.get(f"/api/groups/{group['id']}/messages")
            ).json()["count"]

            await api.get(f"/api/groups/{group['id']}/context-packet")
            await api.get(f"/api/groups/{group['id']}/context-packet")

            assert (
                await built.host.event_store.latest_sequence(conversation.id) == before
            )
            after = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "count"
            ]
            assert after == timeline_before
    finally:
        await built.aclose()


def test_summary_truncation_is_capped_and_collapses_whitespace() -> None:
    assert truncate_summary(None) is None
    assert truncate_summary("   \n  ") is None
    assert truncate_summary(" 两  行\n合成一行 ") == "两 行 合成一行"
    long = truncate_summary("字" * (SUMMARY_LIMIT + 50))
    assert long is not None and len(long) == SUMMARY_LIMIT and long.endswith("…")


# --------------------------------------------------------------------------- #
# 4. 批次二十三提的三个补项
# --------------------------------------------------------------------------- #


async def test_index_rows_carry_the_group_title(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        inside = await _new_conversation(built, "在组里")
        outside = await _new_conversation(built, "不在组里")
        async with _api(built) as api:
            group = await _new_group(api, "有名字的组")
            await _join(api, group["id"], inside.id)
            rows = {
                row["id"]: row
                for row in (await api.get("/api/conversations")).json()["conversations"]
            }
            assert rows[inside.id]["groupTitle"] is None
            assert rows[outside.id]["groupTitle"] is None

            await api.post(f"/api/groups/{group['id']}/close")
            rows = {
                row["id"]: row
                for row in (await api.get("/api/conversations")).json()["conversations"]
            }
            # 关掉的组不算数，两个键一起变回 null（不会只剩半个标签）。
            assert rows[inside.id]["groupId"] is None
            assert rows[inside.id]["groupTitle"] is None
    finally:
        await built.aclose()


async def test_group_list_rows_carry_member_conversation_ids(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "甲")
        second = await _new_conversation(built, "乙")
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], first.id)
            member = await _join(api, group["id"], second.id)
            row = (await api.get("/api/groups")).json()["groups"][0]
            assert set(row["memberConversationIds"]) == {built.group_session_for(first.id), built.group_session_for(second.id)}
            assert row["memberCount"] == 2

            await api.delete(f"/api/groups/{group['id']}/members/{member['id']}")
            row = (await api.get("/api/groups")).json()["groups"][0]
            assert row["memberConversationIds"] == [built.group_session_for(first.id)]
    finally:
        await built.aclose()


async def test_group_stream_replays_recent_changes_with_since(tmp_path) -> None:
    """第 4 件：``?since=`` 补最近的漏帧；断档时先发一帧 ``replayTruncated``。"""
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            await _new_group(api, "第一个")
            await _new_group(api, "第二个")
            await _new_group(api, "第三个")

        async with SseProbe(built.app, "/api/groups/events", query="since=1") as probe:
            frames = await probe.next_events(2)
        assert [f["sequence"] for f in frames] == [2, 3]
        assert all(f["type"] == "kaus/group.changed" for f in frames)

        # 游标在缓冲之外（这里模拟进程重启后前端拿着旧游标回来）：先说一声补不齐。
        async with SseProbe(built.app, "/api/groups/events", query="since=99") as probe:
            frames = await probe.next_events(1)
        assert frames[0]["type"] == "kaus/group.replayTruncated"
        assert frames[0]["data"]["since"] == 99
    finally:
        await built.aclose()


def test_hub_replay_reports_a_gap_when_the_buffer_rolled_over() -> None:
    """环形缓冲的边界：挤掉的那一段不会被静默跳过，而是报断档。"""
    hub = GroupEventHub(buffer_size=3)
    for index in range(6):
        hub.publish({"groupId": f"collaboration:{index}", "change": "group_created"})
    frames, truncated = hub.replay(4)
    assert [f["sequence"] for f in frames] == [5, 6]
    assert truncated is False

    frames, truncated = hub.replay(0)
    # 缓冲里最早的是 4，0 到 3 已经被挤掉了 —— 给得出的仍然给，但要说清楚。
    assert [f["sequence"] for f in frames] == [4, 5, 6]
    assert truncated is True


async def test_group_stream_since_still_needs_a_token(tmp_path) -> None:
    """重放不是绕过鉴权的后门。"""
    built = await _seeded(tmp_path)
    try:
        async with SseProbe(
            built.app, "/api/groups/events", query="since=0", token=None
        ) as probe:
            assert probe.status == 401
        async with SseProbe(
            built.app,
            "/api/groups/events",
            query=f"since=0&token={TEST_TOKEN}",
            token=None,
        ) as probe:
            assert probe.status == 200
    finally:
        await built.aclose()
