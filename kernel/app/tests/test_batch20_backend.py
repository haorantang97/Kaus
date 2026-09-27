"""批次二十（接入层）：能力写端点自述 + SSE 流健康自检。

- 第 2 件：``GET /projects/{id}/capabilities/_meta`` —— 前端不必再拿 ``PUT`` 一条
  不存在的能力去探「这个后端认不认写端点」（真机上 405 / 404 分不清是没路由还是
  中间件先答的）。
- 第 3 件：``GET /backends/{id}/debug/last-stream?conversation=`` —— 只在
  ``DASH_DEBUG=1`` 时挂载，只回**事件类型序列与计数**，一个字的内容都不带。

隔离同批次十七：SQLite 在 ``tmp_path``，Driver 是桩，不碰真实引擎、不读凭据。
"""

from __future__ import annotations

import asyncio
import json

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.tests.test_batch17_backend import Harness  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402


class _StreamDriver(MockDriver):
    """只多一条取证面：最近一次 run 的事件类型序列与计数。"""

    record = {
        "runId": "run_x",
        "eventTypes": ["tool.started", "tool.completed", "run.completed"],
        "counts": {"tool.started": 1, "tool.completed": 1, "run.completed": 1},
        "total": 3,
        "sawRunCompleted": True,
    }

    def debug_last_stream(self, conversation_id: str):
        return self.record if conversation_id.startswith("conversation:") else None


# --------------------------------------------------------------------------- #
# 第 2 件：能力写端点自述
# --------------------------------------------------------------------------- #


def test_capabilities_meta_says_the_write_endpoint_is_there(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/projects/{built.project_id}/capabilities/_meta"
            )
        assert response.status_code == 200
        body = response.json()
        assert body["writable"] is True
        assert body["methods"] == ["PUT", "DELETE"]
        assert body["projectId"] == built.project_id
    finally:
        built.close()


def test_capabilities_meta_does_not_shadow_a_real_capability_type(tmp_path):
    """``_meta`` 与 ``{capability_type}`` 同层：字面量段先匹配，参数化路由照旧可用。"""
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            blocked = test_client.put(
                f"/api/projects/{built.project_id}/capabilities/delegation/delegation",
                json={"blocked": True},
            )
            assert blocked.status_code == 200
            deleted = test_client.delete(
                f"/api/projects/{built.project_id}/capabilities/delegation/delegation"
            )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
    finally:
        built.close()


def test_capabilities_meta_404s_for_an_unknown_project(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get("/api/projects/project:nope/capabilities/_meta")
        assert response.status_code == 404
    finally:
        built.close()


# --------------------------------------------------------------------------- #
# 第 3 件：SSE 流健康自检
# --------------------------------------------------------------------------- #


def test_last_stream_is_not_mounted_by_default(tmp_path):
    built = Harness(tmp_path, driver=_StreamDriver())
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/backends/{built.driver.backend_id}/debug/last-stream"
            )
        assert response.status_code == 404
    finally:
        built.close()


def test_last_stream_returns_types_and_counts_only(tmp_path):
    built = Harness(tmp_path, driver=_StreamDriver(), debug=True)
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/backends/{built.driver.backend_id}/debug/last-stream",
                params={"conversation": conversation.id},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["captured"] is True
        assert body["stream"]["counts"]["run.completed"] == 1
        assert body["stream"]["total"] == 3
        # 只有类型与计数：没有 delta / preview / output 这些内容字段。
        blob = json.dumps(body)
        for forbidden in ("delta", "preview", "output", "content"):
            assert forbidden not in blob
    finally:
        built.close()


def test_last_stream_requires_a_conversation(tmp_path):
    """按会话取证：不给 ``?conversation=`` 就说清楚要什么，不回一份全局大杂烩。"""
    built = Harness(tmp_path, driver=_StreamDriver(), debug=True)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.get(
                f"/api/backends/{built.driver.backend_id}/debug/last-stream"
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "conversation_required"
    finally:
        built.close()
