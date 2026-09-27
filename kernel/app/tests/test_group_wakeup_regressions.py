"""Real Group failures: forced recipients, failed leaders, and reused epochs."""
import pytest

from app.api.group_views import collaboration_message_to_wire
from app.tests.test_batch22_groups import _api, _new_conversation, _new_group, _seeded
from app.tests.test_batch26_group_routing import _join
from app.tests.test_batch45b_room_loop import _messages, _script_plan, _settled
from drivers.mock.fixtures import EmitStep, text_stream_script
from runtime.event_envelope import AgentError, RunFailed


async def test_plain_named_question_does_not_wake_unrelated_members(tmp_path):
    h = await _seeded(tmp_path)
    try:
        lead = await _new_conversation(h, "写手")
        reviewer = await _new_conversation(h, "评审")
        other = await _new_conversation(h, "打杂")
        _script_plan(h, {lead.id: ["已答复，无需补充。"], reviewer.id: ["我的模型是测试模型。"], other.id: ["不该被唤起"]})
        async with _api(h) as api:
            group = await _new_group(api)
            leader = await _join(api, group["id"], lead.id)
            target = await _join(api, group["id"], reviewer.id)
            await _join(api, group["id"], other.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "那你作为评审用的是什么模型？"})
            await _settled(api, group["id"])
            turns = [m for m in await _messages(api, group["id"]) if m["kind"] == "member_turn"]
            assert [m["authorMemberId"] for m in turns] == [target["id"], leader["id"]]
    finally:
        await h.aclose()


@pytest.mark.parametrize("body", ["Failed to authenticate: OAuth session expired", "最终答复：未完成。@评审 继续"])
async def test_failed_leader_is_not_a_final_answer_or_a_new_round(tmp_path, body):
    h = await _seeded(tmp_path)
    try:
        lead = await _new_conversation(h, "写手")
        reviewer = await _new_conversation(h, "评审")
        script = text_stream_script(run_id="run-auth-failed", chunks=(body,))
        failed = EmitStep(event=RunFailed(run_id="run-auth-failed", error=AgentError(code="authentication_required", message="expired", retriable=False)))
        h.driver.set_script(lead.id, script.model_copy(update={"steps": script.steps[:-1] + (failed,)}))
        async with _api(h) as api:
            group = await _new_group(api)
            leader = await _join(api, group["id"], lead.id)
            await _join(api, group["id"], reviewer.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "写手解释一下"})
            thread = await _settled(api, group["id"])
            assert (thread["status"], thread["endedReason"]) == ("stopped", "leader_failed")
            turns = [m for m in await _messages(api, group["id"]) if m["kind"] == "member_turn"]
            assert len(turns) == 1
            assert turns[0]["authorMemberId"] == leader["id"]
            assert turns[0]["outcome"] == "failed"
            assert not turns[0].get("final") and not turns[0].get("routed")
            # Previously stored incorrect flags are also normalized on read.
            stored = await h.repositories.group_messages.get(turns[0]["id"])
            legacy = stored.evolve(metadata={**stored.metadata, "final": True})
            assert not collaboration_message_to_wire(legacy).get("final")
    finally:
        await h.aclose()


async def test_reused_epoch_cannot_replay_old_leader_mentions(tmp_path):
    h = await _seeded(tmp_path)
    try:
        lead = await _new_conversation(h, "写手")
        reviewer = await _new_conversation(h, "评审")
        _script_plan(h, {lead.id: ["@评审 请检查旧任务", "最终答复：旧任务完成。", "新问题已经回答。"], reviewer.id: ["旧任务已检查。"]})
        async with _api(h) as api:
            group = await _new_group(api)
            leader = await _join(api, group["id"], lead.id)
            await _join(api, group["id"], reviewer.id)
            await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "写手处理旧任务"})
            await _settled(api, group["id"])
            # Legacy migration/recovery cleared the thread, but kept its history.
            row = await h.repositories.collaborations.get(group["id"])
            await h.repositories.collaborations.save(row.evolve(thread=None))
            start = await api.post(f"/api/groups/{group['id']}/broadcast", json={"text": "写手回答新问题"})
            sequence = start.json()["message"]["sequence"]
            thread = await _settled(api, group["id"])
            turns = [m for m in await _messages(api, group["id"]) if m["kind"] == "member_turn" and m["sequence"] > sequence]
            assert [m["authorMemberId"] for m in turns] == [leader["id"]]
            assert thread["round"] == 1 and thread["endedReason"] == "leader_closed"
    finally:
        await h.aclose()
