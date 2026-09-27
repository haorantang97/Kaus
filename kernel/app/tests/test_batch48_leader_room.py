"""批次四十八：三合一在组长（PRD §A3/§A5/§A6/§B4/§B6，2026-09-18 第四次修订）。

为什么有这一批
--------------
批次四十六、四十七做的「每人每轮写 ``[状态]``、全员可收口才停、可收口粘性、``@``
召回」是**六家参考项目都没有的投票制**（``docs/audits/termination-comparison-
2026-09-14.md`` §7 三条硬结论：零举手、零投票、收口人=判定人=选人者三合一）。这一批
把三合一落在**组长**身上，并删掉整套表态机制。

覆盖
----
1. 组长的生命周期：加入自动指定 / ``PATCH`` 换 / 暂停与离开自动移交 / 老组补位；
2. 队列：末尾恒为组长（点没点它都一样）；
3. :func:`next_round_queue`：去重与顺序、``@所有人``、剔除非 active、组长在末尾；
4. 终止 2（组长最终答复）、终止 3（组长不点人就停，``round == 1`` 同此）、
   终止 4（触顶投组长）、非组长的「最终答复」被忽略；
5. 端到端：「随便挑两个」（组长第一轮点两人 → 第二轮只有那两人 + 组长）与
   「举手」（B 反驳 A 但不提名，组长 ``@A`` → 第三轮 A 在队列里）。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据、不起任何真进程。
"""

from __future__ import annotations

import asyncio

import pytest

from app.collaboration.models import DEFAULT_ROUND_CAP, RoomThread
from app.collaboration.room import (
    LEADER_NOTE,
    ROOM_CONVENTIONS,
    ROOM_CONVENTIONS_NO_LEADER,
    MemberTurn,
    RoomMember,
    advance_thread,
    next_round_queue,
    resolve_everyone,
    room_delta,
    start_thread,
)

A = RoomMember("member:a", "写手", "hermes", True)
B = RoomMember("member:b", "评审", "dsh", True)
C = RoomMember("member:c", "打杂", None, True)
ROOM = (A, B, C)
ACTIVE = ("member:a", "member:b", "member:c")
LEADER = "member:c"


# --------------------------------------------------------------------------- #
# 1. 纯函数：@所有人 / 下一轮队列 / 队尾恒为组长
# --------------------------------------------------------------------------- #


def test_everyone_is_two_literal_spellings_with_an_at_sign() -> None:
    """``@所有人`` 是一条「别按名字算」的指令，所以它认的是**封闭的两种写法**。

    裸的「所有人」不认：它在中文里太常出现（「我问过所有人了」），认它等于把一句
    叙述读成一条指令——与停止词那张表同一条口径。
    """
    assert resolve_everyone("@所有人 都来说一句")
    assert resolve_everyone("ok @everyone")
    assert not resolve_everyone("我问过所有人了")
    assert not resolve_everyone("")


def test_next_round_queue_ignores_mentions_written_by_members() -> None:
    """**批次五十（裁决 B）**：成员写的 `@` 不进下一轮队列。

    真机第一份记录里三场讨论的评审与打杂每一轮结尾都 `@下一位`——把「想让谁接着
    说就写 @名字」读成了传话筒。「三者同权」于是让队列永远非空，终止 3 一次都没
    到过。这一条钉的就是那件事：两位成员点了两个人，**一个都不算数**，队列里只剩
    组长（于是房间停，见 :func:`advance_thread` 的终止 3）。
    """
    turns = (
        MemberTurn("member:a", "我同意 @打杂 的看法"),
        MemberTurn("member:b", "@写手 你再说一次，@打杂 也补一句"),
    )
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (LEADER,)


def test_next_round_queue_takes_only_the_leader_turn() -> None:
    """组长点谁，谁下一轮说；同一轮里成员点的人不掺进来。

    顺序按**组长写名字的次序**：这一层回答的是「组长打算让谁接着说」。
    """
    turns = (
        MemberTurn("member:a", "@评审 你来"),  # 不算数
        MemberTurn(LEADER, "@写手 你先说，然后 @评审"),
    )
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (
        "member:a",
        "member:b",
        LEADER,
    )


def test_next_round_queue_puts_the_leader_last_even_if_it_names_itself() -> None:
    """组长**永远**是队尾：它开口时必须已经读完这一轮所有人的话。"""
    turns = (MemberTurn(LEADER, "@打杂 @评审 你们俩来"),)
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == ("member:b", LEADER)


def test_next_round_queue_expands_everyone_to_all_active() -> None:
    """``@所有人`` 也只认组长写的。"""
    turns = (MemberTurn(LEADER, "@所有人 都说一句"),)
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (
        "member:a",
        "member:b",
        LEADER,
    )


def test_next_round_queue_ignores_everyone_written_by_a_member() -> None:
    """成员喊一句 ``@所有人`` 不能把全组拉起来——那是路由，路由只在组长手里。"""
    turns = (MemberTurn("member:a", "@所有人 都说一句"),)
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (LEADER,)


def test_next_round_queue_drops_members_who_are_no_longer_active() -> None:
    """暂停 / 离开的人被点到也不进队列：那是一个永远投不出去的位置。"""
    turns = (MemberTurn(LEADER, "@评审 @写手 你们接着"),)
    assert next_round_queue(turns, ROOM, LEADER, ("member:a", LEADER)) == (
        "member:a",
        LEADER,
    )


def test_next_round_queue_is_empty_when_the_leader_said_nothing() -> None:
    """组长略过 / 投递失败 → 本轮没有它那一条 → 队列为空（终止 3 / 5）。"""
    turns = (MemberTurn("member:a", "@评审 你接着"),)
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (LEADER,)
    assert next_round_queue((), ROOM, LEADER, ACTIVE) == (LEADER,)


def test_next_round_queue_without_a_leader_is_empty() -> None:
    """没有组长 = 没有人能点名（批次五十）：房间一轮即停。

    第四次修订时这一格是「成员点到的那几位」，现在它是空的——成员的点名不算数，
    而那个唯一算数的人不在。
    """
    turns = (MemberTurn("member:a", "@评审 你接着"),)
    assert next_round_queue(turns, ROOM, None, ("member:a", "member:b")) == ()


def test_the_leader_is_appended_even_when_the_user_named_somebody_else() -> None:
    """PRD §A3：「你点名了就只有被点的人说」——**但组长仍旧排在最后**。

    房间永远要有一个读完全场的人，否则「什么时候停」就又回到每个人自己身上了。
    """
    thread = start_thread(
        previous=None,
        speaker_queue=("member:a", "member:b"),
        spectators=(LEADER,),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )
    assert thread.speaker_queue == ("member:a", "member:b", LEADER)
    assert thread.spectators == ()  # 组长进了队列，就不再是旁观者


def test_naming_the_leader_does_not_move_it_out_of_the_tail() -> None:
    """用户完全可能在句子中间写组长的名字。摘掉再追加，位置不变。"""
    thread = start_thread(
        previous=None,
        speaker_queue=(LEADER, "member:a"),
        spectators=(),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )
    assert thread.speaker_queue == ("member:a", LEADER)


def test_the_three_room_notices_are_verbatim() -> None:
    """房间说明那三段是**逐字**的（批次五十 / 裁决 B）。

    它们是这一版唯一写给模型看的东西，而真机证明模型会照着约定的字面做事（第四次
    修订那句「想让谁接着说就写 @名字」被三场讨论一致读成了传话筒）。所以这里钉的
    不是实现，是**文案本身**：谁想顺手润一句，得先在这儿把它改成另一句。
    """
    assert ROOM_CONVENTIONS == (
        "约定：发言顺序由组长安排，不用在结尾 @ 下一位。"
        "要让谁回应你、或你还想再说一次，在正文里写明（可以写 @名字），"
        "组长会看到并决定下一轮谁说。"
        "没有新内容就只回「（略过）」。组长每轮最后发言。"
    )
    # 没有组长时队列恒空、房间一轮即停，所以这一份不教任何点名的写法。
    assert ROOM_CONVENTIONS_NO_LEADER == "约定：没有新内容就只回「（略过）」。"
    assert LEADER_NOTE == (
        "你是组长，本轮最后发言。读完全场后："
        "还有分歧、没答完的点、或有人请求回应 → 写 @名字 点下一轮该说的人"
        "（只有你的 @ 算数，可以 @所有人）；"
        "已经收敛 → 以「最终答复」开头给结论，或谁也不点（你这一段就是结论）。"
        "别为了礼貌点人。"
        # 批次五十一：路由只认显式 `@`，所以这一句必须在它读到的文本里。
        "只有写了 @ 的名字才算点名，正文里提到的名字不算。"
    )
    # 约定这一段与「组长叫什么」无关了：它不再往里填显示名（第四次修订填过）。
    assert "{" not in ROOM_CONVENTIONS


def test_a_room_without_a_leader_only_gets_the_pass_convention() -> None:
    """没有组长的房间：约定只剩「略过」，也没有那段只给组长的话。"""
    delta = room_delta(
        group_title="组",
        members=ROOM,
        recipient_member_id="member:a",
        speaker_ids=("member:a",),
        spectator_ids=(),
        entries=(),
        leader_member_id=None,
    )
    assert delta is not None
    assert delta.splitlines()[-1] == ROOM_CONVENTIONS_NO_LEADER
    assert LEADER_NOTE not in delta


def test_the_leader_gets_an_extra_line_in_its_own_copy() -> None:
    """PRD §B6：投给组长的那一份多一段「你是组长：…」，别人那份没有。"""
    for recipient, expected in (("member:a", False), (LEADER, True)):
        delta = room_delta(
            group_title="组",
            members=ROOM,
            recipient_member_id=recipient,
            speaker_ids=ACTIVE,
            spectator_ids=(),
            entries=(),
            leader_member_id=LEADER,
        )
        assert delta is not None
        assert ("组长：打杂。" in delta) is True
        assert (LEADER_NOTE in delta) is expected


# --------------------------------------------------------------------------- #
# 2. 终止梯子（纯状态机）
# --------------------------------------------------------------------------- #


def _thread() -> RoomThread:
    return start_thread(
        previous=None,
        speaker_queue=("member:a", "member:b"),
        spectators=(),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )


def _say(thread: RoomThread, member_id: str, **changes) -> RoomThread:
    options = {
        "passed": False,
        "final": False,
        "next_queue": (),
        "active_member_ids": ACTIVE,
        "leader_member_id": LEADER,
        "round_cap": DEFAULT_ROUND_CAP,
    }
    options.update(changes)
    return advance_thread(thread, member_id=member_id, **options)  # type: ignore[arg-type]


def test_termination_2_the_leader_writes_a_final_reply() -> None:
    thread = _say(_thread(), "member:a", final=True)
    assert (thread.status, thread.ended_reason) == ("final", "leader_final")


def test_termination_3_the_leader_names_nobody() -> None:
    """一轮走完、下一轮除组长外没人 → 房间停，组长那一段就是结论。"""
    thread = _thread()
    for member_id in ("member:a", "member:b"):
        thread = _say(thread, member_id)
    thread = _say(thread, LEADER, next_queue=(LEADER,))
    assert (thread.status, thread.ended_reason) == ("final", "leader_closed")
    assert thread.round == 1  # 「介绍一下自己」那种一轮完事的问题


def test_termination_3_also_fires_on_the_very_first_round() -> None:
    """``round == 1`` 同此（PRD §B4 终止 3 末句）：没有「至少两轮」这条门槛了。

    批次四十六那条 ``MIN_ROUNDS_BEFORE_CLOSING`` 是为收口轮设的——收口要另投一次
    引擎，一轮就问完的问题不值得。这一版组长本来就在最后说，它那一段**已经**是
    答案，所以门槛整条删掉。
    """
    solo = start_thread(
        previous=None,
        speaker_queue=(),
        spectators=(),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )
    assert solo.speaker_queue == (LEADER,)
    thread = _say(solo, LEADER, next_queue=(LEADER,))
    assert (thread.status, thread.ended_reason, thread.round) == (
        "final",
        "leader_closed",
        1,
    )


def test_termination_4_hitting_the_cap_hands_the_closing_to_the_leader() -> None:
    thread = _thread()
    keeps_going = ("member:a", LEADER)
    for member_id in ("member:a", "member:b", LEADER):
        thread = _say(thread, member_id, next_queue=keeps_going, round_cap=1)
    assert (thread.status, thread.phase) == ("closing", "closing")
    assert thread.ended_reason == "round_cap"
    assert thread.awaiting_member_id == LEADER
    # 收口那一轮跑完 → final，且**不计入 round**。
    done = _say(thread, LEADER)
    assert (done.status, done.ended_reason, done.round) == ("final", "closed", 1)


def test_termination_4_without_a_leader_says_so_instead_of_picking_somebody() -> None:
    """没有组长时不硬找一个人来收口（PRD §B4 终止 6）。"""
    thread = start_thread(
        previous=None,
        speaker_queue=("member:a",),
        spectators=(),
        started_by_message_id="groupmsg:1",
    )
    ended = advance_thread(
        thread,
        member_id="member:a",
        passed=False,
        final=False,
        next_queue=("member:a",),
        active_member_ids=("member:a",),
        leader_member_id=None,
        round_cap=1,
    )
    assert (ended.status, ended.ended_reason) == ("idle", "leader_missing")


def test_a_closing_turn_that_fails_stops_without_retrying() -> None:
    """重试等于再花一次钱去赌一件刚刚失败的事（PRD §B4 终止 4 末句）。"""
    closing = _thread().evolve(status="closing", phase="closing")
    ended = _say(closing, LEADER, outcome="failed")
    assert (ended.status, ended.ended_reason) == ("stopped", "closing_failed")


# --------------------------------------------------------------------------- #
# 3. 路由：组长的生命周期与两条端到端剧本
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
from app.tests.test_batch45b_room_loop import (  # noqa: E402
    _messages,
    _script_plan,
    _settled,
    _thread_of,
)


async def _members_of(api, group_id: str) -> list[dict]:
    return (await api.get(f"/api/groups/{group_id}")).json()["members"]


async def test_the_first_active_member_becomes_the_leader(tmp_path) -> None:
    """PRD §A6：默认是最早加入的那位——那正是前三版里悄悄写最终答复的「队首」。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        async with _api(built) as api:
            group = await _new_group(api, "有组长的房间")
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)

            detail = (await api.get(f"/api/groups/{group['id']}")).json()
            assert detail["group"]["leaderMemberId"] == first["id"]
            flags = {row["id"]: row["isLeader"] for row in detail["members"]}
            assert flags == {first["id"]: True, second["id"]: False}
            rows = await _messages(api, group["id"])
            assert rows[1]["text"] == "写手 是组长"
    finally:
        await built.aclose()


async def test_patching_the_leader_needs_an_active_member(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        async with _api(built) as api:
            group = await _new_group(api, "换组长")
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)

            ok = await api.patch(
                f"/api/groups/{group['id']}", json={"leaderMemberId": second["id"]}
            )
            assert ok.status_code == 200
            assert ok.json()["group"]["leaderMemberId"] == second["id"]
            rows = await _messages(api, group["id"])
            assert rows[-1]["text"] == "评审 是组长"

            # 暂停之后他就不是合法的组长人选了。
            await api.post(f"/api/groups/{group['id']}/members/{first['id']}/pause")
            bad = await api.patch(
                f"/api/groups/{group['id']}", json={"leaderMemberId": first["id"]}
            )
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "leader_not_active"
            # 不存在的成员同样是这一条（不泄漏别的组的存在）。
            ghost = await api.patch(
                f"/api/groups/{group['id']}", json={"leaderMemberId": "member:nobody"}
            )
            assert ghost.status_code == 400
            assert ghost.json()["error"]["code"] == "leader_not_active"
    finally:
        await built.aclose()


async def test_a_leader_that_leaves_hands_over_to_the_next_earliest(tmp_path) -> None:
    """PRD §B4 终止 6：自动移交给**最早加入的其他 active 成员**，并说一句。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        gamma = await _new_conversation(built, "打杂")
        async with _api(built) as api:
            group = await _new_group(api, "组长会走的房间")
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            third = await _join(api, group["id"], gamma.id)

            await api.delete(f"/api/groups/{group['id']}/members/{first['id']}")
            detail = (await api.get(f"/api/groups/{group['id']}")).json()
            assert detail["group"]["leaderMemberId"] == second["id"]
            rows = await _messages(api, group["id"])
            assert rows[-1]["text"] == "写手 不在了，评审 接任组长"

            # 再暂停一位 → 再移交；最后一位也走 → 组长位空着（不硬找一个人）。
            await api.post(f"/api/groups/{group['id']}/members/{second['id']}/pause")
            assert (await api.get(f"/api/groups/{group['id']}")).json()["group"][
                "leaderMemberId"
            ] == third["id"]
            await api.post(f"/api/groups/{group['id']}/members/{third['id']}/pause")
            assert (await api.get(f"/api/groups/{group['id']}")).json()["group"][
                "leaderMemberId"
            ] is None
    finally:
        await built.aclose()


async def test_an_old_group_without_a_leader_gets_one_on_startup(tmp_path) -> None:
    """兼容：批次四十八之前建的组库里那一格是 NULL，而房间从此靠组长收口。"""
    built = await _seeded(tmp_path)
    try:
        alpha = await _new_conversation(built, "写手")
        async with _api(built) as api:
            group = await _new_group(api, "老组")
            member = await _join(api, group["id"], alpha.id)
            # 把它改回「没有组长」的样子（= 迁移前的存量行）。
            row = await built.repositories.collaborations.get(group["id"])
            await built.repositories.collaborations.save(
                row.evolve(leader_member_id=None)
            )

            await reconcile_room_threads(built.repositories, coordinator_enabled=False)

            detail = (await api.get(f"/api/groups/{group['id']}")).json()
            assert detail["group"]["leaderMemberId"] == member["id"]
            rows = await _messages(api, group["id"])
            assert rows[-1]["text"] == "写手 是组长"
    finally:
        await built.aclose()


async def test_the_leader_picks_two_and_the_next_round_is_just_them(tmp_path) -> None:
    """PRD §A3 末条：「你们随便挑两个人辩论」——房间不猜，组长挑。

    三人组（外加组长自己）：用户那句话一个名字都没有 → 全员进队列、组长最后。组长
    第一轮说「我抽写手和评审」→ 第二轮只有那两位 + 组长。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        gamma = await _new_conversation(built, "打杂")
        _script_plan(
            built,
            {
                boss.id: ["我抽 @写手 @评审 你们俩来", "最终答复：方案二。"],
                alpha.id: ["我先说", "我还是那个意见"],
                beta.id: ["我也说一句", "我同意写手"],
                gamma.id: ["我也想说", "不该轮到我"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "随便挑两个")
            leader = await _join(api, group["id"], boss.id)
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            third = await _join(api, group["id"], gamma.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "你们随便挑两个人辩论一下"},
            )
            await _settled(api, group["id"], what="组长收口")

            turns = [r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"]
            assert [t["authorMemberId"] for t in turns] == [
                first["id"],
                second["id"],
                third["id"],
                leader["id"],  # 第 1 轮：全员 + 组长最后
                first["id"],
                second["id"],
                leader["id"],  # 第 2 轮：只有被抽中的两位 + 组长
            ]
            assert [t["round"] for t in turns] == [1, 1, 1, 1, 2, 2, 2]
            assert turns[-1]["final"] is True
    finally:
        await built.aclose()


async def test_raising_a_hand_goes_through_the_leader(tmp_path) -> None:
    """L16 举手（批次五十改写）：**B 在正文里写 `@写手`，也要组长点了才算数。**

    第 2 轮评审写了 `@写手`，而组长那一轮谁也没点 → 房间就停了，写手不说话。这正是
    裁决 B 要的形状：成员的 `@` 是一条请求，路由在组长手里。下一条用例走另一半
    （组长点了，写手就回来）。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {
                boss.id: ["@评审 你先说", "我听明白了。"],
                alpha.id: ["我的方案是甲", "我坚持甲"],
                beta.id: ["上面那个方案有问题", "@写手 你回应一下我"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "举手的房间")
            leader = await _join(api, group["id"], boss.id)
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "这个问题怎么办"}
            )
            thread = await _settled(api, group["id"], what="组长收口")

            turns = [r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"]
            by_round: dict[int, list[str]] = {}
            for turn in turns:
                by_round.setdefault(turn["round"], []).append(turn["authorMemberId"])
            # 第 1 轮全员；第 2 轮组长只点了评审；评审举了手，组长没接 → 没有第 3 轮。
            assert by_round[1] == [first["id"], second["id"], leader["id"]]
            assert by_round[2] == [second["id"], leader["id"]]
            assert 3 not in by_round
            assert thread["endedReason"] == "leader_closed"
            # 举的那只手在时间线上看得见：他点了写手，而写手没有第 3 轮。
            hand = [t for t in turns if t["round"] == 2][0]
            assert hand["mentions"] == [first["id"]]
    finally:
        await built.aclose()


async def test_the_leader_can_answer_a_raised_hand(tmp_path) -> None:
    """L16 的另一半：组长**点了**写手 → 第 3 轮写手在队列里。

    两条路径都对；区别只在组长那一段里有没有那个名字。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {
                boss.id: ["@评审 你先说", "@写手 你回应一下他", "最终答复：写手是对的。"],
                alpha.id: ["我的方案是甲", "我坚持甲"],
                beta.id: ["上面那个方案有问题", "好吧"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "接得住举手的房间")
            leader = await _join(api, group["id"], boss.id)
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "这个问题怎么办"}
            )
            await _settled(api, group["id"], what="组长收口")

            turns = [r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"]
            by_round: dict[int, list[str]] = {}
            for turn in turns:
                by_round.setdefault(turn["round"], []).append(turn["authorMemberId"])
            assert by_round[1] == [first["id"], second["id"], leader["id"]]
            assert by_round[2] == [second["id"], leader["id"]]
            assert by_round[3] == [first["id"], leader["id"]]
            assert turns[-1]["final"] is True
    finally:
        await built.aclose()


async def test_members_passing_the_baton_no_longer_keep_the_room_running(
    tmp_path,
) -> None:
    """**真机那三场的复现**（`docs/quality/reports/2026-09-19-group/REPORT.md` §3）。

    评审与打杂每一轮结尾都 `@下一位`（评审→`@打杂`、打杂→`@写手`），组长第一轮
    谁也不点。第四次修订下这些接力 `@` 会把人塞进第 2 轮，终止 3 永远到不了；裁决
    B 之后队列只认组长那条 → **房间一轮即停**。

    时间线上那两只手仍旧看得见（`mentions` 有名字），只是没生效——L20 验的就是
    「有名字、但下一轮没有他」这两件事同时成立。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        gamma = await _new_conversation(built, "打杂")
        _script_plan(
            built,
            {
                boss.id: ["三位都介绍过了。", "不该有第 2 轮"],
                beta.id: ["我负责挑错与验收。\n\n@打杂", "不该有第 2 轮"],
                gamma.id: ["我负责跑工具、验结果。\n\n@写手", "不该有第 2 轮"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "传话筒")
            leader = await _join(api, group["id"], boss.id)
            second = await _join(api, group["id"], beta.id)
            third = await _join(api, group["id"], gamma.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "你们各自介绍一下自己"},
            )
            thread = await _settled(api, group["id"], what="房间停下来")

            assert thread["status"] == "final"
            assert thread["endedReason"] == "leader_closed"
            turns = [r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"]
            # 只有第 1 轮，三条：接力的 `@` 一个都没把房间往下推。
            assert [t["round"] for t in turns] == [1, 1, 1]
            assert [t["authorMemberId"] for t in turns] == [
                second["id"],
                third["id"],
                leader["id"],
            ]
            assert turns[-1]["final"] is True
            # 评审点了打杂、打杂点了写手（组长）——两条都记在时间线上，都没生效。
            assert turns[0]["mentions"] == [third["id"]]
            assert turns[1]["mentions"] == [leader["id"]]
            assert "mentions" not in turns[2]  # 组长谁也没点，所以没有这个键
    finally:
        await built.aclose()


async def test_a_final_reply_from_somebody_else_is_ignored(tmp_path) -> None:
    """L17 / 终止 2：非组长写「最终答复」不生效，只留一笔 ``finalIgnored``。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(
            built,
            {
                boss.id: ["@写手 你接着说", "最终答复：这才算数。"],
                alpha.id: ["最终答复：我说了算。", "那好吧"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "谁说了算")
            await _join(api, group["id"], boss.id)
            await _join(api, group["id"], alpha.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "谁来定"}
            )
            await _settled(api, group["id"], what="组长收口")

            turns = [r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"]
            # 写手那一条：正文照落、房间照转，但不是最终答复。
            assert turns[0]["text"].startswith("最终答复")
            assert turns[0].get("final") is not True
            assert turns[0]["finalIgnored"] is True
            assert len(turns) == 4  # 房间接着转了第二轮
            assert turns[-1]["final"] is True
            assert turns[-1]["authorMemberId"] == turns[1]["authorMemberId"]
    finally:
        await built.aclose()


async def test_the_leader_closing_the_room_marks_its_own_turn_as_final(
    tmp_path,
) -> None:
    """终止 3 的界面那一面：组长最后那段被标成最终答复卡，并有一行人话说明为什么。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(
            built,
            {boss.id: ["大家都说完了，结论是甲。"], alpha.id: ["我觉得是甲"]},
        )
        async with _api(built) as api:
            group = await _new_group(api, "一轮完事")
            await _join(api, group["id"], boss.id)
            await _join(api, group["id"], alpha.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "介绍一下自己"}
            )
            thread = await _settled(api, group["id"], what="房间停下来")

            assert thread["status"] == "final"
            assert thread["endedReason"] == "leader_closed"
            rows = await _messages(api, group["id"])
            turns = [r for r in rows if r["kind"] == "member_turn"]
            assert len(turns) == 2  # 没有多余的总结条
            assert turns[-1]["final"] is True
            assert rows[-1]["text"] == "组长 组长 没有再点名，讨论到此为止"
    finally:
        await built.aclose()


async def test_the_room_says_so_when_there_is_nobody_left_to_close(tmp_path) -> None:
    """终止 6：组长在这一轮里被移出了，组里也没有别人 → 「没有可收口的成员」。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        _script_plan(built, {boss.id: ["@组长 我再想想"]})
        async with _api(built) as api:
            group = await _new_group(api, "空房间")
            leader = await _join(api, group["id"], boss.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "说说看"}
            )
            await _settled(api, group["id"])
            # 现在把唯一的成员移出，再发一句：队列是空的，线程直接 idle。
            await api.delete(f"/api/groups/{group['id']}/members/{leader['id']}")
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "还有人吗"}
            )
            thread = await _thread_of(api, group["id"])
            assert thread["status"] == "idle"
            assert thread["speakerQueue"] == []
            assert (
                await api.get(f"/api/groups/{group['id']}")
            ).json()["group"]["leaderMemberId"] is None
    finally:
        await built.aclose()


async def test_everyone_in_a_user_message_queues_all_active_members(tmp_path) -> None:
    """L19 的后端那一半：``@所有人`` 发出去 → 全员进队列（组长仍在末尾）。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {boss.id: ["（略过）"], alpha.id: ["（略过）"], beta.id: ["（略过）"]},
        )
        async with _api(built) as api:
            group = await _new_group(api, "全体")
            leader = await _join(api, group["id"], boss.id)
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "@所有人 说一句"}
            )
            thread = await _settled(api, group["id"])
            assert thread["speakerQueue"] == [
                first["id"],
                second["id"],
                leader["id"],
            ]
            assert thread["endedReason"] == "silent"  # 全员略过（含组长）
    finally:
        await built.aclose()


async def test_a_paused_leader_does_not_hold_up_the_queue(tmp_path) -> None:
    """组长暂停之后马上又有人发话：队尾换成新组长，不是那个投不出去的旧的。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(built, {alpha.id: ["我一个人说了算"]})
        async with _api(built) as api:
            group = await _new_group(api, "组长不在")
            leader = await _join(api, group["id"], boss.id)
            heir = await _join(api, group["id"], alpha.id)
            await api.post(f"/api/groups/{group['id']}/members/{leader['id']}/pause")
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "说说看"}
            )
            thread = await _settled(api, group["id"])
            assert thread["speakerQueue"] == [heir["id"]]
            assert thread["endedReason"] == "leader_closed"
    finally:
        await built.aclose()


async def test_the_leader_note_only_goes_to_the_leader(tmp_path) -> None:
    """真的投出去的那一份里：组长那份多一段「你是组长：…」，别人那份没有。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(built, {boss.id: ["（略过）"], alpha.id: ["（略过）"]})
        async with _api(built) as api:
            group = await _new_group(api, "两个人")
            await _join(api, group["id"], boss.id)
            await _join(api, group["id"], alpha.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "说说看"}
            )
            await _settled(api, group["id"])

            async def delivered(conversation_id: str) -> list[str]:
                envelopes = await built.host.event_store.replay(built.group_session_for(conversation_id))
                return [
                    envelope.event.data.get("text") or ""
                    for envelope in envelopes
                    if getattr(envelope.event, "type", None) == "extension.event"
                    and getattr(envelope.event, "name", None) == "user.message"
                ]

            to_leader = await delivered(boss.id)
            to_member = await delivered(alpha.id)
            assert to_leader and all(LEADER_NOTE in text for text in to_leader)
            assert to_member and all(LEADER_NOTE not in text for text in to_member)
            assert all("组长：组长。" in text for text in to_member)
            # 表态那一行整个不存在了。
            assert all("[状态]" not in text for text in to_leader + to_member)
    finally:
        await built.aclose()


async def test_asking_a_single_member_still_ends_with_the_leader(tmp_path) -> None:
    """定向发送 = 点名（PRD §B4）：只有他说话，**但组长仍旧在队尾**。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(built, {boss.id: ["（略过）"], alpha.id: ["我来答"]})
        async with _api(built) as api:
            group = await _new_group(api, "定向")
            leader = await _join(api, group["id"], boss.id)
            member = await _join(api, group["id"], alpha.id)
            await api.post(
                f"/api/groups/{group['id']}/members/{member['id']}/send",
                json={"text": "你来说"},
            )
            thread = await _settled(api, group["id"])
            assert thread["speakerQueue"] == [member["id"], leader["id"]]
    finally:
        await built.aclose()


async def test_a_stopped_room_does_not_advance_even_with_a_leader(tmp_path) -> None:
    """「停」仍旧压过一切（终止 1）：组长这一轮照记，但没有下一位。"""
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "组长")
        alpha = await _new_conversation(built, "写手")
        _script_plan(built, {boss.id: ["@写手 接着说"], alpha.id: ["好的"]})
        async with _api(built) as api:
            group = await _new_group(api, "会停的")
            await _join(api, group["id"], boss.id)
            await _join(api, group["id"], alpha.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "开始"}
            )
            await _settled(api, group["id"])
            stopped = await api.post(f"/api/groups/{group['id']}/thread/stop")
            assert stopped.status_code == 200
            await asyncio.sleep(0.1)
            assert (await _thread_of(api, group["id"]))["status"] in (
                "stopped",
                "final",
            )
    finally:
        await built.aclose()
