from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.tests.test_batch16_backend import Harness


def test_permission_is_persisted_per_conversation_and_engine_is_immutable(tmp_path, monkeypatch):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    monkeypatch.setattr(built.driver, "conversation_controls", lambda _: {"reasoning": True, "approvalModes": ["ask", "read_only"]})
    first = asyncio.run(built.new_conversation())
    second = asyncio.run(built.new_conversation())
    try:
        with TestClient(built.app, headers={"Authorization": f"Bearer {built.token}"}) as client:
            response = client.patch(f"/api/conversations/{first.id}", json={"approvalMode": "read_only"})
            assert response.status_code == 200, response.text
            assert response.json()["conversation"]["approvalMode"] == "read_only"
            assert client.get(f"/api/conversations/{first.id}").json()["conversation"]["approvalMode"] == "read_only"
            assert client.get(f"/api/conversations/{second.id}").json()["conversation"]["approvalMode"] is None
            assert client.patch(f"/api/conversations/{first.id}", json={"approvalMode": "bypass"}).status_code == 501
            assert client.patch(f"/api/conversations/{first.id}", json={"agentBindingId": "binding:other"}).status_code in (400, 422)
            assert client.patch(f"/api/conversations/{first.id}", json={"modelId": "mock-large", "reasoningMode": "xhigh"}).status_code == 200
            assert client.patch(f"/api/conversations/{first.id}", json={"modelId": "mock-small"}).status_code == 400
        saved = asyncio.run(built.repositories.conversations.get(first.id))
        assert saved.approval_mode == "read_only"
        assert saved.agent_binding_id == first.agent_binding_id
        assert saved.model_id == "mock-large"
        assert asyncio.run(built.repositories.bindings.get(built.binding.id)) == built.binding
    finally:
        built.close()
