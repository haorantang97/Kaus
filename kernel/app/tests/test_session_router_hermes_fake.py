"""会话接口 → Hermes HTTP Driver → **假网关** 的端到端接线测试（批次六）。

和 `test_session_router.py` 的分工
----------------------------------
那一份用 MockDriver 验**接口形状**；这一份把整条链路换成真的 Hermes Driver，
只把最外面的 `hermes gateway run` 换成
:class:`~drivers.hermes.testing.fake_api_server.FakeHermesApiServer`。所以这里验的是
**接线**：`dashboard-config.json` 的一条 `backends[]` → `GatewayConfig` →
`HermesDriver` → HTTP/SSE → 公共信封。沙盒里没有 Hermes，真机那一段由
`scripts/session_smoke.py --driver native-http` 承担。

覆盖
----
1. 一条 `native-http` 配置真的能装出可用的 Driver（`session_bootstrap.build_registry`，
   凭据只走 `env_keys` + `credential-store:` 引用，配置里零明文）；
2. 一个完整回合：`POST /messages` → SSE → `message.delta` / `tool.*` /
   `usage.updated` / `message.completed` / `run.completed`；
3. **审批往返**：`permission.requested` → `POST /interactions/{id}`（deny）→
   `permission.resolved`，回合随后收敛到终态；
4. `POST /stop` 之后 runtime 不再活跃；
5. AD-41 / AD-28：后台任务把 `probe_state` / `installed` / `version` 写回领域库，
   并经 `GET /api/backends/{id}` 露出来（真机上这一格就是 `installed=true`）。

隔离：假网关只绑 127.0.0.1 的随机端口；`HERMES_HOME` 指向 `tmp_path`；
key 用假服务器现场生成的串，经**进程环境变量**交给 Driver，不读任何 `.env`、
不碰任何真实 `~/.hermes`。
"""

from __future__ import annotations

import asyncio
import json
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
    BACKEND_VERSION,
    FakeHermesApiServer,
    approval_script,
)

PROJECT_SLUG = "wiring"

#: 交给 Driver 的环境变量**名**。值是假服务器现场生成的，只活在进程环境里。
KEY_ENV_NAME = "DASH_TEST_GATEWAY_KEY"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


class Harness:
    """假网关 + 一条 `native-http` backend 配置 + 会话/领域路由。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.server = FakeHermesApiServer().start()
        monkeypatch.setenv(KEY_ENV_NAME, self.server.api_key)
        self.home = tmp_path / "hermes-home"
        self.home.mkdir(parents=True, exist_ok=True)
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
                    # AD-48：配置里只有**引用**与变量名，没有值。
                    "key_ref": f"credential-store:{KEY_ENV_NAME}",
                    "env_keys": [KEY_ENV_NAME],
                    # 真机上是 `hermes`；这里给一个一定存在的可执行文件，
                    # 好让 probe 的 installed 位走到 true 这一侧（真机同理）。
                    "hermes_bin": sys.executable,
                }
            ],
        }
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.runtime = session_bootstrap.open_session_runtime(
            config=self.config,
            repositories=self.repositories,
            idle_timeout=timedelta(minutes=15),
            # D-17：会话端点一律鉴权；token 文件落在临时目录，客户端夹具带 Bearer。
            token_path=tmp_path / "session_api.token",
        )
        self.token = self.runtime.token_path.read_text(encoding="utf-8").strip()
        self.app = fastapi.FastAPI()
        self.app.include_router(self.runtime.router)
        self.app.include_router(build_domain_router(self.repositories))
        # SSE 必须走**真的**socket：TestClient 会把整个响应体读完才返回，
        # 一条永不结束的事件流在它手里就是一次死锁。真机形态也正是这个。
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
            Project.create(slug=PROJECT_SLUG, display_name="接线")
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

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        asyncio.run(self.runtime.aclose())
        self.unit_of_work.close()
        self.server.stop()

    # --- 便捷方法 ------------------------------------------------------- #

    def new_conversation(self, client: httpx.Client, title: str = "接线冒烟") -> str:
        response = client.post(
            f"/api/projects/{self.project_id}/conversations",
            json={"bindingId": self.binding.id, "title": title},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    built = Harness(tmp_path, monkeypatch)
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


def stream_events(client: httpx.Client, conversation_id: str, *, until, limit: int = 200):
    """开一条 SSE，逐条读到 ``until(event) is True`` 为止。返回读到的公共事件。

    ``until`` 而不是固定条数：回合长度取决于剧本，写死条数只会让测试在剧本
    一改的时候莫名其妙地红。
    """
    collected: list[dict] = []
    with client.stream(
        "GET", f"/api/conversations/{conversation_id}/events"
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if not line or line.startswith(":"):
                continue
            assert line.startswith("data: "), line
            envelope = json.loads(line[len("data: ") :])
            collected.append(envelope)
            if until(envelope["event"]) or len(collected) >= limit:
                break
    return collected


def types_of(envelopes) -> list[str]:
    return [envelope["event"]["type"] for envelope in envelopes]


# --------------------------------------------------------------------------- #
# 1. 装配（AD-53：一条配置 → 一个真 Driver）
# --------------------------------------------------------------------------- #


def test_a_native_http_entry_registers_a_native_driver(harness):
    registry = harness.runtime.registry
    assert registry.backend_ids() == ("backend:hermes",)
    assert registry.get("backend:hermes").driver_kind == "native"
    assert harness.runtime.warnings == ()


def test_the_gateway_config_only_carries_the_six_allowed_keys(harness):
    driver = harness.runtime.registry.get("backend:hermes")
    gateway = driver.default_gateway
    assert gateway.host == "127.0.0.1"
    assert gateway.port == harness.server.port
    assert gateway.hermes_home == harness.home
    assert gateway.profile == "default"
    assert gateway.mode == "adopted"
    # 规格 §1.3：配置里出现的是 ref，不是 key 本身。
    assert gateway.key_ref == f"credential-store:{KEY_ENV_NAME}"
    assert harness.server.api_key not in json.dumps(harness.config)


# --------------------------------------------------------------------------- #
# 2. 一个完整回合
# --------------------------------------------------------------------------- #


def test_a_full_turn_streams_public_events(harness, client):
    conversation_id = harness.new_conversation(client)
    accepted = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "Reply with exactly HERMES-PROBE-TOOL-MARKER"},
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["runId"] and body["runId"].startswith("run_")
    assert body["runIdPending"] is False

    envelopes = stream_events(
        client, conversation_id, until=lambda e: e["type"] == "run.completed"
    )
    kinds = types_of(envelopes)
    assert "message.delta" in kinds
    assert "tool.started" in kinds and "tool.completed" in kinds
    # AD-55 的收尾顺序：usage → message.completed → 终态。
    assert kinds.index("usage.updated") < kinds.index("message.completed")
    assert kinds.index("message.completed") < kinds.index("run.completed")
    completed = [e for e in envelopes if e["event"]["type"] == "message.completed"]
    assert completed[-1]["event"]["text"] == "HERMES-PROBE-TOOL-MARKER"
    # Driver 产出的每条信封都挂在同一条 run 上，且带的是公共 id（AD-37 的合成规则）。
    # 用户自己那句话（批次八第 6 件）不属于任何一轮——它是**发起**这一轮的东西，
    # run.started 还没发生，所以它的 runId 是 null，这里把它排除在外。
    from_driver = [
        e
        for e in envelopes
        if (e["event"]["type"], e["event"].get("name")) != ("extension.event", "user.message")
    ]
    assert {e["runId"] for e in from_driver} == {body["runId"]}


def test_stop_releases_the_runtime_but_keeps_the_conversation(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}
    )
    stream_events(client, conversation_id, until=lambda e: e["type"] == "run.completed")

    stopped = client.post(f"/api/conversations/{conversation_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False
    snapshot = client.get(f"/api/conversations/{conversation_id}").json()
    assert snapshot["runtime"]["active"] is False
    # v1.0 §9.4：停 Runtime 不删会话，也不丢原生映射。
    assert snapshot["conversation"]["nativeSessionId"].startswith("api_")


# --------------------------------------------------------------------------- #
# 3. 审批往返（规格 §2.8 / §8-②）
# --------------------------------------------------------------------------- #


def test_permission_round_trip_denies_and_the_run_still_converges(harness, client):
    harness.server.set_script(approval_script)
    conversation_id = harness.new_conversation(client)
    client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "Use the terminal tool to run: rm -f /tmp/nope"},
    )

    requested: dict = {}
    resolved: list[dict] = []

    def answer_when_asked(event: dict) -> bool:
        """审批必须在**流还开着**的时候答——等流结束再答就永远闭不了环。"""
        if event["type"] == "permission.requested" and not requested:
            requested.update(event["request"])
            answer = client.post(
                f"/api/conversations/{conversation_id}"
                f"/interactions/{event['request']['requestId']}",
                json={"kind": "permission", "cancelled": True},
            )
            assert answer.status_code == 200, answer.text
        if event["type"] == "permission.resolved":
            resolved.append(event)
        return event["type"] == "run.completed"

    envelopes = stream_events(client, conversation_id, until=answer_when_asked)
    kinds = types_of(envelopes)

    assert requested, "没有收到 permission.requested"
    assert requested["detail"].startswith("rm -f")
    assert {option["optionId"] for option in requested["options"]} == {
        "allow_once",
        "allow_session",
        "allow_always",
        "deny",
    }
    assert resolved and resolved[-1]["decision"] == "deny"
    assert kinds.index("permission.requested") < kinds.index("permission.resolved")
    assert "run.completed" in kinds
    # 被拒的这一轮不会产生任何工具执行。
    assert "tool.started" not in kinds


def test_answering_an_interaction_without_a_live_run_is_a_clean_error(harness, client):
    conversation_id = harness.new_conversation(client)
    answer = client.post(
        f"/api/conversations/{conversation_id}/interactions/appr_nope",
        json={"kind": "permission", "cancelled": True},
    )
    assert answer.status_code == 409
    assert answer.json()["error"]["code"] == "runtime_not_active"


# --------------------------------------------------------------------------- #
# 4. probe_state 写回（AD-41 / AD-28）
# --------------------------------------------------------------------------- #


def test_the_background_round_writes_installed_and_version_back(harness, client):
    outcome = asyncio.run(harness.runtime.maintenance.run_once())
    assert outcome["probed"] == {"backend:hermes": "available"}

    wire = client.get("/api/backends/backend:hermes").json()
    # 真机上这三格就是验收点：装了、通了、版本对得上。
    assert wire["installed"] is True
    assert wire["probeState"] == "available"
    assert wire["version"] == BACKEND_VERSION
    assert wire["lastProbeAt"] is not None
    # 能力来自 /v1/capabilities 的运行时协商，不是写死的。
    assert wire["capabilities"]["ui"]["card"]["streaming"] == "supported"


def test_probe_reports_unavailable_when_the_gateway_is_gone(harness):
    harness.server.stop()
    outcome = asyncio.run(harness.runtime.maintenance.run_once())
    # N §13.1：连不上是显式状态，不是异常，也不是「未知」。
    assert outcome["probed"] == {"backend:hermes": "unavailable"}


# --------------------------------------------------------------------------- #
# 5. 凭据边界
# --------------------------------------------------------------------------- #


def test_the_key_never_leaves_the_authorization_header(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    envelopes = stream_events(
        client, conversation_id, until=lambda e: e["type"] == "run.completed"
    )
    blob = json.dumps(envelopes, ensure_ascii=False)
    snapshot = client.get(f"/api/conversations/{conversation_id}").text
    assert harness.server.api_key not in blob
    assert harness.server.api_key not in snapshot
    # 但假服务器确实收到了它——否则这条断言就只是「没连上」。
    assert any(
        headers.get("Authorization") == f"Bearer {harness.server.api_key}"
        for _method, _path, headers in harness.server.requests
    )


def test_a_key_ref_outside_env_keys_cannot_read_the_environment(monkeypatch):
    """`credential-store:` 只认本条 backend 自己声明过的变量名。"""
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-be-readable")
    specs, warnings = session_bootstrap.parse_backend_specs(
        {
            "backends": [
                {
                    "id": "hermes",
                    "driver": "native-http",
                    "base_url": "http://127.0.0.1:18642",
                    "env_keys": [KEY_ENV_NAME],
                }
            ]
        }
    )
    assert warnings == ()
    store = session_bootstrap.env_credential_store(specs[0])
    assert store("SOME_OTHER_SECRET") is None


# --------------------------------------------------------------------------- #
# 6. 断线只影响订阅（AD-61）
# --------------------------------------------------------------------------- #


def test_reconnecting_with_after_replays_the_missing_events(harness, client):
    conversation_id = harness.new_conversation(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"})
    first = stream_events(
        client, conversation_id, until=lambda e: e["type"] == "message.delta"
    )
    cutoff = first[0]["sequence"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with client.stream(
            "GET", f"/api/conversations/{conversation_id}/events?after={cutoff}"
        ) as response:
            replayed = []
            for line in response.iter_lines():
                if not line or line.startswith(":"):
                    continue
                replayed.append(json.loads(line[len("data: ") :]))
                if replayed[-1]["event"]["type"] == "run.completed":
                    break
            if replayed and replayed[-1]["event"]["type"] == "run.completed":
                assert all(e["sequence"] > cutoff for e in replayed)
                return
        time.sleep(0.1)
    raise AssertionError("重连后没有从 Event Store 补齐到终态")


def test_the_fake_gateway_runs_on_loopback_only(harness):
    """护栏：测试用的假网关不得暴露到回环之外。"""
    assert harness.server.base_url.startswith("http://127.0.0.1:")
    assert threading.active_count() >= 1
