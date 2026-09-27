"""批次二十七热修：spawn 首句 + 投递状态，全链路走**假 Hermes 网关**。

为什么不是 mock
----------------
批次二十二/二十六的 Group 测试用 MockDriver 验形状——那一层永远是绿的，因为
MockDriver 的 ``send_message`` 从不拒绝第二个回合、``start_runtime`` 也不需要
先建原生会话。真机上不通过的两处（spawn 带首句没回复、定向发送回 HTTP 500）
恰好都发生在「Driver 真的要和网关说话」这一段，所以这一份把最外面那层换成
:class:`~drivers.hermes.testing.fake_api_server.FakeHermesApiServer`。

隔离：假网关只绑 127.0.0.1 随机端口；``HERMES_HOME`` 指向 ``tmp_path``；
key 是假服务器现场生成的串，只经进程环境变量交给 Driver——不读任何 ``.env``、
不碰真实 ``~/.hermes``、不起任何真引擎。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import session_bootstrap  # noqa: E402

from app.api.router import build_domain_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.hermes.testing.fake_api_server import (  # noqa: E402
    FakeHermesApiServer,
    hold_script,
)

KEY_ENV_NAME = "DASH_TEST_GROUP_GATEWAY_KEY"


class GatewayHarness:
    """假网关 + 一条 native-http backend + 会话路由 + Group 路由（真机的挂法）。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.server = FakeHermesApiServer().start()
        monkeypatch.setenv(KEY_ENV_NAME, self.server.api_key)
        self.home = tmp_path / "hermes-home"
        self.home.mkdir(parents=True, exist_ok=True)
        # 一份**临时**的 config.yaml：物化真写时要先备份它（AD-149）。
        # 真实 ~/.hermes 一个字都不碰。
        (self.home / "config.yaml").write_text(
            "model:\n  default: hermes-agent\n", encoding="utf-8"
        )
        self.config = {
            "features": {"session_host_v1": True, "session_host_background": False},
            "backends": [
                {
                    "id": "backend:hermes",
                    "driver": "native-http",
                    "base_url": self.server.base_url,
                    "home": str(self.home),
                    "profile": "default",
                    "mode": "adopted",
                    "key_ref": f"credential-store:{KEY_ENV_NAME}",
                    "env_keys": [KEY_ENV_NAME],
                    "hermes_bin": sys.executable,
                }
            ],
        }
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        # Historical transport/delivery semantics remain covered in legacy mode.
        from functools import partial
        from app.api import group_router
        monkeypatch.setattr(group_router, 'build_group_router', partial(group_router.build_group_router, coordinator_enabled=False))
        self.runtime = session_bootstrap.open_session_runtime(
            config=self.config,
            repositories=self.repositories,
            idle_timeout=timedelta(minutes=15),
            token_path=tmp_path / "session_api.token",
        )
        self.token = self.runtime.token_path.read_text(encoding="utf-8").strip()
        self.app = fastapi.FastAPI()
        self.app.include_router(self.runtime.router)
        assert self.runtime.group_router is not None
        self.app.include_router(self.runtime.group_router)
        # 物化端点（批次二十七热修③）也挂上：它问的「这条 Binding 忙不忙」
        # 与 Group 投递问的是同一件事，得在同一条真链路上验。
        assert self.runtime.binding_router is not None
        self.app.include_router(self.runtime.binding_router)
        self.app.include_router(build_domain_router(self.repositories))
        self.port = _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port, log_level="warning"
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self._server.started, "本地测试服务没起来"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="hermes", driver_kind="native"))
        project = await repos.projects.save(
            Project.create(slug="media", display_name="Media")
        )
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend="hermes",
                display_name="假网关绑定",
                native_scope_ref="default",
                is_default=True,
            )
        )
        self.project_id = project.id

    def events_of(self, conversation_id: str) -> list:
        """直接问 Event Store 要这条会话的事件（不开 SSE，测试里更直接）。"""
        return asyncio.run(
            self.runtime.session_host.event_store.replay(conversation_id)
        )

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        asyncio.run(self.runtime.aclose())
        self.unit_of_work.close()
        self.server.stop()


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    built = GatewayHarness(tmp_path, monkeypatch)
    asyncio.run(built.seed())
    built.start()
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def client(harness):
    with httpx.Client(
        base_url=harness.base_url,
        timeout=30.0,
        headers={"Authorization": f"Bearer {harness.token}"},
    ) as http_client:
        yield http_client


def _new_group(client: httpx.Client, title: str = "热修组") -> dict:
    response = client.post("/api/groups", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _kinds(envelopes) -> list[str]:
    out = []
    for envelope in envelopes:
        event = envelope.event
        name = getattr(event, "name", None)
        out.append(f"{event.type}:{name}" if name else event.type)
    return out




def _wait_for(harness, conversation_id: str, kind: str, timeout: float = 20.0):
    """轮询 Event Store 直到出现某种事件；返回最后一次读到的种类清单。"""
    deadline = time.monotonic() + timeout
    kinds: list[str] = []
    while time.monotonic() < deadline:
        kinds = _kinds(harness.events_of(conversation_id))
        if kind in kinds:
            return kinds
        time.sleep(0.05)
    return kinds


def _assert_plain(detail: str) -> None:
    """投递说明必须是**人话**：没有异常类名、没有状态码、没有 Markdown 标记。"""
    assert detail, "detail 不能是空的：没说为什么等于没说"
    for leak in ("HTTP ", "Error:", "Error：", "**", "Traceback"):
        assert leak not in detail, f"{leak!r} 不该出现在给用户看的说明里：{detail}"


# --------------------------------------------------------------------------- #
# ① spawn 的首句
# --------------------------------------------------------------------------- #


def test_spawn_initial_message_reaches_the_new_conversation(harness, client):
    """首句要真的落在**这条**会话上：user.message 在前，引擎的回合在后。"""
    group = _new_group(client)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={
            "projectId": harness.project_id,
            "title": "新成员",
            "initialMessage": "你好，先自我介绍一下",
        },
    )
    assert spawned.status_code == 201, spawned.text
    initial = spawned.json()["initialMessage"]
    assert initial["sent"] is True and initial["error"] is None
    # 批次二十七：「引擎收下了」不等于「这一轮起来了」——runId 是那个区别。
    assert initial["runId"] and initial["runId"].startswith("run_")
    assert initial["runIdPending"] is False

    conversation_id = spawned.json()["conversation"]["id"]
    kinds = _wait_for(harness, conversation_id, "run.completed")
    assert "extension.event:user.message" in kinds, kinds
    assert "message.completed" in kinds, kinds
    assert kinds.index("extension.event:user.message") < kinds.index("run.started")


def test_spawn_says_why_the_first_message_did_not_go_out(harness, client):
    """网关不在时：spawn 仍然 201（不回滚），但首句失败要带 code + 人话 + 修法。"""
    group = _new_group(client)
    harness.server.stop()  # 真机上最常见的一种：`hermes gateway run` 没在跑
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id, "initialMessage": "在吗"},
    )
    # AD-105：首句发不出去不回滚 —— 会话与成员都在库里。
    assert spawned.status_code == 201, spawned.text
    body = spawned.json()
    assert body["member"]["participationState"] == "active"
    error = body["initialMessage"]["error"]
    assert body["initialMessage"]["sent"] is False
    # 认得出根因就报根因，而不是一律 `runtime_start_failed` + 一个异常类名。
    assert error["code"] == "gateway_unreachable"
    _assert_plain(error["message"])
    assert "hermes gateway run" in error["hint"]


def test_spawn_adopts_a_model_the_engine_will_not_take(harness, client):
    """快照与引擎实际会用的不是一个 → **发得出去**（AD-155 / 批次三十一）。

    这条测试原来断言「说清楚为什么没发出去」。真机复测
    （``docs/quality/verify-batch30-retest.md`` ①③）证明那条规则的代价太大：
    用户把项目配置物化到引擎、``model.default`` 一变，所有旧会话的组投递全成
    ``failed``。改判后这里守的是「组投递不再因为过期快照失败」。
    """
    # Legacy gateways cannot honor an explicit per-run model and still adopt
    # their default. Current gateways send the requested model downstream.
    harness.server.capabilities = {**harness.server.capabilities, "features": {**harness.server.capabilities["features"], "session_model_lock": False}}
    group = _new_group(client)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={
            "projectId": harness.project_id,
            "modelId": "no-such-model",
            "initialMessage": "你好",
        },
    )
    assert spawned.status_code == 201, spawned.text
    initial = spawned.json()["initialMessage"]
    assert initial["sent"] is True
    assert initial["error"] is None
    conversation_id = spawned.json()["member"]["conversationId"]
    # 快照已经被采纳成引擎的有效模型：界面上那句「这条会话指定模型为 X，而这台
    # 引擎实际会用 Y」自此不再出现。
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["conversation"]["modelId"] == "hermes-agent"


# --------------------------------------------------------------------------- #
# ② 投递状态
# --------------------------------------------------------------------------- #


def test_a_paused_member_short_circuits_before_any_gateway_call(harness, client):
    """暂停的成员：``skipped_paused``，而且**一次网关往返都不发生**。"""
    group = _new_group(client)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id},
    ).json()
    member_id = spawned["member"]["id"]
    assert client.post(
        f"/api/groups/{group['id']}/members/{member_id}/pause"
    ).status_code == 200

    before = len(harness.server.runs)
    routed = client.post(
        f"/api/groups/{group['id']}/members/{member_id}/send", json={"text": "点名"}
    )
    assert routed.status_code == 200, routed.text
    row = routed.json()["deliveries"][0]
    assert row["status"] == "skipped_paused"
    assert row["reason"] == "member_paused"
    _assert_plain(row["detail"])
    # 「在任何投递动作之前短路」= 网关上没有多出一个 run。
    assert len(harness.server.runs) == before


def test_a_busy_member_gets_wait_a_moment_not_an_http_status(harness, client):
    """上一轮还在跑时定向发送：``failed`` + ``turn_already_running`` + 人话。"""
    group = _new_group(client)
    harness.server.set_script(hold_script)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id, "initialMessage": "跑个长活"},
    ).json()
    member_id = spawned["member"]["id"]
    conversation_id = spawned["conversation"]["id"]
    _wait_for(harness, conversation_id, "run.started")

    again = client.post(
        f"/api/groups/{group['id']}/members/{member_id}/send", json={"text": "再来一句"}
    )
    assert again.status_code == 200, again.text
    row = again.json()["deliveries"][0]
    assert row["status"] == "failed"
    assert row["reason"] == "turn_already_running"
    _assert_plain(row["detail"])
    assert "上一轮" in row["detail"]
    # 短路发生在发送之前：不该给这条会话留下一条永远等不到回复的用户消息。
    assert _kinds(harness.events_of(conversation_id)).count(
        "extension.event:user.message"
    ) == 1


def test_a_gateway_refusal_is_translated_not_forwarded(harness, client):
    """站内以为空闲、网关拒绝第二个 run：翻成「等一等」，不把 500 原样上 wire。"""
    group = _new_group(client)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id, "initialMessage": "第一句"},
    ).json()
    member_id = spawned["member"]["id"]
    _wait_for(harness, spawned["conversation"]["id"], "run.completed")

    harness.server.fail_runs()  # 500 + error.code=run_in_progress（真机形状）
    routed = client.post(
        f"/api/groups/{group['id']}/members/{member_id}/send", json={"text": "第二句"}
    )
    assert routed.status_code == 200, routed.text
    row = routed.json()["deliveries"][0]
    assert row["status"] == "failed"
    assert row["reason"] == "turn_already_running"
    _assert_plain(row["detail"])


def test_an_unclassified_gateway_refusal_keeps_the_gateways_own_words(harness, client):
    """认不出来的拒绝：带上网关自己那句话，但状态码留在日志里、不上 wire。"""
    group = _new_group(client)
    spawned = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id, "initialMessage": "第一句"},
    ).json()
    member_id = spawned["member"]["id"]
    _wait_for(harness, spawned["conversation"]["id"], "run.completed")

    harness.server.fail_runs(
        status=500,
        body={"error": {"message": "配额用完了", "code": "quota_exhausted"}},
    )
    row = client.post(
        f"/api/groups/{group['id']}/members/{member_id}/send", json={"text": "第二句"}
    ).json()["deliveries"][0]
    assert row["status"] == "failed"
    assert row["reason"] == "run_submit_rejected"
    assert "配额用完了" in row["detail"]
    _assert_plain(row["detail"])


def test_broadcast_mixes_skipped_and_busy_without_stopping_anyone(harness, client):
    """真机 C3 的原样复现：一个暂停 + 一个正在跑，各自拿到对的状态。"""
    group = _new_group(client)
    paused = client.post(
        f"/api/groups/{group['id']}/spawn", json={"projectId": harness.project_id}
    ).json()
    client.post(f"/api/groups/{group['id']}/members/{paused['member']['id']}/pause")

    harness.server.set_script(hold_script)
    busy = client.post(
        f"/api/groups/{group['id']}/spawn",
        json={"projectId": harness.project_id, "initialMessage": "跑个长活"},
    ).json()
    _wait_for(harness, busy["conversation"]["id"], "run.started")

    routed = client.post(
        f"/api/groups/{group['id']}/broadcast",
        json={
            "text": "两位都看一下",
            "targetMemberIds": [paused["member"]["id"], busy["member"]["id"]],
        },
    )
    assert routed.status_code == 200, routed.text
    rows = {row["memberId"]: row for row in routed.json()["deliveries"]}
    assert rows[paused["member"]["id"]]["status"] == "skipped_paused"
    assert rows[busy["member"]["id"]]["reason"] == "turn_already_running"
    for row in rows.values():
        _assert_plain(row["detail"])
    # 落在组时间线上的那一行与响应体是同一份账。
    timeline = client.get(f"/api/groups/{group['id']}/messages").json()
    sent = next(
        message for message in timeline["messages"]
        if message["id"] == routed.json()["message"]["id"]
    )
    assert sent["deliveries"] == routed.json()["deliveries"]
    # Group 投递失败会停止本次讨论，但不会中断成员已经在执行的任务。
    assert "run.interrupted" not in _kinds(harness.events_of(busy["conversation"]["id"]))


# --------------------------------------------------------------------------- #
# 会话端点：网关的状态码不该变成我们自己的 500
# --------------------------------------------------------------------------- #


def test_send_message_maps_a_busy_turn_to_409(harness, client):
    """``POST /conversations/{id}/messages`` 在上一轮还在跑时回 409，不是 500。"""
    harness.server.set_script(hold_script)
    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "占着"},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    first = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "长活"}
    )
    assert first.status_code == 202, first.text
    _wait_for(harness, conversation_id, "run.started")

    second = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "插一句"}
    )
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "turn_already_running"
    _assert_plain(error["message"])


def test_send_message_maps_a_gateway_refusal_to_502(harness, client):
    """网关拒绝提交 → 502 ``message_rejected``；它的状态码不是我们的状态码。"""
    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "被拒的"},
    ).json()
    harness.server.fail_runs(
        status=500, body={"error": {"message": "配额用完了", "code": "quota_exhausted"}}
    )
    refused = client.post(
        f"/api/conversations/{created['id']}/messages", json={"text": "试一句"}
    )
    assert refused.status_code == 502, refused.text
    error = refused.json()["error"]
    assert error["code"] == "message_rejected"
    assert "配额用完了" in error["message"]
    _assert_plain(error["message"])


# --------------------------------------------------------------------------- #
# ③ 物化的「忙」判据：跑没跑完，而不是 runtime 活没活着
# --------------------------------------------------------------------------- #


def test_materialize_is_blocked_while_a_run_is_actually_in_flight(harness, client):
    """真在跑 → 409 ``binding_busy``，并说得出该去停哪一条（带标题）。"""
    harness.server.set_script(hold_script)
    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "正在跑的那条"},
    ).json()
    assert client.post(
        f"/api/conversations/{created['id']}/messages", json={"text": "长活"}
    ).status_code == 202
    _wait_for(harness, created["id"], "run.started")

    refused = client.post(
        f"/api/bindings/{harness.binding.id}/materialize", params={"confirm": "1"}
    )
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "binding_busy"
    titles = [row["title"] for row in error["activeConversations"]]
    assert titles == ["正在跑的那条"]
    assert error["activeConversationIds"] == [created["id"]]
    # dry-run 不受此限（批次二十四的口径没变）。
    assert client.post(
        f"/api/bindings/{harness.binding.id}/materialize"
    ).status_code == 200


def test_materialize_writes_once_the_run_is_done_even_if_the_runtime_lives_on(
    harness, client
):
    """真机③：会话页显示 Idle 了，物化还一直 409。

    根因是「忙」按 ``is_active``（有没有活跃 Runtime）判——而一轮跑完之后
    Runtime 照旧活着（要等空闲回收才收掉）。判据改成「这一轮跑没跑完」之后，
    **同一条**还活着的 runtime 不再挡写入。
    """
    # 先给这条 Binding 一个真的会落盘的旋钮，否则「写成功」与「没什么可写」
    # 在响应体上分不开（backupPath 都是 null）。
    assert client.patch(
        f"/api/bindings/{harness.binding.id}",
        json={"runtimeConfig": {"approval_mode": "ask"}},
    ).status_code == 200

    created = client.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "跑完了的那条"},
    ).json()
    assert client.post(
        f"/api/conversations/{created['id']}/messages", json={"text": "一句话"}
    ).status_code == 202
    _wait_for(harness, created["id"], "run.completed")

    # 前置事实：runtime **还活着**（这正是此前误挡的来路），但这一轮已经结束。
    assert harness.runtime.session_host.is_active(created["id"]) is True
    conversation = asyncio.run(
        harness.repositories.conversations.get(created["id"])
    )
    assert (
        asyncio.run(harness.runtime.session_host.run_state_of(conversation)) == "idle"
    )

    written = client.post(
        f"/api/bindings/{harness.binding.id}/materialize", params={"confirm": "1"}
    )
    assert written.status_code == 200, written.text
    body = written.json()
    assert body["dryRun"] is False
    # 真写前必须先备份（AD-149），备份路径就在临时 HERMES_HOME 里。
    assert body["backupPath"]
    assert str(harness.home) in body["backupPath"]
