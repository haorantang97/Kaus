"""Normal chats and isolated coordinators share native approval selection."""
import asyncio

import pytest

from app.tests.test_independent_coordinator import setup, cleanup, install_responder, completed
from app.tests.test_batch22_groups import _new_conversation

pytestmark = pytest.mark.asyncio


async def test_conversation_native_selection_persists_without_changing_binding_or_other_chats(tmp_path):
    built, api, _gid, _members = await setup(tmp_path, configured=False)
    try:
        built.driver.conversation_controls = lambda binding: {
            "reasoning": False, "approvalModes": ["ask", "native:workspace-write"],
            "approvalOptions": [{"id": "native:workspace-write", "nativeId": "workspace-write", "name": "Workspace write"}],
        }
        first = await _new_conversation(built, "one")
        second = await _new_conversation(built, "two")
        response = await api.patch(f"/api/conversations/{first.id}", json={"approvalMode": "native:workspace-write"})
        assert response.status_code == 200, response.text
        assert (await built.repositories.conversations.get(first.id)).approval_mode == "native:workspace-write"
        assert (await built.repositories.conversations.get(second.id)).approval_mode is None
        assert (await built.repositories.bindings.get(built.binding.id)).runtime_config.get("approval_mode") is None
        response = await api.patch(f"/api/conversations/{first.id}", json={"approvalMode": "native:unknown"})
        assert response.status_code == 501, response.text
        assert response.json()["error"]["code"] == "conversation_approval_unsupported"
        assert (await built.repositories.conversations.get(first.id)).approval_mode == "native:workspace-write"
    finally:
        await cleanup(built, api)


async def test_coordinator_native_policy_is_validated_snapshotted_and_revoked_on_change(tmp_path):
    built, api, gid, members = await setup(tmp_path, configured=False)
    try:
        built.driver.conversation_controls = lambda binding: {
            "reasoning": False, "approvalModes": ["ask", "native:smart_approve", "native:chat"],
        }
        config = {"bindingId": built.binding.id, "modelId": "mock-small", "providerId": "mock-provider", "approvalMode": "native:smart_approve"}
        response = await api.put(f"/api/groups/{gid}/coordinator", json={"config": config, "expectedRevision": 0})
        assert response.status_code == 200, response.text
        calls = install_responder(built, lambda call, all_calls: None)
        response = await api.post(f"/api/groups/{gid}/broadcast", json={"text": "需要组长处理"})
        assert response.status_code == 200, response.text
        async with asyncio.timeout(3):
            while not calls:
                await asyncio.sleep(.01)
        runtime_conv = await built.repositories.conversations.get(calls[0]["cid"])
        assert runtime_conv.approval_mode == "native:smart_approve"
        assert calls[0]["binding"].runtime_config["approval_mode"] == "native:smart_approve"
        assert (await built.repositories.bindings.get(built.binding.id)).runtime_config.get("approval_mode") is None
        original = await built.repositories.conversations.get(members[0]["conversationId"])
        assert original.approval_mode is None
        changed = {**config, "approvalMode": "native:chat"}
        response = await api.put(f"/api/groups/{gid}/coordinator", json={"config": changed, "expectedRevision": 1})
        assert response.status_code == 200, response.text
        group = await completed(built, gid)
        assert group.thread.status == "stopped"
        assert group.settings["coordinator"]["approvalMode"] == "native:chat"
        assert not built.group_router.group_coordinator.tasks
        response = await api.put(f"/api/groups/{gid}/coordinator", json={"config": {**changed, "approvalMode": "native:unknown"}, "expectedRevision": 2})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "approval_not_supported"
        group = await built.repositories.collaborations.get(gid)
        assert group.settings["coordinator"]["approvalMode"] == "native:chat"
    finally:
        await cleanup(built, api)
