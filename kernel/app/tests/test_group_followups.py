"""Group close, pending approvals and coordinator scratch state (2026-11-01 follow-ups)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.persistence.sqlite.migrations import MIGRATIONS
from app.tests.test_batch22_groups import _api, _new_conversation, _new_group
from app.tests.test_independent_coordinator import cleanup, completed, install_responder, setup

pytestmark = pytest.mark.asyncio


async def test_upgrade_deletes_groups_closed_before_close_meant_delete(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        install_responder(b, lambda c, all: {'action': 'finish'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '做一件事'})
        await completed(b, gid)
        internal = await b.repositories.conversations.list_for_collaboration(gid)
        kept = internal[0]
        await b.repositories.conversations.save(kept.promote_to_project())
        # A group closed by an older release: still on disk, reachable from no screen.
        group = await b.repositories.collaborations.get(gid)
        await b.repositories.collaborations.save(group.close())
        open_group = await _new_group(api, 'still open')

        migration = next(m for m in MIGRATIONS if m.name == 'delete_closed_groups')
        with b.unit_of_work.database.transaction() as connection:
            for statement in migration.statements:
                connection.execute(statement)

        assert await b.repositories.collaborations.get(gid) is None
        assert await b.repositories.group_messages.list_for_collaboration(gid) == ()
        survivors = {c.id for c in await b.repositories.conversations.list_for_collaboration(gid)}
        assert survivors == {kept.id}
        assert await b.repositories.collaborations.get(open_group['id']) is not None
        for conversation in internal[1:]:
            assert await b.host.event_store.latest_sequence(conversation.id) in (None, 0)
    finally:
        await cleanup(b, api)


async def test_group_list_names_conversations_waiting_for_the_user(tmp_path, monkeypatch):
    b, api, gid, members = await setup(tmp_path)
    try:
        waiting = members[1]['conversationId']
        monkeypatch.setattr('app.api.group_router.awaiting_input', lambda state: state == 'pending')
        monkeypatch.setattr(b.host, 'timeline', lambda cid: 'pending' if cid == waiting else None)
        rows = (await api.get('/api/groups')).json()['groups']
        assert next(r for r in rows if r['id'] == gid)['awaitingConversationIds'] == [waiting]
    finally:
        await cleanup(b, api)


async def test_recent_index_flags_a_pending_approval(tmp_path, monkeypatch):
    b, api, gid, members = await setup(tmp_path)
    try:
        conversation = await _new_conversation(b, '等审批')
        pending = SimpleNamespace(run_state='running')
        monkeypatch.setattr('app.api.session_router.awaiting_input', lambda state: state is pending)
        monkeypatch.setattr(b.host, 'timeline', lambda cid: pending if cid == conversation.id else None)
        rows = (await api.get('/api/conversations')).json()['conversations']
        flagged = {row['id'] for row in rows if row.get('awaitingInput')}
        assert flagged == {conversation.id}
    finally:
        await cleanup(b, api)


async def test_scratch_bindings_are_reused_and_deleted_on_close(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: {'action': 'finish'})
        for text in ('第一件事', '第二件事'):
            await api.post(f'/api/groups/{gid}/broadcast', json={'text': text})
            await completed(b, gid)
        assert len(calls) == 2 and calls[0]['cid'] != calls[1]['cid']
        assert calls[0]['binding'].id == calls[1]['binding'].id != b.binding.id
        scratch = [c for c in await b.repositories.conversations.list_for_collaboration(gid) if c.retention == 'ephemeral']
        assert len(scratch) == 2

        closed = await api.post(f'/api/groups/{gid}/close')
        assert closed.status_code == 200, closed.text
        for conversation in scratch:
            assert await b.repositories.conversations.get(conversation.id) is None
        assert await b.repositories.bindings.get(calls[0]['binding'].id) is None
        assert (await b.repositories.bindings.get(b.binding.id)).enabled
        assert not (tmp_path / 'group-executions' / gid.split(':')[-1]).exists()
    finally:
        await cleanup(b, api)


async def test_settings_change_that_stops_a_task_says_so(tmp_path):
    from app.api.group_coordinator import CONFIG_CHANGED_NOTICE
    b, api, gid, members = await setup(tmp_path)
    try:
        calls = install_responder(b, lambda c, all: None)
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': '慢任务'})
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        coordinator = b.group_router.group_coordinator
        validate = coordinator.validate_config
        async def tightened(raw, *, existing=False):
            # The mock engine lists no approval modes; tighten the validated result instead.
            return (await validate(raw, existing=existing)).model_copy(update={'approval_mode': 'read_only'})
        coordinator.validate_config = tightened
        config = {'bindingId': b.binding.id, 'modelId': 'mock-small', 'providerId': 'mock-provider'}
        changed = await api.put(f'/api/groups/{gid}/coordinator', json={'config': config, 'expectedRevision': 1})
        assert changed.status_code == 200, changed.text
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'configuration_changed'
        rows = await b.repositories.group_messages.list_for_collaboration(gid)
        assert [r.content for r in rows if r.kind == 'system'][-1] == CONFIG_CHANGED_NOTICE
    finally:
        await cleanup(b, api)


async def test_deleted_project_members_cannot_join_or_run(tmp_path):
    b, api, gid, members = await setup(tmp_path)
    try:
        spare = await _new_conversation(b, '晚到的')
        project = await b.repositories.projects.get(spare.project_id)
        await b.repositories.projects.save(project.evolve(status='disabled'))
        refused = await api.post(f'/api/groups/{gid}/members', json={'conversationId': spare.id})
        assert refused.status_code == 409 and refused.json()['error']['code'] == 'project_deleted'
        calls = install_responder(b, lambda c, all: {'action': 'answer_done'})
        await api.post(f'/api/groups/{gid}/broadcast', json={'text': f"@{members[0]['displayName']} 看一下"})
        group = await completed(b, gid)
        assert group.thread.ended_reason == 'protocol_error' and not calls
    finally:
        await cleanup(b, api)
