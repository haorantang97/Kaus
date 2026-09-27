"""批次四十五 a：成员回复进组时间线（PRD §B2）+ 房间说明（PRD §B3）。

覆盖
----
1. 纯函数：房间说明的拼法、重名后缀、暂停 / 离开不列入、摘名片；
2. 纯函数：这一轮的正文口径、工具只给数、4000 字截断、outcome 映射、成员期判定；
3. 路由 / 契约：广播后假引擎的回复出现在 ``GET /groups/{id}/messages``，
   带成员名（``authorMemberId``）、``runId``、``toolCount``；
4. 幂等：同一条终态事件重放两次只落一行；
5. 多组：一条会话同时在两个组里，各记一条；
6. 成员期过滤：加入之前跑的那一轮不记。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据、不起任何真进程。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.collaboration.member_turns import (
    MEMBER_TURN_MAX_CHARS,
    find_recorded_turn,
    member_turn_metadata,
    membership_covers,
    summarize_turn,
    truncate_body,
    turn_outcome,
)
from app.collaboration.room import (
    ROOM_HEADER_SEPARATOR,
    RoomMember,
    member_id_tail,
    resolve_display_names,
    room_header,
    strip_room_header,
    with_room_header,
)


def _at(minute: int) -> datetime:
    return datetime(2026, 9, 14, 10, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. 房间说明（PRD §B3）
# --------------------------------------------------------------------------- #


def test_room_header_matches_the_prd_format() -> None:
    """格式是规格里逐字写死的那一段——它是发给引擎的话，改一个字都要有人知道。"""
    members = [
        RoomMember("member:a", "写手", "hermes-acp", True),
        RoomMember("member:b", "评审", "dsh", True),
        RoomMember("member:c", "打杂", None, True),
    ]
    header = room_header(
        group_title="周五评审", members=members, recipient_member_id="member:a"
    )
    assert header == (
        "[协作组「周五评审」] 你是「写手」（hermes-acp）。"
        "组里还有：评审（dsh）、打杂。"
        "要对某位成员说话，在回复里写 @<名字>。"
    )


def test_paused_and_left_members_are_not_in_the_roster() -> None:
    """PRD §B3 末句：暂停 / 离开的人不列入「组里还有」。

    他们仍留在表里占着自己的名字——否则别人的 ``#2`` 会在某人暂停的那一刻换号。
    """
    members = [
        RoomMember("member:a", "写手", "e1", True),
        RoomMember("member:b", "写手", "e2", False),  # 暂停中
        RoomMember("member:c", "写手", "e3", True),
    ]
    header = room_header(
        group_title="组", members=members, recipient_member_id="member:a"
    )
    assert header is not None
    assert "写手#2" not in header  # 暂停的那位不出现
    assert "写手#3（e3）" in header


def test_duplicate_names_get_a_stable_suffix() -> None:
    """重名加 ``#2``，且顺序稳定（``@`` 解析将来要用同一张表）。"""
    names = resolve_display_names(
        [
            RoomMember("member:a", "写手", None, True),
            RoomMember("member:b", "写手", None, True),
            RoomMember("member:c", "评审", None, True),
            RoomMember("member:d", "写手", None, True),
        ]
    )
    assert names == {
        "member:a": "写手",
        "member:b": "写手#2",
        "member:c": "评审",
        "member:d": "写手#3",
    }


def test_a_nameless_member_falls_back_to_the_id_tail() -> None:
    names = resolve_display_names([RoomMember("member:3f2a8c1199", "", None, True)])
    assert names["member:3f2a8c1199"] == "3f2a8c11"
    assert member_id_tail("member:3f2a8c1199") == "3f2a8c11"


def test_a_lone_member_is_told_so_instead_of_an_empty_roster() -> None:
    """组里只有他一个人时不写空名单、也不写一条找不到人的 ``@`` 用法（AD-71）。"""
    header = room_header(
        group_title="独角戏",
        members=[RoomMember("member:a", "写手", "e1", True)],
        recipient_member_id="member:a",
    )
    assert header == "[协作组「独角戏」] 你是「写手」（e1）。组里目前只有你一个人。"


def test_an_unknown_recipient_gets_no_header_at_all() -> None:
    """成员表与收件人不是同一次取的 → 不拼一句「你是『』」。"""
    assert (
        room_header(
            group_title="组",
            members=[RoomMember("member:a", "写手", None, True)],
            recipient_member_id="member:zzz",
        )
        is None
    )


def test_the_header_goes_in_front_of_the_text_with_a_separator() -> None:
    assert with_room_header("名片", "正文") == f"名片\n{ROOM_HEADER_SEPARATOR}\n正文"
    assert with_room_header(None, "正文") == "正文"
    assert with_room_header("", "正文") == "正文"


def test_stripping_the_header_gives_the_original_text_back() -> None:
    delivered = with_room_header(
        room_header(
            group_title="组",
            members=[RoomMember("member:a", "写手", "e1", True)],
            recipient_member_id="member:a",
        ),
        "各位早",
    )
    assert strip_room_header(delivered) == "各位早"


def test_stripping_leaves_a_users_own_separator_line_alone() -> None:
    """只做减法，且要两条证据齐全才动手——用户正文里的 ``---`` 不该被切。"""
    plain = "第一段\n---\n第二段"
    assert strip_room_header(plain) == plain


# --------------------------------------------------------------------------- #
# 2. 这一轮的正文（PRD §B2）
# --------------------------------------------------------------------------- #


class _Item:
    def __init__(self, kind: str, run_id: str, **extra) -> None:
        self.kind = kind
        self.run_id = run_id
        for key, value in extra.items():
            setattr(self, key, value)


class _State:
    def __init__(self, items) -> None:
        self.items = tuple(items)


def test_the_turn_body_is_the_last_assistant_message_of_that_run() -> None:
    state = _State(
        [
            _Item("message", "run-1", role="assistant", text="上一轮说的"),
            _Item("message", "run-2", role="user", text="用户这一句"),
            _Item("tool", "run-2", name="read_file"),
            _Item("tool", "run-2", name="write_file"),
            _Item("message", "run-2", role="assistant", text="中途"),
            _Item("message", "run-2", role="assistant", text="最终结论"),
        ]
    )
    assert summarize_turn(state, "run-2") == ("最终结论", 2)


def test_tools_of_other_runs_are_not_counted() -> None:
    state = _State(
        [
            _Item("tool", "run-1", name="a"),
            _Item("message", "run-2", role="assistant", text="答"),
        ]
    )
    assert summarize_turn(state, "run-2") == ("答", 0)


def test_a_run_with_no_owned_message_falls_back_to_the_last_assistant_one() -> None:
    """PRD §B2 括号里那一句：有的引擎不给 ``runId``，那就退到最后一条。"""
    state = _State([_Item("message", None, role="assistant", text="没挂上轮次的")])
    assert summarize_turn(state, "run-9") == ("没挂上轮次的", 0)


def test_no_timeline_at_all_is_an_empty_body_not_a_crash() -> None:
    assert summarize_turn(None, "run-1") == ("", 0)


def test_a_long_body_is_cut_and_flagged() -> None:
    body, truncated = truncate_body("字" * (MEMBER_TURN_MAX_CHARS + 10))
    assert truncated is True
    assert body.startswith("字" * 10)
    assert len(body) > MEMBER_TURN_MAX_CHARS  # 截断提示语本身也算正文
    assert body[:MEMBER_TURN_MAX_CHARS] == "字" * MEMBER_TURN_MAX_CHARS
    assert "4000" in body


def test_a_short_body_is_untouched() -> None:
    assert truncate_body("短") == ("短", False)


def test_outcome_is_only_written_for_the_two_bad_endings() -> None:
    assert turn_outcome("run.completed") is None
    assert turn_outcome("run.failed") == "failed"
    assert turn_outcome("run.interrupted") == "interrupted"


def test_metadata_omits_the_keys_that_do_not_apply() -> None:
    assert member_turn_metadata(
        run_id="run-1", tool_count=0, truncated=False, outcome=None
    ) == {"runId": "run-1", "toolCount": 0}
    assert member_turn_metadata(
        run_id="run-1", tool_count=3, truncated=True, outcome="failed"
    ) == {"runId": "run-1", "toolCount": 3, "truncated": True, "outcome": "failed"}


# --------------------------------------------------------------------------- #
# 3. 成员期与幂等（纯函数那一半）
# --------------------------------------------------------------------------- #


def test_membership_window_is_judged_by_run_start() -> None:
    joined, left = _at(10), _at(20)
    assert membership_covers(joined_at=joined, left_at=left, run_started_at=_at(15))
    assert membership_covers(joined_at=joined, left_at=left, run_started_at=_at(10))
    assert not membership_covers(joined_at=joined, left_at=left, run_started_at=_at(9))
    assert not membership_covers(joined_at=joined, left_at=left, run_started_at=_at(20))
    # 还没走的人：右边界敞开。
    assert membership_covers(joined_at=joined, left_at=None, run_started_at=_at(59))
    # 缺时间戳时判真：宁可多记一行可追溯的发言。
    assert membership_covers(joined_at=joined, left_at=left, run_started_at=None)


class _Row:
    def __init__(self, conversation_id: str, run_id: str) -> None:
        self.conversation_id = conversation_id
        self.metadata = {"runId": run_id}


def test_the_idempotency_key_is_conversation_plus_run() -> None:
    rows = [_Row("conv:a", "run-1"), _Row("conv:b", "run-2")]
    assert find_recorded_turn(rows, conversation_id="conv:a", run_id="run-1") is rows[0]
    # 同一个 runId、不同会话 → 不是同一轮。
    assert find_recorded_turn(rows, conversation_id="conv:z", run_id="run-1") is None
    assert find_recorded_turn(rows, conversation_id="conv:a", run_id="run-9") is None


# --------------------------------------------------------------------------- #
# 4. 路由 / 契约：广播 → 假引擎回复 → 组时间线
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.tests.test_batch22_groups import (  # noqa: E402
    GroupHarness,
    _api,
    _new_conversation,
    _new_group,
    _seeded,
)
from app.tests.test_batch26_group_routing import _join  # noqa: E402
from drivers.mock.fixtures import text_stream_script, tool_lifecycle_script  # noqa: E402


async def _delivered_text(
    built: GroupHarness, conversation_id: str, *, index: int = -1
) -> str:
    """这条会话**真的发出去**的某一份用户文本（``kaus/user.message`` 的正文）。

    ``index`` 缺省是最后一份；批次四十五 b 起房间会转好几轮，同一条会话上因此有
    好几份，要看第一次投过去的那一份就传 ``0``。
    """
    envelopes = await built.host.event_store.replay(built.group_session_for(conversation_id))
    texts = [
        envelope.event.data.get("text")
        for envelope in envelopes
        if getattr(envelope.event, "type", None) == "extension.event"
        and getattr(envelope.event, "name", None) == "user.message"
    ]
    assert texts, "这条会话上没有任何用户消息事件"
    return texts[index] or ""


async def _wait_for_kind(api, group_id: str, kind: str, *, timeout: float = 3.0):
    """轮询组时间线直到出现某种 kind 的行。假引擎的一轮是后台泵推的。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = (await api.get(f"/api/groups/{group_id}/messages")).json()["messages"]
        found = [row for row in rows if row["kind"] == kind]
        if found:
            return found, rows
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"等不到 kind={kind} 的行；现在有：{[r['kind'] for r in rows]}"
            )
        await asyncio.sleep(0.02)


async def test_a_members_reply_lands_on_the_group_timeline(tmp_path) -> None:
    """L1 的后端那一半：广播之后，成员这一轮的正文出现在组时间线上。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "组员甲")
        built.driver.set_script(
            conversation.id, tool_lifecycle_script(run_id="run-a", update_count=1)
        )
        async with _api(built) as api:
            group = await _new_group(api, "会答话的组")
            member = await _join(api, group["id"], conversation.id)
            await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "各位早"}
            )
            turns, _rows = await _wait_for_kind(api, group["id"], "member_turn")

            assert len(turns) == 1
            turn = turns[0]
            assert turn["authorRole"] == "member"
            assert turn["authorMemberId"] == member["id"]
            assert turn["conversationId"] == member["conversationId"]
            assert turn["runId"] == "run-a"
            # 工具**只给数**（PRD §B8 L1）：没有名字、没有入参、没有输出。
            assert turn["toolCount"] == 1
            assert "read_file" not in turn["text"]
            assert turn.get("outcome") is None
            assert turn.get("truncated") is None
    finally:
        await built.aclose()


async def test_the_delivered_text_carries_a_room_header_but_the_timeline_does_not(
    tmp_path,
) -> None:
    """PRD §B3：名片只加在投递给引擎的那一份上；时间线存原文 + 一个版本号。"""
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "写手")
        second = await _new_conversation(built, "评审")
        for conversation in (first, second):
            built.driver.set_script(conversation.id, text_stream_script())
        async with _api(built) as api:
            group = await _new_group(api, "两个人的组")
            await _join(api, group["id"], first.id)
            await _join(api, group["id"], second.id)
            posted = await api.post(
                f"/api/groups/{group['id']}/broadcast", json={"text": "各位早"}
            )
            message = posted.json()["message"]
            # 时间线上是原文。
            assert message["text"] == "各位早"
            assert message["roomHeaderVersion"] == 1
            # 批次四十五 b：**串行**（PRD §B4）——这一刻真的投出去的只有队首那位，
            # 其余几位等他说完再轮。响应体里因此只有第一行投递。
            assert [row["status"] for row in message["deliveries"]] == ["sent"]
            assert all(row["roomHeaderVersion"] == 1 for row in message["deliveries"])

            # 真的发给引擎的那一份带名片，且每个人的名片说的是**别人**是谁。
            # 假引擎自己不留正文，所以取会话事件流里那条 `kaus/user.message`
            # ——它记的就是真的发出去的那一份。
            #
            # 看的是**队首**那位（批次四十八：先加入的写手是组长、排队尾，所以队首是
            # 评审）：正文那一段是「这一轮要你回应的最新一条」，对队首而言就是用户
            # 刚说的那句；轮到组长时最新的一条已经是队首说的话了。
            delivered = await _delivered_text(built, second.id, index=0)
            assert delivered.startswith("[协作组「两个人的组」] 你是「评审」")
            assert "写手" in delivered
            assert delivered.endswith("各位早")
    finally:
        await built.aclose()


async def test_recording_the_same_run_twice_writes_one_row(tmp_path) -> None:
    """幂等键 ``(group, conversation, run)``：重放同一条终态事件不多记一行。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "组员甲")
        built.driver.set_script(conversation.id, text_stream_script(run_id="run-x"))
        async with _api(built) as api:
            group = await _new_group(api)
            await _join(api, group["id"], conversation.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "在吗"})
            await _wait_for_kind(api, group["id"], "member_turn")

            # 把那条终态事件再喂一次（进程重启补记 / 订阅重复触发都是这个形状）。
            envelopes = await built.host.event_store.replay(built.group_session_for(conversation.id))
            terminal = [
                envelope
                for envelope in envelopes
                if envelope.event.type == "run.completed"
            ]
            assert terminal, "假引擎这一轮没有终态事件"
            await built.group_router.record_member_turn(terminal[-1])

            turns, _rows = await _wait_for_kind(api, group["id"], "member_turn")
            assert len(turns) == 1
    finally:
        await built.aclose()


async def test_a_group_turn_does_not_leak_to_another_group(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "两头忙")
        built.driver.set_script(conversation.id, text_stream_script(run_id="run-two"))
        async with _api(built) as api:
            first = await _new_group(api, "甲组")
            second = await _new_group(api, "乙组")
            await _join(api, first["id"], conversation.id)
            await _join(api, second["id"], conversation.id)
            await api.post(f"/api/groups/{first['id']}/broadcast", json={"text": "在吗"})
            first_turns, _ = await _wait_for_kind(api, first["id"], "member_turn")
            second_rows = (await api.get(f"/api/groups/{second['id']}/messages")).json()["messages"]
            assert len(first_turns) == 1
            assert not [row for row in second_rows if row["kind"] == "member_turn"]
            assert not await built.host.event_store.replay(conversation.id)
    finally:
        await built.aclose()


async def test_a_turn_that_started_before_joining_is_not_recorded(tmp_path) -> None:
    """只记**是组成员期间**的轮次（PRD §B2）。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "先跑后进组")
        built.driver.set_script(conversation.id, text_stream_script(run_id="run-early"))
        async with _api(built) as api:
            group = await _new_group(api)
            member = await _join(api, group["id"], conversation.id)
            # 把加入时间挪到「以后」——等价于这一轮开跑时他还不是成员。
            row = await built.repositories.members.get(member["id"])
            await built.repositories.members.save(
                row.evolve(joined_at=datetime.now(tz=timezone.utc) + timedelta(hours=1))
            )
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "在吗"})
            await asyncio.sleep(0.3)

            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "messages"
            ]
            assert [row["kind"] for row in rows if row["kind"] == "member_turn"] == []
    finally:
        await built.aclose()
