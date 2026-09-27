"""Native failure causes survive Group scheduling without empty public replies."""
import json

import pytest

from app.tests.test_independent_coordinator import setup, cleanup, completed
from drivers.mock.fixtures import MockScript, EmitStep
from drivers.acp.client import AcpRpcError
from runtime.event_envelope import AgentError, RunStarted, MessageStarted, MessageCompleted, RunFailed, RunCompleted

pytestmark = pytest.mark.asyncio


def failing_driver(built, *, message, partial='', fail_on_repair=False):
    original = built.driver.send_message
    calls = []

    async def send(handle, content):
        calls.append(content.text)
        run = f'failure-{len(calls)}'
        events = [RunStarted(run_id=run)]
        if fail_on_repair and len(calls) == 1:
            events += [MessageStarted(message_id=run), MessageCompleted(message_id=run, text='已核查第一项'),
                       RunCompleted(run_id=run)]
        else:
            if partial:
                events += [MessageStarted(message_id=run), MessageCompleted(message_id=run, text=partial)]
            events += [RunFailed(run_id=run, error=AgentError(
                code='auth_required', message=message, retriable=False,
                detail={'diagnostic_dump': 'private engine response must not be copied'},
            ))]
        built.driver.set_script(handle.conversation_id, MockScript(
            name='native-failure', steps=tuple(EmitStep(event=event) for event in events),
        ))
        return await original(handle, content)

    built.driver.send_message = send
    return calls


@pytest.mark.parametrize('prompt,reason', [('请处理这个问题', 'leader_failed'), ('@写手 请回答', 'member_failed')])
async def test_failure_cause_is_persisted_and_visible_without_empty_reply(tmp_path, monkeypatch, prompt, reason):
    built, api, gid, _members = await setup(tmp_path)
    try:
        secret = 'acceptance-only-not-a-real-credential'
        monkeypatch.setenv('KAUS_CONTRACT_SECRET', secret)
        failing_driver(built, message=f'No API key configured; rejected value {secret}. Authorization: Bearer xyz987654321TOKEN')
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': prompt})
        group = await completed(built, gid)
        assert group.thread.ended_reason == reason
        failure = group.thread.coordination['error']
        record = group.thread.coordination['callRecords'][0]
        assert failure == record['error']
        assert failure['code'] == 'auth_required'
        assert 'No API key configured' in failure['message']
        assert failure['retriable'] is False
        assert 'detail' not in failure
        rows = await built.repositories.group_messages.list_for_collaboration(gid)
        replies = [row for row in rows if row.kind == 'member_turn']
        assert len(replies) == 1
        assert 'No API key configured' in replies[0].content
        assert replies[0].metadata['error'] == failure
        assert not [row for row in rows if row.kind == 'system' and '本轮未完成' in row.content]
        public = json.dumps(group.thread.coordination, ensure_ascii=False) + json.dumps([
            {'content': row.content, 'metadata': row.metadata} for row in rows], ensure_ascii=False)
        assert secret not in public
        assert 'xyz987654321TOKEN' not in public
        assert 'private engine response' not in public
    finally:
        await cleanup(built, api)


async def test_repair_failure_keeps_original_partial_answer_and_native_cause(tmp_path):
    built, api, gid, _members = await setup(tmp_path)
    try:
        calls = failing_driver(built, message='TLS certificate verify failed', fail_on_repair=True)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '请核查证据'})
        group = await completed(built, gid)
        assert len(calls) == 2
        assert group.thread.ended_reason == 'leader_failed'
        assert group.thread.coordination['error']['message'] == 'TLS certificate verify failed'
        rows = await built.repositories.group_messages.list_for_collaboration(gid)
        replies = [row for row in rows if row.kind == 'member_turn']
        assert len(replies) == 1
        assert '已核查第一项' in replies[0].content
        assert 'TLS certificate verify failed' in replies[0].content
    finally:
        await cleanup(built, api)


async def test_startup_failure_retains_structured_cause_without_duplicate_notice(tmp_path):
    built, api, gid, _members = await setup(tmp_path)
    try:
        async def failed_start(conversation):
            raise AcpRpcError(-32603, 'Internal error', {
                'message': 'Native startup could not connect',
                'error': {'message': 'TLS certificate verify failed'},
                'unrelated_dump': 'do not publish this field',
            })
        built.host.ensure_runtime = failed_start
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '请回答这个问题'})
        group = await completed(built, gid)
        record = group.thread.coordination['callRecords'][0]
        assert record['runId'] is None
        assert record['outcome'] == 'failed'
        assert record['error'] == group.thread.coordination['error']
        assert record['error']['code'] == '-32603'
        assert 'TLS certificate verify failed' in record['error']['message']
        assert 'do not publish' not in str(record)
        rows = await built.repositories.group_messages.list_for_collaboration(gid)
        replies = [row for row in rows if row.kind in ('member_turn', 'system') and row.metadata.get('epoch') == group.thread.epoch]
        assert len(replies) == 1
        assert 'TLS certificate verify failed' in replies[0].content
    finally:
        await cleanup(built, api)
