"""Group scope, explicit materials, versioned delivery, and safe legacy migration."""
import asyncio
import json
from pathlib import Path
import pytest
from app.collaboration.materials import GroupMaterial, GroupMaterials, InMemoryGroupMaterialsRepository, MaterialConflict, material_context
from app.collaboration.models import CollaborationSession
from app.persistence.sqlite import SqliteUnitOfWork
from app.persistence.sqlite.database import SqliteDatabase
from app.persistence.sqlite.migrations import migrate
from app.tests.test_batch22_groups import _seeded, _api, _new_conversation, _new_group
from app.tests.test_batch26_group_routing import _join, _wait_for_deliveries
from drivers.mock.fixtures import text_stream_script

@pytest.mark.parametrize('persistent', [False, True])
async def test_materials_compare_and_swap_and_group_scope(tmp_path, persistent):
    uow = SqliteUnitOfWork(tmp_path/'materials.db') if persistent else None
    repo = uow.repositories.group_materials if uow else InMemoryGroupMaterialsRepository()
    a, b = CollaborationSession.create(title='A'), CollaborationSession.create(title='B')
    try:
        first = await repo.save(GroupMaterials(group_id=a.id, items=(GroupMaterial(title='背景',content='原文末尾END'),)), expected_revision=0)
        with pytest.raises(MaterialConflict):
            await repo.save(first, expected_revision=0)
        assert (await repo.get(b.id)).items == ()
        assert (await repo.get(a.id)).items[0].content.endswith('END')
        deleted = await repo.save(first.evolve(items=()), expected_revision=1)
        assert deleted.revision == 2
        assert '[]' in material_context(deleted.wire())
        assert '撤回' in material_context(deleted.wire())
    finally:
        if uow: uow.close()


def test_limits_count_unicode_and_never_truncate():
    gid = CollaborationSession.create(title='group').id
    valid = GroupMaterials(group_id=gid, revision=1, items=(GroupMaterial(title='A',content='中'*11998+'😀'),))
    assert valid.wire()['charCount'] == 12000
    assert ('中'*11998+'😀') in material_context(valid.wire())
    with pytest.raises(ValueError):
        valid.evolve(items=(GroupMaterial(title='AB',content='中'*11999),))
    with pytest.raises(ValueError):
        valid.evolve(items=tuple(GroupMaterial(title='x',content='y') for _ in range(21)))


async def test_join_separates_native_context_and_preserves_configuration(tmp_path):
    h = await _seeded(tmp_path)
    try:
        source = await _new_conversation(h, '原单聊')
        source = await h.repositories.conversations.save(source.evolve(model_id='chosen',provider_id='provider',reasoning_mode='high',approval_mode='read_only',native_session_id='native-private'))
        async with _api(h) as api:
            one, two = await _new_group(api,'one'), await _new_group(api,'two')
            a, b = await _join(api,one['id'],source.id), await _join(api,two['id'],source.id)
            assert len({a['conversationId'],b['conversationId'],source.id}) == 3
            for member in (a,b):
                clone = await h.repositories.conversations.get(member['conversationId'])
                assert clone.native_session_id is None and clone.native_session_segments == ()
                assert clone.agent_binding_id == source.agent_binding_id
                assert (clone.model_id,clone.provider_id,clone.reasoning_mode,clone.approval_mode) == ('chosen','provider','high','read_only')
                assert clone.visibility == 'group_only'
                assert member['sourceConversationId'] == source.id
            assert (await api.post(f"/api/groups/{one['id']}/members",json={'conversationId':source.id})).status_code == 409
            await api.post(f"/api/groups/{one['id']}/close")
            assert await h.repositories.conversations.get(source.id) == source
    finally: await h.aclose()


async def test_material_api_cas_scope_validation_and_retention(tmp_path):
    h = await _seeded(tmp_path)
    try:
        async with _api(h) as api:
            a,b = await _new_group(api),await _new_group(api)
            path = f"/api/groups/{a['id']}/materials"
            payload={'title':'观点','content':'选定的原文','sourceKind':'note','expectedRevision':0}
            result=await api.post(path,json=payload)
            assert result.status_code == 200, result.text
            first=result.json();mid=first['items'][0]['id']
            assert (await api.get(f"/api/groups/{b['id']}/materials")).json()['items']==[]
            assert (await api.post(path,json=payload)).status_code == 409
            assert (await api.patch(f"/api/groups/{b['id']}/materials/{mid}",json=payload)).status_code==404
            bad=await api.post(path,json={**payload,'expectedRevision':1,'sourceKind':'group_message','sourceId':'missing'})
            assert bad.status_code==404
            too_long=await api.post(path,json={**payload,'expectedRevision':1,'content':'中'*11999})
            assert too_long.status_code==400
            blank=await api.post(path,json={**payload,'expectedRevision':1,'title':' '})
            assert blank.status_code==400
            await api.patch(f"/api/groups/{a['id']}",json={'status':'minimized'})
            assert (await api.get(path)).json()==first
            await api.post(f"/api/groups/{a['id']}/close")
            assert (await api.get(path)).json()==first
            assert (await api.delete(path+'/'+mid,params={'expectedRevision':1})).status_code==409
    finally: await h.aclose()


async def test_all_speakers_receive_same_snapshot_and_original_chat_receives_nothing(tmp_path):
    h=await _seeded(tmp_path)
    sent=[]
    original=h.driver.send_message
    async def record(runtime, content):
        sent.append((runtime.conversation_id,content.text))
        await original(runtime,content)
    h.driver.send_message=record
    try:
        async with _api(h) as api:
            group=await _new_group(api)
            source=await _new_conversation(h,'原单聊')
            a=await _join(api,group['id'],source.id)
            other=await _new_conversation(h,'另一位')
            b=await _join(api,group['id'],other.id)
            for member in (a,b):h.driver.set_script(member['conversationId'],text_stream_script())
            path=f"/api/groups/{group['id']}/materials"
            added=await api.post(path,json={'title':'共同背景','content':'KAUS_SHARED_资料尾端','expectedRevision':0})
            assert added.status_code==200
            posted=await api.post(f"/api/groups/{group['id']}/broadcast",json={'text':'请讨论'})
            assert posted.status_code==200,posted.text
            receipts=await _wait_for_deliveries(api,group['id'],2)
            assert {r['materialsRevision'] for r in receipts}=={1}
            assert {cid for cid,_ in sent}=={a['conversationId'],b['conversationId']}
            assert all('KAUS_SHARED_资料尾端' in text for _,text in sent)
            row=await h.repositories.group_messages.get(posted.json()['message']['id'])
            assert row.metadata['materialsSnapshot']['revision']==1
            assert await h.host.event_store.replay(source.id)==()
            assert (await h.repositories.conversations.get(source.id)).native_session_id is None
    finally: await h.aclose()


def test_empty_legacy_memory_table_is_removed_and_upgrade_is_repeatable(tmp_path):
    db=SqliteDatabase(tmp_path/'upgrade.db',apply_migrations=False)
    try:
        migrate(db,target_version=9)
        db.run("INSERT INTO backends(id,key,display_name,driver_kind,capabilities_json) VALUES (?,?,?,?,?)",
               ('backend:legacy','legacy','Legacy','mock','{"capabilityProjection":{"memory":"unsupported","mcp":"native"}}'))
        assert migrate(db)==(10,11,12,13)
        assert '"memory"' not in db.query_one("SELECT capabilities_json FROM backends WHERE id='backend:legacy'")[0]
        assert '"mcp"' in db.query_one("SELECT capabilities_json FROM backends WHERE id='backend:legacy'")[0]
        assert migrate(db)==()
        names={r[0] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'shared_memory_records' not in names
        assert 'retired_project_memory_records' not in names
        assert 'group_materials' in names
    finally:db.close()


async def test_upgrade_isolates_legacy_members_and_keeps_user_data(tmp_path):
    from app.persistence.sqlite import build_repository_set
    from app.projects.models import Project, Backend, AgentBinding
    from app.conversations.models import Conversation
    db=SqliteDatabase(tmp_path/'legacy.db',apply_migrations=False)
    try:
        migrate(db,target_version=9)
        repos=build_repository_set(db)
        project=await repos.projects.save(Project.create(slug='legacy',display_name='Legacy'))
        await repos.backends.save(Backend.create(key='mock',display_name='Mock',driver_kind='mock'))
        binding=await repos.bindings.save(AgentBinding.create(project=project,backend='mock',display_name='binding'))
        source=Conversation.create(project_id=project.id,agent_binding_id=binding.id,title='Private').evolve(native_session_id='private-native',model_id='model',approval_mode='ask')
        # Seed the historical schema directly; today's repository writes today's columns.
        legacy=source.model_dump(mode='json')
        legacy['native_session_segments_json']=json.dumps(legacy.pop('native_session_segments'))
        columns=[row['name'] for row in db.query_all('PRAGMA table_info(conversations)')]
        db.run(f"INSERT INTO conversations ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(legacy[name] for name in columns))
        group=await repos.collaborations.save(CollaborationSession.create(title='existing'))
        timestamp=source.created_at.isoformat()
        from app.ids import collaboration_member_id
        mid=collaboration_member_id()
        db.run("INSERT INTO collaboration_members(id,collaboration_session_id,conversation_id,join_mode,participation_state,isolation_mode,joined_at) VALUES (?,?,?,?,?,?,?)",
               (mid,group.id,source.id,'existing','active','shared_read_only',timestamp))
        db.run("INSERT INTO shared_memory_records(id,project_id,record_type,content,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               ('memory:legacy',project.id,'current_state','Do not lose this existing record',timestamp,timestamp))
        assert migrate(db)==(10,11,12,13)
        assert await repos.conversations.get(source.id)==source
        member=await repos.members.get(mid)
        clone=await repos.conversations.get(member.conversation_id)
        assert member.source_conversation_id==source.id
        assert clone.id!=source.id and clone.native_session_id is None
        assert clone.visibility=='group_only' and clone.model_id=='model'
        assert clone.created_by_collaboration_id==group.id
        assert db.query_one('SELECT content FROM retired_project_memory_records')[0]=='Do not lose this existing record'
        assert (await repos.group_materials.get(group.id)).items==()
        assert migrate(db)==()
        assert (await repos.members.get(mid)).conversation_id==clone.id
    finally:db.close()
