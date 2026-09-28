"""Conversation and coordinator config persistence, validation and isolation."""
import asyncio

from fastapi.testclient import TestClient

from app.tests.test_batch16_backend import Harness
from app.tests.test_independent_coordinator import setup, cleanup, install_responder, completed
from tests.test_engine_config_options import OPTIONS


def test_config_round_trip_rejects_unknown_values_and_does_not_change_binding(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    built.driver.conversation_controls = lambda _: {'reasoning': False, 'approvalModes': [], 'configOptions': OPTIONS}
    first = asyncio.run(built.new_conversation())
    other = asyncio.run(built.new_conversation())
    try:
        with TestClient(built.app, headers={'Authorization': f'Bearer {built.token}'}) as api:
            url = f'/api/conversations/{first.id}'
            for patch in ({'allow-network': True}, {'my-policy': 'allow'}):
                result = api.patch(url, json={'configOptions': patch})
                assert result.status_code == 200, result.text
            assert api.get(url).json()['conversation']['configOptions'] == {'allow-network': True, 'my-policy': 'allow'}
            assert api.get(f'/api/conversations/{other.id}').json()['conversation']['configOptions'] == {}
            for invalid in ({'unknown': 'anything'}, {'allow-network': 'true'}, {'my-policy': 'invented'}):
                assert api.patch(url, json={'configOptions': invalid}).status_code == 501
            assert api.patch(url, json={'configOptions': {'allow-network': 1}}).status_code == 422
            assert api.patch(url, json={'configOptions': {'my-policy': 'allow', 'allow-network': True}}).status_code == 400
        assert asyncio.run(built.repositories.bindings.get(built.binding.id)) == built.binding
        assert asyncio.run(built.repositories.conversations.get(first.id)).config_options == {'allow-network': True, 'my-policy': 'allow'}
    finally:
        built.close()


async def test_group_config_is_snapshotted_and_tightening_host_policy_stops_current_task(tmp_path):
    built, api, gid, members = await setup(tmp_path, configured=False)
    try:
        built.driver.conversation_controls = lambda _: {'reasoning': False, 'approvalModes': ['host:ask', 'host:auto'], 'configOptions': OPTIONS}
        config = {'bindingId': built.binding.id, 'modelId': 'mock-small', 'providerId': 'mock-provider',
                  'approvalMode': 'host:auto', 'configOptions': {'allow-network': True}}
        result = await api.put(f'/api/groups/{gid}/coordinator', json={'config': config, 'expectedRevision': 0})
        assert result.status_code == 200, result.text
        calls = install_responder(built, lambda call, all_calls: None)
        result = await api.post(f'/api/groups/{gid}/broadcast', json={'text': 'synthetic coordinator task'})
        assert result.status_code == 200, result.text
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        execution = await built.repositories.conversations.get(calls[0]['cid'])
        assert execution.config_options == {'allow-network': True} and execution.approval_mode == 'host:auto'
        assert (await built.repositories.conversations.get(members[0]['conversationId'])).config_options == {}
        result = await api.put(f'/api/groups/{gid}/coordinator', json={'config': {**config, 'approvalMode': 'host:ask'}, 'expectedRevision': 1})
        assert result.status_code == 200, result.text
        assert (await completed(built, gid)).thread.status == 'stopped'
    finally:
        await cleanup(built, api)
