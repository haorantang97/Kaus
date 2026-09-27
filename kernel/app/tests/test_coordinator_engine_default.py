"""A model-free catalog is usable only with a successful driver attestation."""
import pytest

from app.tests.test_independent_coordinator import setup, cleanup, completed, install_responder
from drivers.base import ModelCatalog

pytestmark = pytest.mark.asyncio


def engine_catalog(built, *, available=True, degraded=False):
    async def catalog(binding):
        return ModelCatalog(
            binding_id=binding.id, mode='fixed', models=(),
            engine_default_available=available, degraded=degraded,
            diagnostics=('需要先登录引擎',) if degraded else (),
            supports_reasoning=True,
        )
    built.driver.get_model_catalog = catalog
    built.driver.conversation_controls = lambda binding: {
        'reasoning': True, 'reasoningLevels': ['low', 'medium'], 'approvalModes': [],
    }


async def test_engine_default_reaches_fresh_coordinator_runtime_without_model_override(tmp_path):
    built, api, gid, _members = await setup(tmp_path, configured=False)
    try:
        engine_catalog(built)
        response = await api.put(f'/api/groups/{gid}/coordinator', json={
            'config': {'bindingId': built.binding.id, 'modelId': None, 'reasoningMode': 'low'},
            'expectedRevision': 0,
        })
        assert response.status_code == 200, response.text
        config = response.json()['group']['settings']['coordinator']
        assert config['modelId'] is None
        assert config['providerId'] is None
        assert config['reasoningMode'] == 'low'
        calls = install_responder(built, lambda call, all_calls: {'action': 'finish'})
        response = await api.post(f'/api/groups/{gid}/broadcast', json={'text': '请回答这个简单问题'})
        assert response.status_code == 200, response.text
        group = await completed(built, gid)
        assert group.thread.ended_reason == 'task_completed'
        assert len(calls) == 1
        assert calls[0]['binding'].id != built.binding.id
        assert calls[0]['binding'].default_model_id is None
        assert calls[0]['binding'].default_provider_id is None
        conversation = await built.repositories.conversations.get(calls[0]['cid'])
        assert conversation.model_id is None
        assert conversation.reasoning_mode == 'low'
    finally:
        await cleanup(built, api)


@pytest.mark.parametrize('available,degraded', [(True, True), (False, True), (False, False)])
async def test_empty_or_failed_catalog_cannot_silently_enable_engine_default(tmp_path, available, degraded):
    built, api, gid, _members = await setup(tmp_path, configured=False)
    try:
        engine_catalog(built, available=available, degraded=degraded)
        response = await api.put(f'/api/groups/{gid}/coordinator', json={
            'config': {'bindingId': built.binding.id}, 'expectedRevision': 0,
        })
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'model_catalog_unavailable'
        group = await built.repositories.collaborations.get(gid)
        assert not group.settings.get('coordinator')
    finally:
        await cleanup(built, api)


@pytest.mark.parametrize('selection', [{'modelId': 'invented-model'}, {'providerId': 'invented-provider'}])
async def test_engine_default_cannot_persist_unverified_selection(tmp_path, selection):
    built, api, gid, _members = await setup(tmp_path, configured=False)
    try:
        engine_catalog(built)
        response = await api.put(f'/api/groups/{gid}/coordinator', json={
            'config': {'bindingId': built.binding.id, **selection}, 'expectedRevision': 0,
        })
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'model_not_available'
    finally:
        await cleanup(built, api)


async def test_engine_default_reasoning_still_requires_advertised_native_level(tmp_path):
    built, api, gid, _members = await setup(tmp_path, configured=False)
    try:
        engine_catalog(built)
        response = await api.put(f'/api/groups/{gid}/coordinator', json={
            'config': {'bindingId': built.binding.id, 'reasoningMode': 'unadvertised'},
            'expectedRevision': 0,
        })
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'reasoning_not_supported'
    finally:
        await cleanup(built, api)
