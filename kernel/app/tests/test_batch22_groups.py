"""批次二十二：Group（临时协作组）数据面的端到端测试（`app/api/group_router.py`）。

覆盖（对应任务书「验证」一节）
------------------------------
1. 建组 / 列表 / 详情；
2. 加入的两条 409（同组重复、已在别的开着的组里）；
3. 移出 / 暂停 / 恢复（含「移出后重新加入」）；
4. ``/spawn`` 全链路（Mock Driver，含首句真的发出去了）；
5. ``/promote``（group_only → project_visible + persistent）；
6. 关组对三种 ``retention`` 的处置；
7. 两条事件流：会话流里的 ``kaus/group.changed``、组级全局 SSE；
8. 关组之后成员端点一律 409 ``group_closed``；
9. ``GET /api/conversations`` 索引行上的 ``groupId``。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver——不碰任何真实引擎、
不读任何凭据。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.api.group_router import (  # noqa: E402
    GroupEventHub,
    build_group_router,
    group_ids_for_conversations,
)
from app.api.group_views import group_change_data  # noqa: E402
from app.collaboration.models import CollaborationSession  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.tests.test_session_router import (  # noqa: E402
    TEST_TOKEN,
    Harness,
    SseProbe,
)


class GroupHarness(Harness):
    """会话路由 + Group 路由挂在同一个 app 上（真机就是这么挂的）。"""

    def __init__(self, tmp_path: Path, *, coordinator_enabled: bool = False) -> None:
        super().__init__(tmp_path)
        # Script fixtures are keyed by logical member identity; isolated runtimes
        # use the same canned engine response without copying native history.
        original_script = self.driver.script_for
        self.driver.script_for = lambda cid: original_script(cid if cid in self.driver._scripts_by_conversation else self.source_for(cid))
        self.hub = GroupEventHub()
        #: 批次四十五 a：路由自己留一份引用——`record_member_turn`（成员一轮跑完
        #: 的旁观者）挂在它上面，重放那条终态事件的用例要直接叫它。
        self.group_router = build_group_router(
            session_host=self.host,
            repositories=self.repositories,
            registry=self.registry,
            auth_policy=self.auth_policy,
            hub=self.hub,
            keepalive_interval=0.2,
            coordinator_enabled=coordinator_enabled,
            coordinator_root=str(tmp_path / "group-executions"),
        )
        self.app.include_router(self.group_router)


    def source_for(self, conversation_id):
        row = self.unit_of_work.database.query_one(
            "SELECT source_conversation_id FROM collaboration_members WHERE conversation_id=?", (conversation_id,))
        return row[0] if row and row[0] else conversation_id

    def group_session_for(self, source_id):
        rows = self.unit_of_work.database.query_all(
            "SELECT conversation_id FROM collaboration_members WHERE source_conversation_id=?", (source_id,))
        assert len(rows) <= 1, "Choose a specific group member when testing multiple groups"
        return rows[0][0] if rows else source_id


async def _seeded(tmp_path: Path) -> GroupHarness:
    built = GroupHarness(tmp_path)
    await built.seed()
    return built


def _api(built: GroupHarness):
    """一条挂在 ASGI 上的 httpx 客户端（与会话测试同一套做法）。"""
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built.app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )


async def _new_group(api, title: str = "临时协作组") -> dict:
    response = await api.post("/api/groups", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


async def _new_conversation(built: GroupHarness, title: str) -> Conversation:
    return await built.repositories.conversations.save(
        Conversation.create(
            project_id=built.project_id,
            agent_binding_id=built.binding.id,
            title=title,
        )
    )


# --------------------------------------------------------------------------- #
# 1. 建组 / 列表 / 详情
# --------------------------------------------------------------------------- #


async def test_create_group_starts_active_and_empty(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api, "设计评审")
            assert group["title"] == "设计评审"
            assert group["status"] == "active"
            assert group["closedAt"] is None
            assert group["memberCount"] == 0
            # C-6：不给 homeProjectId 就是 null，不替用户挑一个项目。
            assert group["homeProjectId"] is None
    finally:
        await built.aclose()


async def test_list_groups_defaults_to_open_ones(tmp_path) -> None:
    """``?status=active`` 只列开着的；``all`` 连关掉的一起列。"""
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            open_group = await _new_group(api, "开着的")
            closed = await _new_group(api, "关掉的")
            assert (await api.post(f"/api/groups/{closed['id']}/close")).status_code == 200

            active = (await api.get("/api/groups")).json()
            assert [g["id"] for g in active["groups"]] == [open_group["id"]]
            assert active["count"] == 1

            everything = (await api.get("/api/groups", params={"status": "all"})).json()
            assert {g["id"] for g in everything["groups"]} == {
                open_group["id"],
                closed["id"],
            }

            bad = await api.get("/api/groups", params={"status": "nope"})
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "invalid_status_filter"
    finally:
        await built.aclose()


async def test_group_detail_recomputes_members_every_call(tmp_path) -> None:
    """N §9.4：成员列表是现算的，不是创建时快照。

    做法很朴素：先取一次详情（0 个成员），加一个成员，再取一次（1 个成员）——
    如果哪天有人在建组时把成员列表快照下来，这条用例第二次仍会看到 0。
    成员行上还必须带得出会话摘要（Phase 7 清单第 6 条的数据面）。
    """
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "先有的会话")
        async with _api(built) as api:
            group = await _new_group(api)
            before = (await api.get(f"/api/groups/{group['id']}")).json()
            assert before["members"] == [] and before["memberCount"] == 0

            joined = await api.post(
                f"/api/groups/{group['id']}/members",
                json={"conversationId": conversation.id, "role": "评审"},
            )
            assert joined.status_code == 201, joined.text

            after = (await api.get(f"/api/groups/{group['id']}")).json()
            assert after["memberCount"] == 1
            member = after["members"][0]
            assert member["sourceConversationId"] == conversation.id
            assert member["conversationId"] != conversation.id
            assert member["roleLabel"] == "评审"
            assert member["joinMode"] == "existing"
            assert member["participationState"] == "active"
            summary = member["conversation"]
            assert summary["title"] == "先有的会话"
            assert summary["projectId"] == built.project_id
            assert summary["bindingId"] == built.binding.id
            assert summary["backendId"] == built.binding.backend_id
            assert summary["origin"] == "group_spawned"
            assert summary["runState"] == "idle"
            assert summary["surface"] == "card"

            assert (await api.get("/api/groups/group:missing")).status_code == 404
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 2. 加入的两条 409
# --------------------------------------------------------------------------- #


async def test_joining_the_same_group_twice_is_409(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "只能加一次")
        async with _api(built) as api:
            group = await _new_group(api)
            body = {"conversationId": conversation.id}
            assert (
                await api.post(f"/api/groups/{group['id']}/members", json=body)
            ).status_code == 201
            again = await api.post(f"/api/groups/{group['id']}/members", json=body)
            assert again.status_code == 409
            assert again.json()["error"]["code"] == "member_duplicate"
    finally:
        await built.aclose()


async def test_a_conversation_can_join_two_open_groups_independently(tmp_path) -> None:
    """409 ``member_elsewhere``，``detail.groupId`` 指出它现在归谁管。

    另一半同样重要：那个组**关掉之后**这条会话就自由了——关掉的组是历史，
    不该继续挡着。
    """
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "抢手的会话")
        async with _api(built) as api:
            first = await _new_group(api, "第一个组")
            second = await _new_group(api, "第二个组")
            body = {"conversationId": conversation.id}
            assert (
                await api.post(f"/api/groups/{first['id']}/members", json=body)
            ).status_code == 201

            joined = await api.post(f"/api/groups/{second['id']}/members", json=body)
            assert joined.status_code == 201
            one = (await api.get(f"/api/groups/{first['id']}")).json()["members"][0]
            two = joined.json()["member"]
            assert one["conversationId"] != two["conversationId"]
            assert one["sourceConversationId"] == two["sourceConversationId"] == conversation.id
            await api.post(f"/api/groups/{first['id']}/close")
            duplicate = await api.post(f"/api/groups/{second['id']}/members", json=body)
            assert duplicate.status_code == 409
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 3. 移出 / 暂停 / 恢复
# --------------------------------------------------------------------------- #


async def test_removing_a_member_keeps_the_conversation(tmp_path) -> None:
    """N §9.4：移出只标 ``left``，Conversation 与原生映射一个都不动。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "被移出的会话")
        async with _api(built) as api:
            group = await _new_group(api)
            member = (
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]

            removed = await api.delete(
                f"/api/groups/{group['id']}/members/{member['id']}"
            )
            assert removed.status_code == 200
            assert removed.json()["member"]["participationState"] == "left"
            assert removed.json()["member"]["leftAt"] is not None

            # 会话还在。
            assert await built.repositories.conversations.get(conversation.id) is not None
            # 默认视图里不再有他。
            detail = (await api.get(f"/api/groups/{group['id']}")).json()
            assert detail["members"] == []

            missing = await api.delete(f"/api/groups/{group['id']}/members/member:x")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "member_not_found"
    finally:
        await built.aclose()


async def test_pause_then_resume_walks_the_state_machine(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "暂停一下")
        async with _api(built) as api:
            group = await _new_group(api)
            member = (
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]
            base = f"/api/groups/{group['id']}/members/{member['id']}"

            paused = await api.post(f"{base}/pause")
            assert paused.status_code == 200
            assert paused.json()["member"]["participationState"] == "paused"
            # 暂停的成员仍在名单上（他只是没在参与，不是走了）。
            assert (await api.get(f"/api/groups/{group['id']}")).json()["memberCount"] == 1
            # 再暂停一次是无操作，不报错。
            assert (await api.post(f"{base}/pause")).json()["member"][
                "participationState"
            ] == "paused"

            resumed = await api.post(f"{base}/resume")
            assert resumed.status_code == 200
            assert resumed.json()["member"]["participationState"] == "active"
    finally:
        await built.aclose()


async def test_resume_is_how_a_left_member_rejoins(tmp_path) -> None:
    """N §9.4「成员可以稍后重新加入」——入口就是 ``/resume``。

    同时钉死暂停那条路对 ``left`` 的态度：不能暂停一个已经走了的人。
    """
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "去而复返")
        async with _api(built) as api:
            group = await _new_group(api)
            member = (
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]
            base = f"/api/groups/{group['id']}/members/{member['id']}"
            await api.delete(base)

            refused = await api.post(f"{base}/pause")
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "member_left"

            back = await api.post(f"{base}/resume")
            assert back.status_code == 200
            assert back.json()["member"]["participationState"] == "active"
            assert back.json()["member"]["leftAt"] is None
            assert (await api.get(f"/api/groups/{group['id']}")).json()["memberCount"] == 1
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 4. spawn 全链路
# --------------------------------------------------------------------------- #


async def test_spawn_creates_a_group_only_conversation_and_a_member(tmp_path) -> None:
    """v1.2 §Phase 2 第三条验收：用 Mock Driver 建一条 ``group_spawned`` 会话并加入。"""
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            response = await api.post(
                f"/api/groups/{group['id']}/spawn",
                json={"projectId": built.project_id, "title": "组内新成员"},
            )
            assert response.status_code == 201, response.text
            payload = response.json()
            conversation = payload["conversation"]
            assert conversation["origin"] == "group_spawned"
            assert conversation["visibility"] == "group_only"
            assert conversation["retention"] == "decide_on_group_close"
            assert conversation["createdByCollaborationId"] == group["id"]
            assert conversation["agentBindingId"] == built.binding.id
            assert payload["member"]["joinMode"] == "spawned_in_group"
            assert payload["initialMessage"] is None

            # N §9.5：group_only 不进项目常用列表。
            listed = (
                await api.get(f"/api/projects/{built.project_id}/conversations")
            ).json()
            assert conversation["id"] not in [c["id"] for c in listed["conversations"]]

            missing_project = await api.post(
                f"/api/groups/{group['id']}/spawn", json={"projectId": "nope"}
            )
            assert missing_project.status_code == 404
            assert missing_project.json()["error"]["code"] == "project_not_found"
    finally:
        await built.aclose()


async def test_spawn_sends_the_initial_message_through_the_driver(tmp_path) -> None:
    """带了 ``initialMessage`` 就要真的发出去：事件缓冲里能看到那句话。"""
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            payload = (
                await api.post(
                    f"/api/groups/{group['id']}/spawn",
                    json={
                        "projectId": built.project_id,
                        "bindingId": built.binding.id,
                        "initialMessage": "先说第一句",
                    },
                )
            ).json()
            # 批次二十七：首句成功时同时带回本轮的 runId（等不到就
            # `runIdPending`），「引擎收下了」与「这一轮真的起来了」不再是同一句话。
            initial = payload["initialMessage"]
            assert initial["sent"] is True and initial["error"] is None
            assert "runId" in initial and "runIdPending" in initial
            conversation_id = payload["conversation"]["id"]

            for _ in range(200):
                events = await built.host.event_store.replay(conversation_id)
                texts = [
                    e.event.data.get("text")
                    for e in events
                    if e.event.type == "extension.event"
                    and getattr(e.event, "name", None) == "user.message"
                ]
                if texts:
                    assert len(texts) == 1 and texts[0].endswith("先说第一句")
                    break
                await asyncio.sleep(0.02)
            else:  # pragma: no cover - 发不出去就是缺陷，不是竞态
                raise AssertionError("首句没有进事件缓冲")
            assert built.host.is_active(conversation_id)
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 5. promote
# --------------------------------------------------------------------------- #


async def test_promote_turns_a_group_only_conversation_into_a_normal_one(
    tmp_path,
) -> None:
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            spawned = (
                await api.post(
                    f"/api/groups/{group['id']}/spawn",
                    json={"projectId": built.project_id},
                )
            ).json()
            member_id = spawned["member"]["id"]

            promoted = await api.post(
                f"/api/groups/{group['id']}/members/{member_id}/promote"
            )
            assert promoted.status_code == 200
            body = promoted.json()
            assert body["promoted"] is True
            assert body["conversation"]["visibility"] == "project_visible"
            assert body["conversation"]["retention"] == "persistent"

            # 提升之后它就进项目常用列表了。
            listed = (
                await api.get(f"/api/projects/{built.project_id}/conversations")
            ).json()
            assert spawned["conversation"]["id"] in [
                c["id"] for c in listed["conversations"]
            ]

            # 再提升一次是无操作，仍然 200。
            again = await api.post(
                f"/api/groups/{group['id']}/members/{member_id}/promote"
            )
            assert again.status_code == 200 and again.json()["promoted"] is False
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 6. 关组的三种处置
# --------------------------------------------------------------------------- #


async def _spawn_with_retention(built: GroupHarness, api, group_id: str, retention: str):
    """建一条指定 ``retention`` 的组内会话并加入（``ephemeral`` 走不了端点默认值）。"""
    conversation = await built.repositories.conversations.save(
        Conversation.create_group_spawned(
            project_id=built.project_id,
            agent_binding_id=built.binding.id,
            title=f"retention={retention}",
            collaboration_id=group_id,
            retention=retention,  # type: ignore[arg-type]
        )
    )
    response = await api.post(
        f"/api/groups/{group_id}/members", json={"conversationId": conversation.id}
    )
    assert response.status_code == 201, response.text
    return conversation


async def test_close_leaves_persistent_members_alone(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "本来就是普通会话")
        async with _api(built) as api:
            group = await _new_group(api)
            await api.post(
                f"/api/groups/{group['id']}/members",
                json={"conversationId": conversation.id},
            )
            closed = await api.post(f"/api/groups/{group['id']}/close")
            assert closed.status_code == 200
            body = closed.json()
            assert body["group"]["status"] == "closed"
            assert body["group"]["closedAt"] is not None
            assert body["dispositions"][0]["action"] == "kept"

            after = await built.repositories.conversations.get(conversation.id)
            assert after.state == "idle"
            assert after.visibility == "project_visible"
            assert after.retention == "persistent"
    finally:
        await built.aclose()


async def test_close_with_keep_promotes_decide_on_group_close(tmp_path) -> None:
    """缺省 ``onClose=keep``：``decide_on_group_close`` → 提升为普通项目会话。"""
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            spawned = (
                await api.post(
                    f"/api/groups/{group['id']}/spawn",
                    json={"projectId": built.project_id},
                )
            ).json()["conversation"]

            body = (await api.post(f"/api/groups/{group['id']}/close")).json()
            assert body["onClose"] == "keep"
            assert body["dispositions"][0]["action"] == "promoted"

            after = await built.repositories.conversations.get(spawned["id"])
            assert after.visibility == "project_visible"
            assert after.retention == "persistent"
            # 归档才置 ended；保留下来的这条仍然是一条能继续用的会话。
            assert after.state == "idle"
    finally:
        await built.aclose()


async def test_close_archives_on_archive_and_always_archives_ephemeral(
    tmp_path,
) -> None:
    """``onClose=archive`` 归档待定的那批；``ephemeral`` 不问、一律归档。

    归档 = 置 ``state=ended``，**不清事件、不删会话**（任务书第 4 件）。
    """
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            undecided = await _spawn_with_retention(
                built, api, group["id"], "decide_on_group_close"
            )
            throwaway = await _spawn_with_retention(
                built, api, group["id"], "ephemeral"
            )

            body = (
                await api.post(
                    f"/api/groups/{group['id']}/close", json={"onClose": "archive"}
                )
            ).json()
            actions = {d["conversationId"]: d["action"] for d in body["dispositions"]}
            assert actions[undecided.id] == "archived"
            assert actions[throwaway.id] == "archived"

            for conversation_id in (undecided.id, throwaway.id):
                after = await built.repositories.conversations.get(conversation_id)
                assert after is not None, "归档不是删除"
                assert after.state == "ended"
                assert after.visibility == "group_only"

            bad = await api.post("/api/groups/group:x/close", json={"onClose": "burn"})
            assert bad.status_code == 404  # 组先不存在，轮不到校验 onClose
    finally:
        await built.aclose()


async def test_ephemeral_is_archived_even_when_the_user_says_keep(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            throwaway = await _spawn_with_retention(
                built, api, group["id"], "ephemeral"
            )
            body = (
                await api.post(
                    f"/api/groups/{group['id']}/close", json={"onClose": "keep"}
                )
            ).json()
            actions = {d["conversationId"]: d["action"] for d in body["dispositions"]}
            assert actions[throwaway.id] == "archived"
            after = await built.repositories.conversations.get(throwaway.id)
            assert after.state == "ended"
    finally:
        await built.aclose()


async def test_member_endpoints_are_409_after_the_group_is_closed(tmp_path) -> None:
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "关了之后")
        other = await _new_conversation(built, "想加进来")
        async with _api(built) as api:
            group = await _new_group(api)
            member = (
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]
            await api.post(f"/api/groups/{group['id']}/close")

            base = f"/api/groups/{group['id']}"
            attempts = [
                await api.post(
                    f"{base}/members", json={"conversationId": other.id}
                ),
                await api.delete(f"{base}/members/{member['id']}"),
                await api.post(f"{base}/members/{member['id']}/pause"),
                await api.post(f"{base}/members/{member['id']}/resume"),
                await api.post(f"{base}/spawn", json={"projectId": built.project_id}),
                await api.patch(base, json={"title": "改个名"}),
                await api.post(f"{base}/close"),
            ]
            assert [r.status_code for r in attempts] == [409] * len(attempts)
            assert {r.json()["error"]["code"] for r in attempts} == {"group_closed"}

            # 只读那两条仍然通：组还在，只是关了。
            assert (await api.get(base)).status_code == 200
            # 提升也仍然通——关组之后才决定「这条留下」是正常的用法。
            assert (
                await api.post(f"{base}/members/{member['id']}/promote")
            ).status_code == 200
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 7. 两条事件流
# --------------------------------------------------------------------------- #


async def test_group_change_lands_in_the_member_conversation_stream(tmp_path) -> None:
    """有会话的变更进**那条会话**的事件流（信封 v1.1 冻结 → ``extension.event``）。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "会收到通知的会话")
        async with _api(built) as api:
            group = await _new_group(api)
            member = (
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]
            await api.delete(f"/api/groups/{group['id']}/members/{member['id']}")

        events = await built.host.event_store.replay(member["conversationId"])
        assert not await built.host.event_store.replay(conversation.id)
        changes = [
            e.event
            for e in events
            if e.event.type == "extension.event"
            and e.event.namespace == "kaus"
            and e.event.name == "group.changed"
        ]
        assert [c.data["change"] for c in changes] == ["member_joined", "member_left"]
        # 批次二十六：成员变更同时在 Group 时间线上留一行系统消息，因此 data 里
        # 多了一个 `messageId`（那一行的 id）。其余键一字未变。
        assert changes[0].data == group_change_data(
            group_id=group["id"],
            change="member_joined",
            member_id=member["id"],
            conversation_id=member["conversationId"],
            message_id=changes[0].data["messageId"],
        )
        assert changes[0].data["messageId"].startswith("groupmsg:")
        # 变更通知不属于任何一轮。
        assert all(e.run_id is None for e in events if e.event.type == "extension.event")
    finally:
        await built.aclose()


async def test_group_events_sse_broadcasts_group_level_changes(tmp_path) -> None:
    """``GET /api/groups/events``：组级变更的全局流（右下角浮窗的计数靠它刷新）。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "进组的会话")
        async with SseProbe(built.app, "/api/groups/events") as probe:
            assert probe.status == 200
            assert probe.headers["content-type"].startswith("text/event-stream")
            async with _api(built) as api:
                group = await _new_group(api, "会广播的组")
                await api.post(
                    f"/api/groups/{group['id']}/members",
                    json={"conversationId": conversation.id},
                )
                await api.post(f"/api/groups/{group['id']}/close")
            frames = await probe.next_events(3)
        assert [f["data"]["change"] for f in frames] == [
            "group_created",
            "member_joined",
            "group_closed",
        ]
        assert [f["sequence"] for f in frames] == [1, 2, 3]
        assert all(f["type"] == "kaus/group.changed" for f in frames)
        assert frames[1]["data"]["conversationId"] == built.group_session_for(conversation.id)
    finally:
        await built.aclose()


async def test_group_events_stream_needs_a_token(tmp_path) -> None:
    """SSE 走 ``?token=``（``EventSource`` 带不了头），没 token 就是 401。"""
    built = await _seeded(tmp_path)
    try:
        async with SseProbe(built.app, "/api/groups/events", token=None) as probe:
            assert probe.status == 401
        async with SseProbe(
            built.app, "/api/groups/events", query=f"token={TEST_TOKEN}", token=None
        ) as probe:
            assert probe.status == 200
    finally:
        await built.aclose()


# --------------------------------------------------------------------------- #
# 8. 会话索引行上的 groupId
# --------------------------------------------------------------------------- #


async def test_conversation_index_rows_carry_the_group_id(tmp_path) -> None:
    """任务书第 2 件：侧栏要能给会话标一个组；关掉的组不算数。"""
    built = await _seeded(tmp_path)
    try:
        inside = await _new_conversation(built, "在组里")
        outside = await _new_conversation(built, "不在组里")
        async with _api(built) as api:
            group = await _new_group(api)
            await api.post(
                f"/api/groups/{group['id']}/members",
                json={"conversationId": inside.id},
            )
            rows = {
                row["id"]: row["groupId"]
                for row in (await api.get("/api/conversations")).json()["conversations"]
            }
            assert rows[inside.id] is None
            assert built.group_session_for(inside.id) not in rows
            assert rows[outside.id] is None

            await api.post(f"/api/groups/{group['id']}/close")
            rows = {
                row["id"]: row["groupId"]
                for row in (await api.get("/api/conversations")).json()["conversations"]
            }
            assert rows[inside.id] is None
    finally:
        await built.aclose()


async def test_group_ids_lookup_ignores_left_members(tmp_path) -> None:
    """取数函数本身：走了的成员不该继续给会话挂组标签。"""
    built = await _seeded(tmp_path)
    try:
        conversation = await _new_conversation(built, "来过又走了")
        group = await built.repositories.collaborations.save(
            CollaborationSession.create(title="来去自如")
        )
        async with _api(built) as api:
            member = (
                await api.post(
                    f"/api/groups/{group.id}/members",
                    json={"conversationId": conversation.id},
                )
            ).json()["member"]
            assert await group_ids_for_conversations(
                built.repositories, [member["conversationId"]]
            ) == {member["conversationId"]: group.id}
            await api.delete(f"/api/groups/{group.id}/members/{member['id']}")
            assert (
                await group_ids_for_conversations(
                    built.repositories, [member["conversationId"]]
                )
                == {}
            )
    finally:
        await built.aclose()
