"""Real composer API -> host -> driver queue acceptance, with isolated SQLite."""
import asyncio
import json

import pytest

from app.tests.test_batch22_groups import GroupHarness, _api, _new_group, _new_conversation
from app.collaboration.coordinator import directed_targets, extract_control, public_body
from app.collaboration.room import RoomMember
from drivers.mock.fixtures import MockScript, EmitStep, HoldStep
from runtime.event_envelope import RunStarted, MessageStarted, MessageCompleted, RunCompleted

pytestmark = pytest.mark.asyncio


def wire_text(text, control):
    return text + '\n```kaus-control\n' + json.dumps(control, ensure_ascii=False) + '\n```'


def install_responder(built, responder):
    calls = []
    original = built.driver.send_message
    async def send(handle, content):
        template = json.JSONDecoder().raw_decode(content.text.split('字段模板：\n', 1)[1])[0] if '字段模板：\n' in content.text else json.JSONDecoder().raw_decode(content.text.split('模板：', 1)[1])[0]
        payload = json.JSONDecoder().raw_decode(content.text.split('本次公开输入：\n', 1)[1])[0] if '本次公开输入：\n' in content.text else None
        binding = await built.repositories.bindings.get(handle.binding_id)
        call = {'cid': handle.conversation_id, 'binding': binding, 'template': template, 'payload': payload, 'prompt': content.text}
        calls.append(call)
        answer = responder(call, calls)
        run = f'run-{len(calls)}'
        if isinstance(answer, str):
            body = answer
        elif answer is None:
            built.driver.set_script(handle.conversation_id, MockScript(name='wait', steps=(EmitStep(event=RunStarted(run_id=run)), HoldStep())))
            return await original(handle, content)
        else:
            body = wire_text('答复 ' + str(len(calls)), {**template, **answer})
        built.driver.set_script(handle.conversation_id, MockScript(name='answer', steps=tuple(EmitStep(event=e) for e in [
            RunStarted(run_id=run), MessageStarted(message_id=run, role='assistant'),
            MessageCompleted(message_id=run, text=body), RunCompleted(run_id=run)])))
        return await original(handle, content)
    built.driver.send_message = send
    return calls


async def setup(tmp_path, *, configured=True):
    built = GroupHarness(tmp_path, coordinator_enabled=True)
    await built.seed()
    api = _api(built)
    group = await _new_group(api)
    if configured:
        response = await api.put(f"/api/groups/{group['id']}/coordinator", json={'config': {'bindingId': built.binding.id, 'modelId': 'mock-small', 'providerId': 'mock-provider'}, 'expectedRevision': 0})
        assert response.status_code == 200, response.text
    members = []
    for name in ('写手', '评审', '助手'):
        conv = await _new_conversation(built, name)
        response = await api.post(f"/api/groups/{group['id']}/members", json={'conversationId': conv.id})
        assert response.status_code == 201, response.text
        members.append(response.json()['member'])
    return built, api, group['id'], members


async def completed(built, group_id):
    async with asyncio.timeout(5):
        while True:
            group = await built.repositories.collaborations.get(group_id)
            if group.thread and not group.thread.is_awaiting:
                await asyncio.sleep(.01)
                return group
            await asyncio.sleep(.01)


async def cleanup(built, api):
    for gid in list(built.group_router.group_coordinator.tasks):
        await built.group_router.group_coordinator.stop(gid)
    await api.aclose()
    await built.aclose()


async def test_simple_question_only_chair(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'finish'})
        r = await api.post(f'/api/groups/{gid}/broadcast', json={'text': '为什么最终答复总是写手？'})
        assert r.status_code == 200, r.text
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed', group.thread.coordination
        assert len(calls) == 1
        assert calls[0]['binding'].runtime_config['group_execution'] == 'planning'
        assert calls[0]['cid'] not in [m['conversationId'] for m in members]
        rows = await b.repositories.group_messages.list_for_collaboration(gid)
        output = [r for r in rows if r.kind == 'member_turn']
        assert len(output) == 1 and output[0].metadata['coordinator']
        assert 'kaus-control' not in output[0].content
    finally:
        await cleanup(b, api)


@pytest.mark.parametrize('text', ['@写手 请回答', '写手你来帮我解答一下', '评审，请解释写手的观点'])
async def test_one_addressee_without_chair(tmp_path, text):
    b, api, gid, members = await setup(tmp_path, configured=False)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'answer_done'})
        r = await api.post(f'/api/groups/{gid}/broadcast', json={'text': text})
        assert r.status_code == 200, r.text
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed'
        expected = members[1 if text.startswith('评审') else 0]['conversationId']
        assert [c['cid'] for c in calls] == [expected]
    finally:
        await cleanup(b, api)


async def test_two_person_debate_fresh_review_and_scope(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        def respond(c, all):
            return {'action': 'finish' if c['binding'].runtime_config.get('group_execution') else 'answer_done'}
        calls = install_responder(b, respond)
        r = await api.post(f'/api/groups/{gid}/broadcast', json={'text': '@写手 @评审 辩论这两个方案，形成结论'})
        assert r.status_code == 200, r.text
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed'
        assert [c['cid'] for c in calls[:2]] == [m['conversationId'] for m in members[:2]]
        assert len(calls) == 3
        assert calls[-1]['binding'].runtime_config['group_execution'] == 'review'
        assert len(calls[-1]['payload']['publicEvidence']['recentFirst']) == 3
        assert set(calls[-1]['payload']['allowedMembers']) == {m['id'] for m in members[:2]}
    finally:
        await cleanup(b, api)


async def test_invalid_action_cannot_wake_member_and_repair_once(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'delegate', 'assignments': [{'memberId': 'unknown', 'task': 'wrong'}], 'reason': 'test'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '完成任务'})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'protocol_error'
        assert len(calls) == 2
        assert all(c['binding'].runtime_config.get('group_execution') for c in calls)
    finally:
        await cleanup(b, api)


async def test_defaults_copy_and_running_config_snapshot(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        config = {'bindingId': b.binding.id, 'modelId': 'mock-small', 'providerId': 'mock-provider'}
        r = await api.put('/api/group-settings/coordinator', json={'config': config, 'expectedRevision': 0})
        assert r.status_code == 200, r.text
        second = await _new_group(api, 'second')
        assert second['settings']['coordinator']['sourceRevision'] == 1
        config['modelId'] = 'mock-large'
        r = await api.put('/api/group-settings/coordinator', json={'config': config, 'expectedRevision': 1})
        assert r.status_code == 200, r.text
        second_live = await b.repositories.collaborations.get(second['id'])
        assert second_live.settings['coordinator']['modelId'] == 'mock-small'
        calls = install_responder(b, lambda c, all: None)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '慢任务'})
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        changed = await api.put(f'/api/groups/{gid}/coordinator', json={'config': config, 'expectedRevision': 1})
        assert changed.status_code == 200, changed.text
        group = await b.repositories.collaborations.get(gid)
        assert group.thread.coordination['configSnapshot']['modelId'] == 'mock-small'
        assert group.settings['coordinator']['modelId'] == 'mock-large'
        stopped = await api.post(f'/api/groups/{gid}/thread/stop')
        assert stopped.status_code == 200, stopped.text
        group = await completed(b, gid)
        assert group.thread.status == 'stopped'
        assert group.thread.coordination['activeConversationId'] is None
        assert not b.group_router.group_coordinator.tasks
        assert await b.repositories.settings.get('group.coordinator.default') == r.json()['config']
    finally:
        await cleanup(b, api)


async def test_delegation_reviews_shared_evidence_then_finishes(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        def respond(c, all):
            if len(all) == 1:
                return {'action': 'delegate', 'mode': 'collaborative', 'reason': '分别核查计算和来源',
                        'assignments': [{'memberId': members[i]['id'], 'task': f'核查 {i}'} for i in (0, 1)]}
            return {'action': 'finish' if c['binding'].runtime_config.get('group_execution') else 'answer_done'}
        calls = install_responder(b, respond)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '请设计方案并核对证据'})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed'
        assert len(calls) == 4
        assert calls[0]['cid'] != calls[3]['cid']
        assert calls[1]['payload']['task'] == '核查 0'
        assert len(calls[3]['payload']['publicEvidence']['recentFirst']) == 4
        assert calls[3]['binding'].runtime_config['group_execution'] == 'review'
        # Frozen per-task config, not mutation of the source Binding.
        assert calls[0]['binding'].id != b.binding.id
        assert not (await b.repositories.bindings.get(b.binding.id)).runtime_config.get('group_execution')
    finally:
        await cleanup(b, api)


@pytest.mark.parametrize('text', ['@写手 @评审 各自介绍一下自己', '@写手 @评审 辩论，组长不要参与'])
async def test_no_unnecessary_chair_summary(tmp_path, text):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'answer_done'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': text})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed'
        assert [c['cid'] for c in calls] == [m['conversationId'] for m in members[:2]]
    finally:
        await cleanup(b, api)


async def test_chair_can_participate_then_be_reviewed_in_new_session(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'finish' if len(all) == 3 else 'answer_done'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '@组长 @写手 辩论一下'})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed', group.thread.coordination
        assert len(calls) == 3
        assert calls[1]['cid'] != calls[2]['cid']
        assert calls[2]['binding'].runtime_config['group_execution'] == 'review'
    finally:
        await cleanup(b, api)


async def test_call_cap_retains_results_and_does_not_fake_completion(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        config = {'bindingId': b.binding.id, 'modelId': 'mock-small', 'providerId': 'mock-provider', 'callCap': 2}
        await api.put(f'/api/groups/{gid}/coordinator', json={'config': config, 'expectedRevision': 1})
        def respond(c, all):
            if c['binding'].runtime_config.get('group_execution'):
                return {'action': 'delegate', 'mode': 'collaborative', 'reason': '验证一个问题', 'assignments': [{'memberId': members[0]['id'], 'task': '检查'}]}
            return {'action': 'answer_done'}
        calls = install_responder(b, respond)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '需要验证'})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'call_cap'
        assert group.thread.status == 'stopped' and len(calls) == 2
        assert len(group.thread.coordination['results']) == 2
    finally:
        await cleanup(b, api)


async def test_restart_stops_task_without_assigning_oldest_member(tmp_path):
    from app.api.group_router import reconcile_room_threads
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: None)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '慢任务'})
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        await reconcile_room_threads(b.repositories)
        group = await b.repositories.collaborations.get(gid)
        assert group.thread.status == 'stopped'
        assert group.leader_member_id is None
        assert group.settings['coordinator']['modelId'] == 'mock-small'
    finally:
        await cleanup(b, api)


async def test_handoff_preserves_chair_address_and_uses_new_configuration(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: None if len(all) == 1 else {'action': 'answer_done'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '@组长 只由你核查'})
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        old_cid = calls[0]['cid']
        response = await api.put(f'/api/groups/{gid}/coordinator', json={'config': {
            'bindingId': b.binding.id, 'modelId': 'mock-large', 'providerId': 'mock-provider'}, 'expectedRevision': 1})
        assert response.status_code == 200, response.text
        response = await api.post(f'/api/groups/{gid}/coordinator/handoff')
        assert response.status_code == 200, response.text
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'task_completed', group.thread.coordination
        assert group.thread.epoch == 2
        assert len(calls) == 2 and calls[1]['cid'] != old_cid
        assert calls[1]['binding'].default_model_id == 'mock-large'
        assert group.thread.coordination['addressedMembers'] == ['coordinator']
        rows = await b.repositories.group_messages.list_for_collaboration(gid)
        assert len([r for r in rows if r.kind == 'member_turn']) == 1
    finally:
        await cleanup(b, api)


async def test_reserved_chair_name_is_distinct_from_member(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        member = await b.repositories.members.get(members[0]['id'])
        await b.repositories.members.save(member.evolve(role_label='组长'))
        calls = install_responder(b, lambda c, all: {'action': 'answer_done'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '@组长 请回答'})
        await completed(b, gid)
        assert len(calls) == 1 and calls[0]['cid'] != members[0]['conversationId']
        response = await api.post(f'/api/groups/{gid}/broadcast', json={'text': '@组长（成员） 请回答'})
        assert response.status_code == 200, response.text
        await completed(b, gid)
        assert len(calls) == 2 and calls[1]['cid'] == members[0]['conversationId']
    finally:
        await cleanup(b, api)


async def test_no_chair_without_member_target_does_not_call_anyone(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'finish'})
        response = await api.post(f'/api/groups/{gid}/broadcast', json={'text': '组长不要参与，帮我回答'})
        assert response.status_code == 400, response.text
        assert not calls
    finally:
        await cleanup(b, api)


async def test_empty_finish_does_not_claim_success(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: wire_text('', {**c['template'], 'action': 'finish'}))
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '回答问题'})
        group = await completed(b, gid)
        assert group.thread.status == 'stopped' and group.thread.ended_reason == 'protocol_error'
        assert len(calls) == 1
    finally:
        await cleanup(b, api)
