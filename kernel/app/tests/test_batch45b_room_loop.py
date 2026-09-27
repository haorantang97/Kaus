"""批次四十五 b：房间循环（PRD §B4 / AD-168）。

覆盖
----
1. 纯函数：点名解析（``@名字`` / 裸名 / 重名后缀）、略过与最终答复的判定、
   房间增量的白名单与游标、增量的格式；
2. 纯状态机：队列推进、下一轮、全员略过、最终答复收口、轮数到顶、停、``@`` 接力；
3. 路由：假引擎按剧本跑完一条完整线程（第 1 轮各说一句、第 2 轮 B 收口），
   ``deliveries`` 随房间转下去补齐，``member_turn`` 上带得出 ``round`` 与
   ``deliveredSince``（L8）；
4. 「停」立刻生效、``roundCap=2`` 到顶、全员略过、旁观者的增量在被点名时补齐；
5. **投给成员的那一段里没有任何私有正文**（AD-154 的那条线）；
6. 重启收尾：``running`` 置 ``stopped`` 并在时间线上留一行。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据、不起任何真进程。
"""

from __future__ import annotations

import asyncio

import pytest

from app.collaboration.models import (
    DEFAULT_ROUND_CAP,
    CollaborationSession,
    RoomThread,
)
from app.collaboration.room import (
    ROOM_HEADER_SEPARATOR,
    RoomEntry,
    RoomMember,
    advance_thread,
    is_final,
    is_pass,
    public_entries,
    resolve_mentions,
    room_delta,
    start_thread,
    stop_thread,
)

A = RoomMember("member:a", "写手", "hermes", True)
B = RoomMember("member:b", "评审", "dsh", True)
C = RoomMember("member:c", "打杂", None, True)
ROOM = (A, B, C)

#: 这几个纯状态机用例里的组长。**永远排队尾**（PRD §A3），所以 `_thread()` 建出来的
#: 队列是 a、b、c —— 与批次四十五 b 那时一模一样，只是最后那位现在有了名字。
LEADER = "member:c"
ACTIVE = ("member:a", "member:b", "member:c")


# --------------------------------------------------------------------------- #
# 1. 点名 / 略过 / 最终答复
# --------------------------------------------------------------------------- #


def test_at_sign_and_bare_names_are_both_mentions() -> None:
    """产品方那句话是「A 和 B 你们辩论一下」——裸名不认就等于要求他学一个符号。"""
    assert resolve_mentions("@写手 @评审 你们辩论一下", ROOM) == ("member:a", "member:b")
    assert resolve_mentions("写手 和 评审 观点冲突了", ROOM) == ("member:a", "member:b")
    assert resolve_mentions("你们讨论一下这个问题", ROOM) == ()


def test_mentions_come_back_in_join_order_not_sentence_order() -> None:
    """队列的次序由「谁先进组」定，否则同一句话换个语序就换一个发言顺序。"""
    assert resolve_mentions("评审 你先说，然后 写手 接", ROOM) == (
        "member:a",
        "member:b",
    )


def test_a_suffixed_name_does_not_also_hit_the_plain_one() -> None:
    """重名成员的显示名是「写手」与「写手#2」，短的是长的的前缀。

    不按长度优先并把命中的那一段挖掉的话，一句 ``@写手#2`` 会同时点中两个人——
    而那正是 ``resolve_display_names`` 那张共用表想防住的事。
    """
    twins = (
        RoomMember("member:a", "写手", None, True),
        RoomMember("member:b", "写手", None, True),
    )
    assert resolve_mentions("@写手#2 你来", twins) == ("member:b",)
    assert resolve_mentions("@写手 你来", twins) == ("member:a",)


def test_pass_is_an_exact_match_after_trimming() -> None:
    for token in ("（略过）", "(略过)", "[pass]", "  （略过）  \n"):
        assert is_pass(token)
    # 以「（略过）」开头、后面还说了三句话的回复是**有内容的**。
    assert not is_pass("（略过）不过我补一句：第三段有问题")
    assert not is_pass("")


def test_final_is_judged_by_the_opening_not_by_containment() -> None:
    assert is_final("最终答复：我们选方案二。")
    assert is_final("【最终答复】选方案二")
    assert is_final("  \n最终答复：好")
    # 正文中间提到这四个字不是收口——按包含判会让房间在这句话上停下来。
    assert not is_final("你别急着给最终答复，再想想")


# --------------------------------------------------------------------------- #
# 2. 房间增量：白名单、游标、格式
# --------------------------------------------------------------------------- #


class _Row:
    def __init__(self, sequence, kind, content, *, author=None, metadata=None) -> None:
        self.sequence = sequence
        self.kind = kind
        self.content = content
        self.author_member_id = author
        self.metadata = metadata or {}


def test_only_public_kinds_get_into_the_delta() -> None:
    """AD-154 的那条线：成员的私有正文不在组时间线上，因此也进不了增量。

    白名单而不是黑名单——「哪些东西会被送进别人的上下文」不该靠记得改一处黑名单。
    """
    rows = [
        _Row(0, "broadcast", "各位早"),
        _Row(1, "member_turn", "我看完了", author="member:a"),
        _Row(2, "context_packet", "【私有】这条会话的全文快照"),
        _Row(3, "writeback", "【私有】写回项目的草稿"),
        _Row(4, "system", "打杂 加入了这个组"),
    ]
    entries = public_entries(rows, since_sequence=None)
    assert [entry.text for entry in entries] == [
        "各位早",
        "我看完了",
        "打杂 加入了这个组",
    ]
    joined = "".join(entry.text for entry in entries)
    assert "私有" not in joined


def test_a_passed_turn_is_not_worth_a_line_in_anybody_else_context() -> None:
    rows = [
        _Row(0, "broadcast", "各位早"),
        _Row(1, "member_turn", "（略过）", author="member:b", metadata={"passed": True}),
    ]
    assert [entry.text for entry in public_entries(rows, since_sequence=None)] == [
        "各位早"
    ]


def test_the_cursor_is_exclusive() -> None:
    rows = [_Row(index, "broadcast", f"第 {index} 条") for index in range(4)]
    assert [entry.text for entry in public_entries(rows, since_sequence=1)] == [
        "第 2 条",
        "第 3 条",
    ]


def test_room_delta_matches_the_prd_shape() -> None:
    """批次四十八改了两处：名片句后面多一句「组长：X。」，末尾那段约定换掉了
    （``[状态]`` 那一行整个不存在了）。**批次五十又换了一次**：约定里不再教成员
    点下一位——第五次修订之后成员的点名不进队列，教它写等于教一句不生效的话。"""
    delta = room_delta(
        group_title="周五评审",
        members=ROOM,
        recipient_member_id="member:a",
        speaker_ids=("member:a", "member:b"),
        spectator_ids=("member:c",),
        entries=(
            RoomEntry(None, "你们辩论一下"),
            RoomEntry("member:b", "我不同意"),
        ),
        leader_member_id="member:c",
    )
    assert delta is not None
    lines = delta.splitlines()
    assert lines[0] == (
        "[协作组「周五评审」] 你是「写手」（hermes）。"
        "组里：写手（你）、评审（dsh）、打杂。组长：打杂。"
    )
    assert lines[1] == "本轮发言人：写手、评审；旁观：打杂。"
    assert lines[2] == "自你上次发言以来房间里的对话："
    assert lines[3] == "- 用户：你们辩论一下"
    assert lines[4] == "- 评审：我不同意"
    assert lines[5] == (
        "约定：发言顺序由组长安排，不用在结尾 @ 下一位。"
        "要让谁回应你、或你还想再说一次，在正文里写明（可以写 @名字），"
        "组长会看到并决定下一轮谁说。"
        "没有新内容就只回「（略过）」。组长每轮最后发言。"
    )
    # 这一份是发给写手的，所以**没有**那段只给组长的话。
    assert len(lines) == 6
    assert "[状态]" not in delta


def test_room_delta_needs_the_recipient_to_be_in_the_room() -> None:
    assert (
        room_delta(
            group_title="组",
            members=ROOM,
            recipient_member_id="member:zzz",
            speaker_ids=(),
            spectator_ids=(),
            entries=(),
        )
        is None
    )


# --------------------------------------------------------------------------- #
# 3. 状态机
# --------------------------------------------------------------------------- #


def _thread(**changes) -> RoomThread:
    base = start_thread(
        previous=None,
        speaker_queue=("member:a", "member:b"),
        spectators=(),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )
    return base.evolve(**changes) if changes else base


def _say(
    thread: RoomThread,
    member_id: str,
    *,
    next_queue: tuple[str, ...] = (),
    passed: bool = False,
    final: bool = False,
    outcome: str | None = None,
    round_cap: int = DEFAULT_ROUND_CAP,
    leader: str | None = LEADER,
) -> RoomThread:
    """某位说完了。``next_queue`` 是接入层算好的下一轮队列（含队尾的组长）。"""
    return advance_thread(
        thread,
        member_id=member_id,
        passed=passed,
        final=final,
        outcome=outcome,
        next_queue=next_queue,
        active_member_ids=ACTIVE,
        leader_member_id=leader,
        round_cap=round_cap,
    )


def test_a_new_thread_starts_at_round_one_waiting_for_the_queue_head() -> None:
    thread = _thread()
    assert (thread.epoch, thread.round, thread.status) == (1, 1, "running")
    assert thread.speaker_queue == ("member:a", "member:b", "member:c")
    assert thread.awaiting_member_id == "member:a"


def test_an_empty_queue_is_idle_not_running() -> None:
    """一个活跃成员都没有时标 running，只会让组头永远显示「轮到（空）」。"""
    thread = start_thread(
        previous=None, speaker_queue=(), spectators=(), started_by_message_id=None
    )
    assert thread.status == "idle"
    assert thread.awaiting_member_id is None


def test_the_queue_advances_one_at_a_time_then_rolls_to_the_next_round() -> None:
    """批次四十八：要进下一轮，得**有人被点了名**——组长不点人就停（终止 3）。"""
    thread = _thread()
    for expected in ("member:b", "member:c"):
        thread = _say(thread, thread.awaiting_member_id or "")
        assert thread.status == "running"
        assert thread.awaiting_member_id == expected
        assert thread.round == 1
    # 组长（队尾）说完，它点了 a → 下一轮队列是 a + 组长。
    thread = _say(thread, "member:c", next_queue=("member:a", "member:c"))
    assert (thread.round, thread.awaiting_member_id) == (2, "member:a")
    assert thread.speaker_queue == ("member:a", "member:c")
    assert thread.spectators == ("member:b",)  # 这一轮没被点到 → 旁观
    assert thread.spoke_in_round == 0  # 新一轮的计数器归零


def test_a_final_reply_from_the_leader_stops_the_room() -> None:
    """终止 2。``final`` 是接入层判过「他是组长」之后才传进来的。"""
    thread = _say(_thread(), "member:a", final=True)
    assert thread.status == "final"
    assert thread.ended_reason == "leader_final"
    assert thread.awaiting_member_id is None


def test_a_round_where_nobody_says_anything_stops_the_room() -> None:
    thread = _thread()
    for member_id in ("member:a", "member:b", "member:c"):
        thread = _say(thread, member_id, passed=True)
    assert thread.status == "idle"
    assert thread.ended_reason == "silent"
    assert thread.awaiting_member_id is None


def test_a_single_pass_is_not_a_silent_round() -> None:
    """有人说了话就不算「没人说话」，房间按下一轮队列接着转（终止 5 不成立）。"""
    thread = _thread()
    thread = _say(thread, "member:a")
    thread = _say(thread, "member:b", passed=True)
    thread = _say(thread, "member:c", next_queue=("member:a", "member:c"))
    assert thread.status == "running"
    assert thread.ended_reason is None
    assert thread.round == 2


def test_the_round_cap_is_the_last_line_of_defence() -> None:
    """终止 4：上限到了不是干停，而是单独投给**组长**一条收口指令。

    要走到这一条，组长得**每一轮都还在点人**——它一旦不点，终止 3 先生效（那才是
    预期的停止点）。所以这里让它每轮都点 a：安全阀防的正是这种一直有下一轮的讨论。
    """
    thread = _thread()
    keeps_going = ("member:a", LEADER)
    for member_id in ("member:a", "member:b", "member:c"):
        thread = _say(thread, member_id, next_queue=keeps_going, round_cap=2)
    assert (thread.round, thread.status) == (2, "running")
    assert thread.speaker_queue == keeps_going
    for member_id in keeps_going:
        thread = _say(thread, member_id, next_queue=keeps_going, round_cap=2)
    assert thread.status == "closing"
    assert thread.phase == "closing"
    assert thread.ended_reason == "round_cap"
    assert thread.awaiting_member_id == LEADER  # 收口的永远是组长


def test_the_leader_not_naming_anybody_ends_the_thread() -> None:
    """终止 3：一轮走完、下一轮除组长外没人 → 组长那一段就是结论，房间停。"""
    thread = _thread()
    for member_id in ("member:a", "member:b"):
        thread = _say(thread, member_id)
    thread = _say(thread, LEADER, next_queue=(LEADER,))
    assert thread.status == "final"
    assert thread.ended_reason == "leader_closed"
    assert thread.awaiting_member_id is None


def test_stop_takes_effect_immediately_and_is_idempotent() -> None:
    stopped = stop_thread(_thread())
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.awaiting_member_id is None
    # 没在转的时候再停一次是一次无操作，回的是同一个对象。
    assert stop_thread(stopped) is stopped
    assert stop_thread(None) is None


def test_a_new_user_message_opens_a_new_epoch_over_a_running_thread() -> None:
    running = _thread()
    fresh = start_thread(
        previous=running,
        speaker_queue=("member:b",),
        spectators=("member:a", "member:c"),
        started_by_message_id="groupmsg:9",
        leader_member_id=LEADER,
    )
    assert fresh.epoch == running.epoch + 1
    assert fresh.round == 1
    assert fresh.awaiting_member_id == "member:b"
    assert fresh.speaker_queue == ("member:b", "member:c")


def test_round_cap_defaults_to_twelve_and_is_clamped() -> None:
    """批次四十六：默认 6 → **12**，上限 20 → **50**（轮数降格成安全阀）。"""

    def group(settings) -> CollaborationSession:
        return CollaborationSession.create(title="组").evolve(settings=settings)

    assert DEFAULT_ROUND_CAP == 12
    assert group({}).round_cap == 12
    assert group({"roundCap": 2}).round_cap == 2
    assert group({"roundCap": 999}).round_cap == 50
    assert group({"roundCap": 0}).round_cap == 1
    assert group({"roundCap": "六"}).round_cap == DEFAULT_ROUND_CAP
    # 现有组的旧值照旧（迁移不动数据）：库里存着 6 就还是 6。
    assert group({"roundCap": 6}).round_cap == 6


# --------------------------------------------------------------------------- #
# 4. 路由：一条完整的线程
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.api.group_router import reconcile_room_threads  # noqa: E402
from app.tests.test_batch22_groups import (  # noqa: E402
    GroupHarness,
    _api,
    _new_conversation,
    _new_group,
    _seeded,
)
from app.tests.test_batch26_group_routing import _join  # noqa: E402
from drivers.mock.fixtures import hold_script, text_stream_script  # noqa: E402


def _script_plan(built: GroupHarness, plan: dict[str, list[str]]) -> None:
    """给假引擎排一份**按轮次**的剧本：这条会话第 n 次被投递时说 ``plan[n]``。

    Mock Driver 自带的 ``set_script`` 是「一条会话一份剧本」，每一轮都重放同一段
    ——房间循环要验的恰恰是「第 2 轮说的和第 1 轮不一样」（比如第 2 轮有人收口），
    所以这里换掉取剧本那一步。``run_id`` 每轮不同，否则幂等键会把第 2 轮当成第 1
    轮的重放。
    """
    turns: dict[str, int] = {}
    original = built.driver.script_for

    def script_for(conversation_id: str):
        lines = plan.get(conversation_id) or plan.get(built.source_for(conversation_id))
        if not lines:
            return original(conversation_id)
        index = turns.get(conversation_id, 0)
        turns[conversation_id] = index + 1
        text = lines[min(index, len(lines) - 1)]
        tail = conversation_id.rsplit("-", 1)[-1]
        return text_stream_script(
            run_id=f"run-{tail}-{index}",
            message_id=f"msg-{tail}-{index}",
            chunks=(text,),
        )

    built.driver.script_for = script_for  # type: ignore[method-assign]


async def _delivered(built: GroupHarness, conversation_id: str) -> list[str]:
    """这条会话**真的被投到**的每一份文本（``kaus/user.message`` 的正文）。

    刻意不看「这条会话上有没有事件」：成员一加入，组变更那条 ``kaus/group.changed``
    就已经进了他的会话流——那不是投递。
    """
    envelopes = await built.host.event_store.replay(built.group_session_for(conversation_id))
    return [
        envelope.event.data.get("text") or ""
        for envelope in envelopes
        if getattr(envelope.event, "type", None) == "extension.event"
        and getattr(envelope.event, "name", None) == "user.message"
    ]


async def _messages(api, group_id: str) -> list[dict]:
    """组时间线（**按时间正序**，读起来就是发生的顺序）。"""
    rows = (await api.get(f"/api/groups/{group_id}/messages")).json()["messages"]
    return list(reversed(rows))


async def _wait_until(check, *, timeout: float = 6.0, what: str = "条件"):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        outcome = await check()
        if outcome:
            return outcome
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等不到{what}")
        await asyncio.sleep(0.02)


async def _wait_turns(api, group_id: str, count: int):
    """时间线上的 ``member_turn`` 攒够 ``count`` 条了吗。"""
    turns = [row for row in await _messages(api, group_id) if row["kind"] == "member_turn"]
    return turns if len(turns) >= count else None


async def _thread_of(api, group_id: str) -> dict | None:
    return (await api.get(f"/api/groups/{group_id}")).json()["group"].get("thread")


async def _settled(api, group_id: str, *, what: str = "房间停下来"):
    """等线程走到一个终态（不再 running）。"""

    async def check():
        thread = await _thread_of(api, group_id)
        # 批次四十六：`closing` 也还在等人（收口人），不算停下来。
        return (
            thread
            if thread and thread["status"] not in ("running", "closing")
            else None
        )

    return await _wait_until(check, what=what)


async def test_a_whole_thread_runs_and_the_leader_closes_it(tmp_path) -> None:
    """L3：发一句「你们讨论一下」→ 三个人轮流说 → **组长**收口 → 停。

    **批次四十八改写**：组长是最早加入的那位（写手），而它永远排队尾——所以第 1 轮的
    顺序是 评审、打杂、写手，不再是加入顺序。第 1 轮组长点了评审，于是第 2 轮是
    评审 + 组长；组长第 2 轮以「最终答复」开头，房间停（终止 2）。
    """
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        gamma = await _new_conversation(built, "打杂")
        _script_plan(
            built,
            {
                alpha.id: ["我看了，@评审 你再说一次", "最终答复：就按方案二。"],
                beta.id: ["我不同意写手", "我补充一句"],
                gamma.id: ["（略过）", "（略过）"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "会转的房间")
            members = [
                await _join(api, group["id"], conversation.id)
                for conversation in (alpha, beta, gamma)
            ]
            leader, reviewer, odd = members
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "你们讨论一下这个问题，最后给我一个结论"},
            )
            thread = await _settled(api, group["id"], what="组长收口")

            assert thread["status"] == "final"
            assert thread["endedReason"] == "leader_final"
            assert thread["epoch"] == 1
            rows = await _messages(api, group["id"])
            turns = [row for row in rows if row["kind"] == "member_turn"]
            # 第 1 轮三个人各一条（组长最后）、第 2 轮评审与组长各一条。
            assert [turn["round"] for turn in turns] == [1, 1, 1, 2, 2]
            assert [turn["authorMemberId"] for turn in turns] == [
                reviewer["id"],
                odd["id"],
                leader["id"],
                reviewer["id"],
                leader["id"],
            ]
            # 收口那一条标着 final=true，界面据它渲染高亮卡。
            assert turns[-1]["final"] is True
            assert turns[-1]["text"].startswith("最终答复")
            # 略过那一条标着 passed=true，界面据它折叠。
            assert turns[1]["passed"] is True
            # L8：每一条都答得出第几轮、投给他的那段增量从哪算起，
            # 而 epoch 的起点就是用户那条消息。
            for turn in turns:
                assert turn["epoch"] == 1
                assert "deliveredSince" in turn and "deliveredTo" in turn
            posted = [row for row in rows if row["kind"] == "broadcast"][0]
            assert posted["epoch"] == 1
            assert len(posted["deliveries"]) == 5  # 五次投递，都记在同一行上
            assert {row["status"] for row in posted["deliveries"]} == {"sent"}
    finally:
        await built.aclose()


async def test_the_room_never_carries_a_members_private_body(tmp_path) -> None:
    """AD-154：投给下一位的那一段只含**组时间线上已经公开**的内容。

    读的是**组长**收到的那一份（它排队尾，所以别人说过的话都该在它的增量里）。
    """
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {alpha.id: ["（略过）", "（略过）"], beta.id: ["公开的结论", "（略过）"]},
        )
        async with _api(built) as api:
            group = await _new_group(api, "两个人的房间")
            await _join(api, group["id"], alpha.id)  # 最早加入 → 组长 → 排队尾
            await _join(api, group["id"], beta.id)
            # 一份只属于这个组、不属于任何成员的私有汇编：它在库里，但**不该**出现
            # 在任何一次投递里。
            from app.collaboration.models import CollaborationMessage

            sequence = await built.repositories.group_messages.next_sequence(group["id"])
            await built.repositories.group_messages.append(
                CollaborationMessage.create(
                    collaboration_session_id=group["id"],
                    sequence=sequence,
                    kind="context_packet",
                    author_type="system",
                    content="【私有】写手那条会话的完整历史",
                )
            )
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "各位说一句"}
            )
            await _settled(api, group["id"])

            delivered = await _delivered(built, alpha.id)
            assert delivered, "组长一次都没被投到"
            for text in delivered:
                assert "【私有】" not in text
                # 公开的那条在（房间就是靠它转的），私有的那条不在。
                assert ROOM_HEADER_SEPARATOR in text
            assert any("公开的结论" in text for text in delivered)
    finally:
        await built.aclose()


async def test_stopping_the_thread_stops_the_next_delivery(tmp_path) -> None:
    """L5：「停」立刻生效；停后成员不再收到投递。

    剧本刻意让**队尾那位（组长）停在半路**（``hold``）：那正是真机上按「停」的那
    一刻——一个人正跑着。断言分两半：他这一轮的回复照记（房间不去打断引擎，那会丢掉
    一段已经付过钱的正文），而队列不再往下走（评审拿不到第二轮）。
    """
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(built, {beta.id: ["说一句", "第二轮不该来"]})
        built.driver.set_script(alpha.id, hold_script())
        async with _api(built) as api:
            group = await _new_group(api, "会停的房间")
            await _join(api, group["id"], alpha.id)  # 组长，排队尾
            await _join(api, group["id"], beta.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "各位说一句"}
            )
            # 等房间转到组长身上（它会一直跑着）。
            await _wait_until(
                lambda: _delivered(built, alpha.id), what="房间转到组长"
            )

            stopped = (await api.post(f"/api/groups/{group['id']}/thread/stop")).json()
            assert stopped["thread"]["status"] == "stopped"
            assert stopped["thread"]["awaitingMemberId"] is None

            # 正在跑的那位被打断 → 他这一轮**照记**一行（带 outcome）。
            await built.host.interrupt(built.group_session_for(alpha.id))
            await _wait_until(
                lambda: _wait_turns(api, group["id"], 2), what="被打断那一轮记上账"
            )
            await asyncio.sleep(0.3)

            rows = await _messages(api, group["id"])
            turns = [row for row in rows if row["kind"] == "member_turn"]
            assert len(turns) == 2
            assert turns[-1]["outcome"] == "interrupted"
            # 停了，所以评审没有第二轮——投递总数停在两次。
            assert len(await _delivered(built, beta.id)) == 1
            assert (await _thread_of(api, group["id"]))["status"] == "stopped"
    finally:
        await built.aclose()


async def test_the_round_cap_forces_a_closing_turn(tmp_path) -> None:
    """L6（批次四十八改写）：``roundCap`` 设 2 → 两轮之后**让组长收口**。

    组长每轮都还在点人（``@评审``），所以终止 3 不生效，房间一路撞到安全阀——那正是
    这个阀门要防的情形。到顶不是干停：单独投给组长一条「到达安全上限…只给结论」，
    用户拿到的是一张最终答复卡，不是一条道歉。
    """
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {
                alpha.id: ["@评审 你接着说", "@评审 再说一次", "收口：就按这个来。"],
                beta.id: ["第一轮", "第二轮"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "转不完的房间")
            await _join(api, group["id"], alpha.id)  # 组长
            await _join(api, group["id"], beta.id)
            patched = await api.patch(
                f"/api/groups/{group['id']}", json={"roundCap": 2}
            )
            assert patched.status_code == 200
            assert patched.json()["group"]["settings"]["roundCap"] == 2

            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "说说"})
            thread = await _settled(api, group["id"], what="收口跑完")

            assert thread["status"] == "final"
            assert thread["phase"] == "closing"
            # 收口那一轮**不计入 round**：两轮讨论 + 一次收口。
            assert thread["round"] == 2
            rows = await _messages(api, group["id"])
            turns = [r for r in rows if r["kind"] == "member_turn"]
            assert len(turns) == 5
            assert turns[-1]["final"] is True
            assert turns[-1]["phase"] == "closing"
            assert turns[-1]["text"] == "收口：就按这个来。"
            # 上限那一句说的是「到顶了、已经让组长去收口」。
            system = [r for r in rows if r["kind"] == "system"]
            assert system[-1]["text"] == "到达安全上限（2 轮），已让 写手 收口"
    finally:
        await built.aclose()


async def test_an_out_of_range_round_cap_is_refused(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            bad = await api.patch(f"/api/groups/{group['id']}", json={"roundCap": 51})
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "invalid_round_cap"
    finally:
        await built.aclose()


async def test_a_round_of_nothing_but_passes_stops_the_room(tmp_path) -> None:
    """L7：全员「（略过）」→ 房间停，那两条在时间线上标着 ``passed``。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(built, {alpha.id: ["（略过）"], beta.id: ["[pass]"]})
        async with _api(built) as api:
            group = await _new_group(api, "没人有话说的房间")
            await _join(api, group["id"], alpha.id)
            await _join(api, group["id"], beta.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "还有事吗"})
            thread = await _settled(api, group["id"], what="全员略过之后停下来")

            assert thread["status"] == "idle"
            assert thread["round"] == 1  # 没有进第二轮
            rows = await _messages(api, group["id"])
            turns = [row for row in rows if row["kind"] == "member_turn"]
            assert len(turns) == 2
            assert all(turn["passed"] is True for turn in turns)
    finally:
        await built.aclose()


async def test_naming_two_members_leaves_the_third_a_spectator(tmp_path) -> None:
    """L4：「写手 和 评审 辩论」→ 只有他们俩说话；打杂被点到时接得上。

    写手是组长（最早加入），所以队列是 评审 → 写手：被点名的两位都在，组长仍旧最后。
    """
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        gamma = await _new_conversation(built, "打杂")
        _script_plan(
            built,
            {
                alpha.id: ["最终答复：我说完了。"],
                beta.id: ["我先说我的看法"],
                gamma.id: ["我来总结"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "辩论房间")
            leader = await _join(api, group["id"], alpha.id)
            reviewer = await _join(api, group["id"], beta.id)
            spectator = await _join(api, group["id"], gamma.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "写手 和 评审，你们辩论一下这个问题"},
            )
            thread = await _settled(api, group["id"], what="辩论收口")
            assert thread["speakerQueue"] == [reviewer["id"], leader["id"]]
            assert thread["spectators"] == [spectator["id"]]
            # 旁观者这一轮**一次引擎都没跑**——他明说了不说话。
            assert await _delivered(built, gamma.id) == []

            # 现在点他的名：他这一次收到的增量里有写手刚才说的话（旁观增量生效）。
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "打杂 你总结一下"},
            )
            await _settled(api, group["id"], what="总结那一轮跑完")
            delivered = await _delivered(built, gamma.id)
            assert delivered
            assert "最终答复：我说完了。" in delivered[0]
    finally:
        await built.aclose()


async def test_a_new_user_message_replaces_a_running_thread(tmp_path) -> None:
    """用户新消息 → 无论当前状态都开新 epoch，旧线程不再推进。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        _script_plan(built, {alpha.id: ["第一轮", "第二轮"]})
        async with _api(built) as api:
            group = await _new_group(api, "被打断的房间")
            await _join(api, group["id"], alpha.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "第一句"})
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "第二句"})
            thread = await _thread_of(api, group["id"])
            assert thread["epoch"] == 2
            assert thread["startedByMessageId"] is not None
            rows = await _messages(api, group["id"])
            posted = [row for row in rows if row["kind"] == "broadcast"]
            assert [row["epoch"] for row in posted] == [1, 2]
    finally:
        await built.aclose()


async def test_a_running_thread_is_stopped_when_the_process_comes_back(
    tmp_path,
) -> None:
    """重启收尾：库里那条 ``running`` 不该继续假装还在转。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        async with _api(built) as api:
            group = await _new_group(api, "重启前的房间")
            member = await _join(api, group["id"], alpha.id)
            row = await built.repositories.collaborations.get(group["id"])
            await built.repositories.collaborations.save(
                row.evolve(
                    thread=RoomThread(
                        epoch=1,
                        round=2,
                        status="running",
                        speaker_queue=(member["id"],),
                        awaiting_member_id=member["id"],
                    )
                )
            )

            touched = await reconcile_room_threads(built.repositories)
            assert touched == (group["id"],)

            thread = await _thread_of(api, group["id"])
            assert thread["status"] == "stopped"
            assert thread["awaitingMemberId"] is None
            rows = await _messages(api, group["id"])
            assert rows[-1]["kind"] == "system"
            assert "重启" in rows[-1]["text"]
            # 再跑一次是一次无操作：已经收过尾的组不该每次启动都多一行系统消息。
            assert await reconcile_room_threads(built.repositories) == ()
    finally:
        await built.aclose()
