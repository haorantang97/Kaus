"""假 API server 的报文形状必须与探针报告逐字对得上。

这组用例是**夹具自身的保险**：如果有人把假服务器「修得更合理」（把 202 改成 200、
给 SSE 加上 ``event:`` 行、把 ``output_tokens`` 改成 ``completion_tokens``），
Driver 的测试就会在一个不存在的后端上跑绿。所以先测夹具。
"""

from __future__ import annotations

import json

import pytest

from drivers.hermes.http_client import HermesHttpClient, parse_sse_text
from drivers.hermes.testing.fake_api_server import (
    APPROVAL_400_BODY,
    FakeHermesApiServer,
    hold_script,
    malformed_script,
)


@pytest.fixture()
def server():
    fake = FakeHermesApiServer().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture()
def client(server):
    return HermesHttpClient(base_url=server.base_url, api_key=server.api_key)


async def test_health_needs_no_auth(server) -> None:
    anonymous = HermesHttpClient(base_url=server.base_url, api_key=None)
    response = await anonymous.request("GET", "/health", auth=False)
    assert response.status == 200
    assert response.json() == {
        "status": "ok",
        "platform": "hermes-agent",
        "version": "0.21.0",
    }


async def test_unauthenticated_v1_is_401_with_the_measured_body(server) -> None:
    anonymous = HermesHttpClient(base_url=server.base_url, api_key=None)
    response = await anonymous.request("GET", "/v1/models")
    assert response.status == 401
    assert response.json()["error"]["code"] == "gateway_auth_failed"


async def test_create_session_is_201_with_hermes_session_envelope(client, server) -> None:
    response = await client.request("POST", "/api/sessions", body={})
    assert response.status == 201
    payload = response.json()
    assert payload["object"] == "hermes.session"
    session = payload["session"]
    assert session["id"].startswith("api_")
    assert session["source"] == "api_server"
    # 0.21.0 起建会话时不再预设模型。
    assert session["model"] is None
    # 实测：**没有** X-Hermes-Session-Id 响应头，id 只能从 body 取。
    assert "x-hermes-session-id" not in response.headers


async def test_models_endpoint_returns_the_routing_alias_not_a_catalog(client) -> None:
    payload = (await client.request("GET", "/v1/models")).json()
    assert [m["id"] for m in payload["data"]] == ["hermes-agent"]


async def test_capabilities_features_match_the_report(client) -> None:
    features = (await client.request("GET", "/v1/capabilities")).json()["features"]
    assert features["runs_idempotency"] == {
        "supported": True,
        "durable": True,
        "retention_seconds": 86400,
    }
    assert features["session_continuity_header"] == "X-Hermes-Session-Id"
    assert features["cors"] is False
    for off in ("admin_config_rw", "jobs_admin", "memory_write_api", "audio_api"):
        assert features[off] is False
    for on in ("run_events_sse", "run_stop", "run_steer", "model_options", "skills_api"):
        assert features[on] is True


async def test_create_run_is_202_with_replayed_flag(client) -> None:
    response = await client.request(
        "POST",
        "/v1/runs",
        body={"input": "hi"},
        headers={"Idempotency-Key": "k1", "X-Hermes-Session-Id": "api_x"},
    )
    assert response.status == 202
    payload = response.json()
    assert payload["status"] == "started"
    assert payload["replayed"] is False
    assert payload["run_id"].startswith("run_")
    assert len(payload["run_id"]) == len("run_") + 32


async def test_idempotent_retry_returns_the_same_run(client) -> None:
    first = await client.request(
        "POST", "/v1/runs", body={"input": "hi"}, headers={"Idempotency-Key": "same"}
    )
    second = await client.request(
        "POST", "/v1/runs", body={"input": "hi"}, headers={"Idempotency-Key": "same"}
    )
    assert second.status == 202
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["replayed"] is True
    assert second.headers.get("idempotency-replayed") == "true"


async def test_sse_frames_are_data_only(client) -> None:
    run_id = (await client.request("POST", "/v1/runs", body={"input": "hi"})).json()["run_id"]
    frames = []
    async for event in client.stream_sse(f"/v1/runs/{run_id}/events"):
        frames.append(event)
    assert frames
    assert all(event.declared_event is None for event in frames), "不许有 event: 行"
    assert all(event.declared_id is None for event in frames), "不许有 id: 行"
    assert frames[0].event_name == "tool.started"
    assert frames[-1].event_name == "run.completed"


async def test_run_object_carries_usage_with_measured_field_names(client) -> None:
    run_id = (await client.request("POST", "/v1/runs", body={"input": "hi"})).json()["run_id"]
    async for _ in client.stream_sse(f"/v1/runs/{run_id}/events"):
        pass
    run = (await client.request("GET", f"/v1/runs/{run_id}")).json()
    assert run["object"] == "hermes.run"
    assert run["status"] == "completed"
    assert run["model"] == "hermes-agent", "run 对象的 model 是路由名，不是真实模型"
    assert set(run["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}
    assert "completion_tokens" not in run["usage"]


async def test_stop_returns_200_and_the_full_run_object(client, server) -> None:
    server.set_script(hold_script)
    run_id = (await client.request("POST", "/v1/runs", body={"input": "hi"})).json()["run_id"]
    response = await client.request("POST", f"/v1/runs/{run_id}/stop")
    assert response.status == 200
    assert response.json()["status"] == "cancelled"


async def test_approval_without_pending_request_is_400_verbatim(client) -> None:
    run_id = (await client.request("POST", "/v1/runs", body={"input": "hi"})).json()["run_id"]
    response = await client.request(
        "POST", f"/v1/runs/{run_id}/approval", body={"choice": "once", "all": False}
    )
    assert response.status == 400
    assert response.json() == json.loads(json.dumps(APPROVAL_400_BODY))
    # 这条 400 正是「取值集合」的实测出处。
    assert "always, deny, once, session" in response.json()["error"]["message"]


async def test_messages_pagination_shape(client) -> None:
    session_id = (await client.request("POST", "/api/sessions", body={})).json()["session"]["id"]
    payload = (
        await client.request("GET", f"/api/sessions/{session_id}/messages")
    ).json()
    assert payload["pagination"] == {
        "limit": 500,
        "offset": 0,
        "order": "latest",
        "returned": 0,
    }


async def test_malformed_frames_can_be_injected(client, server) -> None:
    """夹具要能造出「OpenAI 兼容分支混进来」的现场（规格 §3.2 的防御用例）。"""
    server.set_script(malformed_script)
    run_id = (await client.request("POST", "/v1/runs", body={"input": "hi"})).json()["run_id"]
    frames = [event async for event in client.stream_sse(f"/v1/runs/{run_id}/events")]
    assert frames[0].payload() is None, "[DONE] 不是 JSON"
    assert frames[1].event_name is None, "chat.completion.chunk 没有 event 键"


def test_fixture_file_and_server_stream_agree() -> None:
    """假服务器发的第一条帧与 fixture 文件的第一条必须同形。"""
    from pathlib import Path

    from drivers.hermes.testing.fake_api_server import RUN3_STREAM

    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "hermes_run_sse.txt"
    first = parse_sse_text(fixture.read_text(encoding="utf-8"))[0].payload()
    assert first["event"] == RUN3_STREAM[0]["event"]
    assert first["preview"] == RUN3_STREAM[0]["preview"]
    assert first["timestamp"] == RUN3_STREAM[0]["timestamp"]
