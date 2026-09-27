"""批次五十一：组长的路由只认显式 ``@``（PRD §A3/§B4，2026-09-20 第六次修订）。

为什么有这一批
--------------
第二次外派（``docs/quality/reports/2026-09-19-group-2/REPORT.md`` §8）：⑱ 第 1 轮
组长那句 ``@评审 请回应打杂最后的追问，下一轮由你发言``，解析出来是
``mentions=[评审, 打杂]``——**打杂是被裸名匹配捎进去的**，组长只 ``@`` 了评审，
「打杂最后的追问」是在谈论他。那一场里恰好无害（辩论本来就该两人都说），但批次五十
之后路由只在组长手里，而主持人必然要在话里提成员（「评审的质疑成立」「打杂说的那个
边界已经清楚了」）——**组长谈论谁就等于点名谁**。

产品方裁决（2026-09-20）：**谁在说话决定用哪把尺子**。用户的开场话裸名照认（那是给
人的方便，他不必为了让房间转起来去学一个符号）；组长的路由只认显式 ``@名字``；成员
的话本来就只记录不路由（批次五十）。

覆盖
----
1. :func:`resolve_mentions` 的两把尺子：默认那把**一个字没变**，``explicit_only``
   那把不回退到裸名、长名优先仍对；
2. :func:`next_round_queue`：组长写了 ``@`` 才开下一轮，只在正文里提名字 → 队列
   除组长外为空 → 终止 3 停；
3. 用户开场那句裸名**照旧**点得动人（宽尺子没被顺手收窄）；
4. 端到端复现 ⑱：``mentions=[评审, 打杂]`` 而 ``routed=[评审]``，第 2 轮只有评审 +
   组长（测试单 L21）；
5. :data:`LEADER_NOTE` 里那句「只有写了 @ 的名字才算点名」。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据、不起任何真进程。
"""

from __future__ import annotations

import pytest

from app.collaboration.models import DEFAULT_ROUND_CAP
from app.collaboration.room import (
    LEADER_NOTE,
    MemberTurn,
    RoomMember,
    advance_thread,
    next_round_queue,
    resolve_mentions,
    start_thread,
)

# 真机 ⑱ 那一组的形状：写手是组长（最早加入），另外两位是评审与打杂。
LEAD = RoomMember("member:lead", "写手", "codex", True)
REVIEWER = RoomMember("member:rev", "评审", "hermes-acp", True)
GOFER = RoomMember("member:gofer", "打杂", "hermes", True)
ROOM = (LEAD, REVIEWER, GOFER)
ACTIVE = ("member:lead", "member:rev", "member:gofer")
LEADER = "member:lead"

#: ⑱ 第 1 轮组长真的写的那一句（报告 §1 表格里的原话）。
REAL_LEADER_LINE = "@评审 请回应打杂最后的追问，下一轮由你发言，我等辩论结束后再判。"


# --------------------------------------------------------------------------- #
# 1. 两把尺子
# --------------------------------------------------------------------------- #


def test_the_default_ruler_did_not_change_one_character() -> None:
    """默认那把尺子**一个字没变**：``@名字`` 与裸名都认（PRD §A5）。

    这一条钉的是「加参数不许顺手改默认」：用户那句「写手 和 评审 辩论一下」里一个
    ``@`` 都没有，而它必须照旧点得动两个人。
    """
    assert resolve_mentions("@评审 你来", ROOM) == ("member:rev",)
    assert resolve_mentions("评审 你来", ROOM) == ("member:rev",)
    assert resolve_mentions("写手 和 评审 辩论一下", ROOM) == (
        "member:lead",
        "member:rev",
    )
    assert resolve_mentions("你们讨论一下这个问题", ROOM) == ()


def test_the_narrow_ruler_does_not_fall_back_to_bare_names() -> None:
    """``explicit_only=True``：候选只有 ``@名字``，裸名一个都不认。"""
    assert resolve_mentions("@评审 你来", ROOM, explicit_only=True) == ("member:rev",)
    assert resolve_mentions("评审 你来", ROOM, explicit_only=True) == ()
    assert resolve_mentions(
        "评审 和 打杂 你们再来一轮", ROOM, explicit_only=True
    ) == ()


def test_the_narrow_ruler_still_prefers_the_longer_name() -> None:
    """长名优先与「命中就挖掉」不变：``@写手#2`` 不该同时点中两个写手。

    这正是 ``resolve_display_names`` 那张共用表想防的事——收窄候选不能把它一起
    收掉。
    """
    twins = (
        RoomMember("member:a", "写手", None, True),
        RoomMember("member:b", "写手", None, True),
    )
    assert resolve_mentions("@写手#2 你来", twins, explicit_only=True) == ("member:b",)
    assert resolve_mentions("@写手 你来", twins, explicit_only=True) == ("member:a",)
    # 裸名在这把尺子下仍旧不算——两个写手谁都不进。
    assert resolve_mentions("写手#2 你来", twins, explicit_only=True) == ()


def test_the_real_line_from_the_field_only_names_the_reviewer() -> None:
    """**真机 §8 那一句**：宽尺子捎上打杂，窄尺子只剩评审。

    两行断言并排放，是因为这一批改的全部就是这个差：同一句话、两把尺子、两个答案，
    而「用哪一把」由说话的人决定。
    """
    assert resolve_mentions(REAL_LEADER_LINE, ROOM) == ("member:rev", "member:gofer")
    assert resolve_mentions(REAL_LEADER_LINE, ROOM, explicit_only=True) == (
        "member:rev",
    )


def test_the_leader_note_says_that_bare_names_do_not_count() -> None:
    """组长这一边的规则得写在它读得到的地方（PRD §B6，逐字）。"""
    assert LEADER_NOTE.endswith("只有写了 @ 的名字才算点名，正文里提到的名字不算。")


# --------------------------------------------------------------------------- #
# 2. 下一轮队列
# --------------------------------------------------------------------------- #


def test_the_leader_talking_about_somebody_does_not_route_them() -> None:
    """组长在正文里提了两个人、只 ``@`` 了一个 → 下一轮只有那一个（+ 组长）。"""
    turns = (
        MemberTurn("member:rev", "我的质疑是甲"),
        MemberTurn("member:gofer", "我反驳乙"),
        MemberTurn(LEADER, "评审的质疑成立，打杂的反驳不充分。@评审 你再补一点"),
    )
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == ("member:rev", LEADER)


def test_a_leader_who_writes_no_at_sign_stops_the_room() -> None:
    """组长只在正文里写名字、一个 ``@`` 都没有 → 队列除组长外为空 → 终止 3 停。

    这是这一批**唯一会改变真机行为**的那一格：改之前这句话会开出第 2 轮（两个裸名
    都进队列），改之后房间在这里收口。两条都是对的产品行为，区别只在组长写没写那个
    符号——而 :data:`LEADER_NOTE` 每一轮都在教它写。
    """
    turns = (MemberTurn(LEADER, "评审 和 打杂 你们再来一轮"),)
    queue = next_round_queue(turns, ROOM, LEADER, ACTIVE)
    assert queue == (LEADER,)

    thread = start_thread(
        previous=None,
        speaker_queue=("member:rev", "member:gofer"),
        spectators=(),
        started_by_message_id="groupmsg:1",
        leader_member_id=LEADER,
    )
    for member_id in ("member:rev", "member:gofer", LEADER):
        thread = advance_thread(
            thread,
            member_id=member_id,
            passed=False,
            final=False,
            next_queue=queue,
            active_member_ids=ACTIVE,
            leader_member_id=LEADER,
            round_cap=DEFAULT_ROUND_CAP,
        )
    assert (thread.status, thread.ended_reason, thread.round) == (
        "final",
        "leader_closed",
        1,
    )


def test_everyone_is_untouched_because_it_already_needed_an_at_sign() -> None:
    """``@所有人`` 走的是 ``resolve_everyone``，它本来就只认带 ``@`` 的两种写法。"""
    turns = (MemberTurn(LEADER, "@所有人 都说一句"),)
    assert next_round_queue(turns, ROOM, LEADER, ACTIVE) == (
        "member:rev",
        "member:gofer",
        LEADER,
    )
    # 裸的「所有人」照旧不是一条指令（它在中文里太常出现）。
    assert next_round_queue(
        (MemberTurn(LEADER, "所有人 都说一句"),), ROOM, LEADER, ACTIVE
    ) == (LEADER,)


# --------------------------------------------------------------------------- #
# 3. 端到端：用户那句裸名照旧、组长那句只认 @
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.tests.test_batch22_groups import (  # noqa: E402
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
)


async def test_the_user_can_still_name_people_without_an_at_sign(tmp_path) -> None:
    """用户开场「写手 和 评审 辩论一下」→ 第 1 轮仍旧是那两位（+ 组长）。

    宽尺子留在用户这一边是这次裁决的另一半：他不会为了让房间转起来去学一个符号。
    组长（打杂，最早加入）没被点名，照旧追加在队尾。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "打杂")
        alpha = await _new_conversation(built, "写手")
        beta = await _new_conversation(built, "评审")
        _script_plan(
            built,
            {
                boss.id: ["我听完了。"],
                alpha.id: ["我支持甲"],
                beta.id: ["我支持乙"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "裸名开场")
            leader = await _join(api, group["id"], boss.id)
            first = await _join(api, group["id"], alpha.id)
            second = await _join(api, group["id"], beta.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "写手 和 评审 辩论一下"},
            )
            await _settled(api, group["id"], what="房间停下来")

            turns = [
                r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"
            ]
            # 第 1 轮正好是被裸名点到的那两位 + 队尾的组长。
            assert [t["authorMemberId"] for t in turns] == [
                first["id"],
                second["id"],
                leader["id"],
            ]
            assert [t["round"] for t in turns] == [1, 1, 1]
    finally:
        await built.aclose()


async def test_the_field_case_from_report_section_8(tmp_path) -> None:
    """**复现 ⑱ 第 1 轮那一句**（真机报告 §8）：提到了打杂，但没有路由给他。

    测试单 L21 验的就是这一行上的两格：``mentions`` 有打杂（他确实被谈到了），
    ``routed`` 只有评审（真正进了第 2 轮的是他）。第 2 轮因此是评审 + 组长，
    打杂旁观——改之前它会被裸名捎进第 2 轮。
    """
    built = await _seeded(tmp_path)
    try:
        boss = await _new_conversation(built, "写手")
        rev = await _new_conversation(built, "评审")
        gofer = await _new_conversation(built, "打杂")
        _script_plan(
            built,
            {
                boss.id: [REAL_LEADER_LINE, "最终答复：评审略胜。"],
                rev.id: ["我先说 @打杂", "我补一点"],
                gofer.id: ["我追问一句 @评审", "不该轮到我"],
            },
        )
        async with _api(built) as api:
            group = await _new_group(api, "功能 vs 测试")
            leader = await _join(api, group["id"], boss.id)
            second = await _join(api, group["id"], rev.id)
            third = await _join(api, group["id"], gofer.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast",
                json={"text": "功能和测试哪个优先，你们辩论一下"},
            )
            await _settled(api, group["id"], what="组长收口")

            turns = [
                r for r in await _messages(api, group["id"]) if r["kind"] == "member_turn"
            ]
            assert [t["authorMemberId"] for t in turns] == [
                second["id"],
                third["id"],
                leader["id"],  # 第 1 轮：全员 + 组长最后
                second["id"],
                leader["id"],  # 第 2 轮：只有评审 + 组长，打杂不在
            ]
            assert [t["round"] for t in turns] == [1, 1, 1, 2, 2]
            assert turns[-1]["final"] is True

            # L21 的两格：提到了打杂（宽尺子），路由只给了评审（窄尺子）。
            leader_turn = turns[2]
            assert leader_turn["mentions"] == [second["id"], third["id"]]
            assert leader_turn["routed"] == [second["id"]]
            # 收口那一条谁也没点、也没路由到人 → 两个键都不出现。
            assert "routed" not in turns[-1]
    finally:
        await built.aclose()
