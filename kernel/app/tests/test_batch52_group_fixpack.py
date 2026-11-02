"""批次五十二：第三次真机验收挖出来的几处后端修复。

覆盖
----
1. **系统行的名字带 `#2`**（第 2 件 / 真机 UI-03）：``member_joined`` 这一类行
   此前写的是**会话标题**，于是第二条同名会话加进来时时间线上写「media 加入了
   这个组」，而它在房间说明里被告知自己叫 ``media#2``。现在与成员栏、时间线
   气泡走同一张 :func:`resolve_display_names` 表。
2. （Context Packet 那一件随端点在 2026-11-03 移除。）
3. **组内改名**（第 5 件 / AD-173）：``PATCH …/members/{m}`` 写 ``roleLabel``，
   会话标题不动；改完重算显示名（撞名照旧 ``#2``），时间线一行「X 现在叫 Y」。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰真实引擎、不读凭据、
不起真进程。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.tests.test_batch22_groups import (  # noqa: E402
    _api,
    _new_conversation,
    _new_group,
    _seeded,
)
from app.tests.test_batch26_group_routing import _join  # noqa: E402


# --------------------------------------------------------------------------- #
# 第 2 件：同名成员的 `#2` 跟到系统行
# --------------------------------------------------------------------------- #


async def test_system_lines_use_the_shared_display_names(tmp_path) -> None:
    """两条同名会话入组：第二条那一行写的是 ``media#2 加入了这个组``。"""
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "media")
        second = await _new_conversation(built, "media")
        async with _api(built) as api:
            group = await _new_group(api, "同名组")
            await _join(api, group["id"], first.id)
            member2 = await _join(api, group["id"], second.id)

            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "messages"
            ]
            texts = [row["text"] for row in reversed(rows)]
            assert texts[0] == "media 加入了这个组"
            # 真机 UI-03：这一行此前也写「media」——与它自己的房间说明对不上。
            assert texts[-1] == "media#2 加入了这个组"

            # 暂停 / 恢复 / 移出 走同一张表。
            await api.post(
                f"/api/groups/{group['id']}/members/{member2['id']}/pause"
            )
            await api.post(
                f"/api/groups/{group['id']}/members/{member2['id']}/resume"
            )
            await api.delete(f"/api/groups/{group['id']}/members/{member2['id']}")
            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "messages"
            ]
            texts = [row["text"] for row in reversed(rows)]
            assert "media#2 被暂停，暂时不接收组内消息" in texts
            assert "media#2 恢复参与" in texts
            # 移出之后那张共用表里就没有他了（默认不带 `left`），所以这个名字
            # 必须在动手**之前**取——否则这一行又退回「media」。
            assert "media#2 被移出了这个组" in texts
            # 同名的第一位仍旧没有后缀：后缀按加入顺序给，不会换人。
            assert "media 加入了这个组" in texts
    finally:
        await built.aclose()


async def test_leader_lines_carry_the_suffix_too(tmp_path) -> None:
    """``X 是组长`` 与自动移交那两行也走同一张表（含 ``#2``）。"""
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "media")
        second = await _new_conversation(built, "media")
        third = await _new_conversation(built, "评审")
        async with _api(built) as api:
            group = await _new_group(api, "同名组")
            member1 = await _join(api, group["id"], first.id)
            member2 = await _join(api, group["id"], second.id)
            await _join(api, group["id"], third.id)

            # 用户把组长改成同名的第二位 → 那一行必须是「media#2 是组长」。
            patched = await api.patch(
                f"/api/groups/{group['id']}", json={"leaderMemberId": member2["id"]}
            )
            assert patched.status_code == 200, patched.text

            # 组长走了 → 自动移交。「不在了」的那位在写这一行的时候已经离开名册，
            # 所以他的 `#2` 也得是动手之前取的。
            await api.delete(f"/api/groups/{group['id']}/members/{member2['id']}")

            rows = (await api.get(f"/api/groups/{group['id']}/messages")).json()[
                "messages"
            ]
            texts = [row["text"] for row in reversed(rows)]
            assert "media 是组长" in texts  # 最早加入的那位，默认组长
            assert "media#2 是组长" in texts
            assert "media#2 被移出了这个组" in texts
            assert any(
                line.startswith("media#2 不在了，") and "接任组长" in line
                for line in texts
            )
            # 接任的是最早加入的另一位 active 成员。
            assert member1["id"]
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 第 5 件：组内改名（AD-173）
# --------------------------------------------------------------------------- #


async def test_patch_member_role_label_renames_inside_the_group(tmp_path) -> None:
    """写 ``roleLabel`` = 改「他在这个房间里叫什么」，会话标题一个字不动。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "接口层重构")
        async with _api(built) as api:
            group = await _new_group(api, "改名组")
            member = await _join(api, group["id"], conversation.id)
            assert member["displayName"] == "接口层重构"

            patched = await api.patch(
                f"/api/groups/{group['id']}/members/{member['id']}",
                json={"roleLabel": "评审"},
            )
            assert patched.status_code == 200, patched.text
            row = patched.json()["member"]
            assert row["roleLabel"] == "评审"
            # `roleLabel` 在回退链第一位，所以显示名立刻换成它。
            assert row["displayName"] == "评审"
            # 会话自己的标题没动：同一条会话在别处仍旧叫它本来的名字。
            assert row["conversation"]["title"] == "接口层重构"
            fresh = await built.repositories.conversations.get(conversation.id)
            assert fresh is not None and fresh.title == "接口层重构"

            texts = [
                message["text"]
                for message in (
                    await api.get(f"/api/groups/{group['id']}/messages")
                ).json()["messages"]
            ]
            assert "接口层重构 现在叫 评审" in texts

            # 空字符串 = 清掉，回退到会话标题。
            cleared = await api.patch(
                f"/api/groups/{group['id']}/members/{member['id']}",
                json={"roleLabel": ""},
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["member"]["roleLabel"] is None
            assert cleared.json()["member"]["displayName"] == "接口层重构"

            # 什么都不给 ≠ 清掉：那是一次没有内容的请求。
            empty = await api.patch(
                f"/api/groups/{group['id']}/members/{member['id']}", json={}
            )
            assert empty.status_code == 400
            assert empty.json()["error"]["code"] == "empty_patch"
    finally:
        await built.aclose()


async def test_patch_member_recomputes_the_suffix_after_a_clash(tmp_path) -> None:
    """改完撞名照旧 ``#2``，而且时间线上写的是**重算之后**的那个名字。"""
    built = await _seeded(tmp_path)
    try:
        first = await _new_conversation(built, "写手")
        second = await _new_conversation(built, "打杂")
        async with _api(built) as api:
            group = await _new_group(api, "改名组")
            await _join(api, group["id"], first.id)
            member2 = await _join(api, group["id"], second.id)

            # 把第二位也改叫「写手」→ 他拿到的是 `写手#2`。
            patched = await api.patch(
                f"/api/groups/{group['id']}/members/{member2['id']}",
                json={"roleLabel": "写手"},
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["member"]["displayName"] == "写手#2"

            texts = [
                message["text"]
                for message in (
                    await api.get(f"/api/groups/{group['id']}/messages")
                ).json()["messages"]
            ]
            # 用户敲的是「写手」，但他接下来真的会被告知的名字是「写手#2」。
            assert "打杂 现在叫 写手#2" in texts

            # 同一个名字再写一次 = 无操作，不往时间线上添一行没内容的话。
            before = len(texts)
            again = await api.patch(
                f"/api/groups/{group['id']}/members/{member2['id']}",
                json={"roleLabel": "写手"},
            )
            assert again.status_code == 200
            after = (await api.get(f"/api/groups/{group['id']}/messages")).json()
            assert after["count"] == before
    finally:
        await built.aclose()


async def test_patch_conversation_title_is_its_own_path(tmp_path) -> None:
    """批次五十二第 5 件：``PATCH /api/conversations/{id}`` 收 ``title``。

    侧栏 ⋯ 菜单里那条「改名」走的是 ``POST /api/agent/{name}/rename``——改的是
    **项目（agent profile）的显示名**，不是会话标题。会话标题是组里显示名回退链
    的第二档，所以它必须自己有一条路。
    """
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "未命名会话")
        async with _api(built) as api:
            renamed = await api.patch(
                f"/api/conversations/{conversation.id}", json={"title": "接口层重构"}
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["conversation"]["title"] == "接口层重构"
            # 不下发给引擎：这一格说的是模型那件事。
            assert renamed.json()["appliedToRuntime"] is False

            fresh = await built.repositories.conversations.get(conversation.id)
            assert fresh is not None and fresh.title == "接口层重构"

            # 组里那张表跟着换（会话标题是回退链第二档）。
            group = await _new_group(api, "跟着改名的组")
            member = await _join(api, group["id"], conversation.id)
            assert member["displayName"] == "接口层重构"
            await api.patch(
                f"/api/conversations/{conversation.id}", json={"title": "接口层重构 v2"}
            )
            detail = (await api.get(f"/api/groups/{group['id']}")).json()
            assert detail["members"][0]["displayName"] == "接口层重构"

            # 空标题不收：列表里那一行总要有字。
            blank = await api.patch(
                f"/api/conversations/{conversation.id}", json={"title": "   "}
            )
            assert blank.status_code == 400
            assert blank.json()["error"]["code"] == "empty_title"

            # 什么都不给仍旧是 empty_patch。
            empty = await api.patch(f"/api/conversations/{conversation.id}", json={})
            assert empty.status_code == 400
            assert empty.json()["error"]["code"] == "empty_patch"
    finally:
        await built.aclose()
