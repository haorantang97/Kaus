"""批次十五后端：能力报告缓存（AD-127）、索引行 surface、以及三个小补。

真机现象
--------
Hermes 网关暂时离线时 ``GET /api/backends/backend:hermes`` 的 ``capabilities.ui``
全部退成 ``unknown``——上一次成功探测的结论被这次失败抹掉了，前端于是把整张
能力清单和工具卡都判成「缺能力」。探测失败是「引擎离线」这一个事实，不该顺手
删掉已经验证过的能力声明。

覆盖
----
1. AD-127：探测失败后能力原样保留、``verification`` 降为 ``cached`` 且带
   ``capturedAt``；失败原因进 ``probeMessage``；重启（重开库）后仍在；
   从未成功探测过的 Backend 才是全 ``unknown``；下一次成功探测把缓存换掉。
2. 会话索引两个端点的每行带 ``surface``（``card`` / ``external-cli``），
   由 lease 与 ``preferred_surface`` 得出，过期 lease 不算数。
3. ``GET /conversations/{id}/surface`` 在没装 Launcher 的装配下回 200 +
   ``supported:false``；装了则 ``supported:true``。
4. ``POST /surface/card`` 的响应带**完整** ``entries``，与
   ``kaus/history.reconciled`` 事件的 data 同形。
5. ``kaus/lease.taken_over`` 带 ``previousOwnerLabel``（人话），原字段保留。

隔离：SQLite 一律建在 ``tmp_path``；不碰真实 ``~/.hermes``、不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.router import build_domain_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.api.views import backend_to_wire  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import (  # noqa: E402
    AgentBinding,
    Backend,
    Project,
)
from drivers.base import BackendProbeResult  # noqa: E402
from drivers.mock.capabilities import DEFAULT_CAPABILITIES  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry, apply_probe_result  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.external_cli import TerminalLauncher  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402
from runtime.surface_handoff import (  # noqa: E402
    KAUS_NAMESPACE,
    LEASE_TAKEN_OVER_NAME,
    SurfaceCoordinator,
)

TEST_TOKEN = "test-token-0123456789"
PROBED_AT = datetime(2026, 9, 3, 8, 30, tzinfo=timezone.utc)
BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _ready(**changes) -> BackendProbeResult:
    """一次成功探测（能力齐全）。"""
    payload = {
        "backend_id": "backend:mock",
        "driver_kind": "mock",
        "state": "ready",
        "installed": True,
        "capabilities": DEFAULT_CAPABILITIES,
        "probed_at": PROBED_AT,
    }
    payload.update(changes)
    return BackendProbeResult(**payload)


def _offline(message: str = "连接被拒：网关没在运行") -> BackendProbeResult:
    """一次失败探测：网关离线，能力一个都问不出来。"""
    return BackendProbeResult(
        backend_id="backend:mock",
        driver_kind="mock",
        state="unavailable",
        installed=False,
        message=message,
        probed_at=PROBED_AT + timedelta(hours=1),
    )


# --------------------------------------------------------------------------- #
# 1. AD-127 能力报告缓存
# --------------------------------------------------------------------------- #


def test_ad127_probe_failure_keeps_the_last_successful_capabilities() -> None:
    """探测失败只改状态，不动能力：wire 上是 ``cached`` + ``capturedAt``。"""
    backend = apply_probe_result(
        Backend.create(key="mock", driver_kind="mock"), _ready()
    )
    assert backend.capabilities.card.streaming.is_supported

    offline = apply_probe_result(backend, _offline())

    # 领域侧：能力与快照一个字没动，变的只有状态与原因。
    assert offline.probe_state == "unavailable"
    assert offline.installed is False
    assert offline.probe_message == "连接被拒：网关没在运行"
    assert offline.capabilities == backend.capabilities
    assert offline.capability_snapshot is not None
    assert offline.capability_snapshot.captured_at == PROBED_AT

    # wire 侧：值照旧，取证等级降为 cached，并说清是什么时候采的。
    wire = backend_to_wire(offline)
    assert wire["probeState"] == "unavailable"
    assert wire["probeMessage"] == "连接被拒：网关没在运行"
    assert wire["capabilities"]["ui"]["card"]["streaming"] == "supported"
    streaming = wire["capabilities"]["detail"]["card"]["streaming"]
    assert streaming["verification"] == "cached"
    assert streaming["capturedAt"] == "2026-09-03T08:30:00Z"
    assert wire["capabilities"]["cachedAt"] == "2026-09-03T08:30:00Z"
    # 缓存不是第二真源：快照本体不上 wire。
    assert "capabilitySnapshot" not in wire


def test_ad127_a_backend_that_never_probed_successfully_is_all_unknown() -> None:
    """从未成功探测过 → 没有缓存可回放，全 ``unknown``，也没有 ``cachedAt``。"""
    backend = apply_probe_result(
        Backend.create(key="mock", driver_kind="mock"), _offline()
    )

    assert backend.capability_snapshot is None
    wire = backend_to_wire(backend)
    assert wire["capabilities"]["ui"]["card"]["streaming"] == "unknown"
    assert wire["capabilities"]["detail"]["card"]["streaming"]["verification"] == (
        "declared"
    )
    assert "cachedAt" not in wire["capabilities"]
    assert wire["unknownCount"] > 0


def test_ad127_a_later_successful_probe_replaces_the_cache() -> None:
    """引擎回来了：缓存换成这一次的，``capturedAt`` 跟着走，``cached`` 消失。"""
    backend = apply_probe_result(
        Backend.create(key="mock", driver_kind="mock"), _ready()
    )
    backend = apply_probe_result(backend, _offline())

    back_online = PROBED_AT + timedelta(hours=2)
    backend = apply_probe_result(backend, _ready(probed_at=back_online))

    assert backend.probe_state == "available"
    assert backend.capability_snapshot is not None
    assert backend.capability_snapshot.captured_at == back_online
    detail = backend_to_wire(backend)["capabilities"]["detail"]
    assert detail["card"]["streaming"]["verification"] != "cached"
    assert "capturedAt" not in detail["card"]["streaming"]


def test_ad127_cache_survives_a_restart(tmp_path: Path) -> None:
    """缓存落库：进程重启（重开一条连接）之后照样是 ``cached``，不是 unknown。"""
    db_path = tmp_path / "domain.sqlite3"
    backend = apply_probe_result(
        Backend.create(key="mock", driver_kind="mock"), _ready()
    )
    backend = apply_probe_result(backend, _offline())

    first = SqliteUnitOfWork(db_path)
    try:
        asyncio.run(first.repositories.backends.save(backend))
    finally:
        first.close()

    second = SqliteUnitOfWork(db_path)
    try:
        loaded = asyncio.run(second.repositories.backends.get(backend.id))
    finally:
        second.close()

    assert loaded is not None
    assert loaded.probe_state == "unavailable"
    assert loaded.probe_message == "连接被拒：网关没在运行"
    assert loaded.capabilities == backend.capabilities
    assert loaded.capability_snapshot is not None
    assert loaded.capability_snapshot.captured_at == PROBED_AT
    detail = backend_to_wire(loaded)["capabilities"]["detail"]
    assert detail["card"]["streaming"]["verification"] == "cached"
    assert detail["card"]["streaming"]["capturedAt"] == "2026-09-03T08:30:00Z"


def test_ad127_get_backend_endpoint_serves_the_cache(tmp_path: Path) -> None:
    """端到端：``GET /api/backends/{id}`` 在引擎离线时仍给得出能力清单。"""
    unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    try:
        backend = apply_probe_result(
            Backend.create(key="mock", driver_kind="mock"), _ready()
        )
        asyncio.run(
            unit_of_work.repositories.backends.save(apply_probe_result(backend, _offline()))
        )
        app = fastapi.FastAPI()
        app.include_router(build_domain_router(unit_of_work.repositories))
        with TestClient(app) as client:
            body = client.get("/api/backends/backend:mock").json()

        assert body["probeState"] == "unavailable"
        assert body["probeMessage"] == "连接被拒：网关没在运行"
        assert body["capabilities"]["ui"]["card"]["streaming"] == "supported"
        assert body["capabilities"]["cachedAt"] == "2026-09-03T08:30:00Z"
    finally:
        unit_of_work.close()


# --------------------------------------------------------------------------- #
# 2..5 接入层：会话索引 surface / GET surface / surface/card / taken_over
# --------------------------------------------------------------------------- #


class Harness:
    """一个项目、两条会话；``with_launcher`` 决定装不装交接编排。"""

    def __init__(self, tmp_path: Path, *, with_launcher: bool) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(
                self.repositories.leases, heartbeat_timeout=timedelta(seconds=90)
            ),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.opened: list[Path] = []
        self.coordinator = None
        if with_launcher:
            self.coordinator = SurfaceCoordinator(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                launcher=TerminalLauncher(
                    root=tmp_path / "launches",
                    app="fake-terminal",
                    opener=self._open,
                    pid_timeout=5.0,
                ),
            )
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                run_id_timeout=1.0,
                surface_coordinator=self.coordinator,
            )
        )

    def _open(self, app: str, script_path: Path) -> tuple[bool, str | None]:
        """假启动器：只记一笔，不开窗、不跑脚本（pid 自己补上）。"""
        self.opened.append(script_path)
        (script_path.parent / "pid").write_text("1\n", encoding="utf-8")
        return True, None

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", driver_kind="mock"))
        project = await repos.projects.save(Project.create(slug="alpha"))
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(project=project, backend="mock", is_default=True)
        )
        self.staying = await repos.conversations.save(
            Conversation.create(
                project_id=project.id,
                agent_binding_id=self.binding.id,
                title="留在站内",
                created_at=BASE,
            )
        )
        self.leaving = await repos.conversations.save(
            Conversation.create(
                project_id=project.id,
                agent_binding_id=self.binding.id,
                title="去了终端",
                created_at=BASE + timedelta(minutes=10),
            )
        )

    async def aclose(self) -> None:
        await self.host.aclose()
        self.unit_of_work.close()


def _client(harness: Harness) -> TestClient:
    return TestClient(harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"})


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path, with_launcher=True)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        asyncio.run(built.aclose())


def test_index_rows_carry_the_surface_they_are_written_on(harness) -> None:
    """索引行的 ``surface``：外部 lease 那条是 ``external-cli``，其余是 ``card``。"""
    asyncio.run(
        harness.host.leases.acquire_external_cli(
            harness.leaving.id, owner_id="launch:1"
        )
    )
    with _client(harness) as client:
        rows = client.get("/api/conversations").json()["conversations"]
        by_id = {row["id"]: row for row in rows}
        assert by_id[harness.leaving.id]["surface"] == "external-cli"
        assert by_id[harness.staying.id]["surface"] == "card"
        # status 与 surface 是两根轴：没在跑的会话照样可以在外部终端上。
        assert by_id[harness.leaving.id]["status"] == "idle"

        project_rows = client.get(
            f"/api/projects/{harness.project_id}/conversations"
        ).json()["conversations"]
        assert {row["id"]: row["surface"] for row in project_rows} == {
            harness.leaving.id: "external-cli",
            harness.staying.id: "card",
        }


def test_index_surface_falls_back_to_preferred_surface_when_no_one_holds_the_lease(
    harness,
) -> None:
    """没人持有写权时按 ``preferred_surface``：上次在终端就还是终端。"""
    asyncio.run(
        harness.repositories.conversations.save(
            harness.leaving.evolve(preferred_surface="external-cli")
        )
    )
    with _client(harness) as client:
        rows = client.get("/api/conversations").json()["conversations"]

    by_id = {row["id"]: row["surface"] for row in rows}
    assert by_id[harness.leaving.id] == "external-cli"
    assert by_id[harness.staying.id] == "card"


def test_index_surface_ignores_a_stale_lease(tmp_path) -> None:
    """过期的 lease 不算数：持有者早就不在了，拿它当事实就是说谎。"""
    built = Harness(tmp_path, with_launcher=True)
    # ttl 设成 0，写完立刻就是 stale。
    built.host.leases.heartbeat_timeout = timedelta(seconds=0)
    asyncio.run(built.seed())
    try:
        asyncio.run(
            built.host.leases.acquire_external_cli(
                built.leaving.id, owner_id="launch:1"
            )
        )
        with _client(built) as client:
            rows = client.get("/api/conversations").json()["conversations"]
        assert {row["id"]: row["surface"] for row in rows} == {
            built.leaving.id: "card",
            built.staying.id: "card",
        }
    finally:
        asyncio.run(built.aclose())


def test_get_surface_without_a_launcher_is_200_and_says_unsupported(tmp_path) -> None:
    """没装 Launcher：200 + ``supported:false``，不是 501/404。"""
    built = Harness(tmp_path, with_launcher=False)
    asyncio.run(built.seed())
    try:
        with _client(built) as client:
            response = client.get(f"/api/conversations/{built.staying.id}/surface")
            assert response.status_code == 200, response.text
            assert response.json() == {
                "conversationId": built.staying.id,
                "surface": "card",
                "supported": False,
                "lease": None,
                "launch": None,
            }
            # 会开终端的那两条路仍然不该存在。
            assert (
                client.post(
                    f"/api/conversations/{built.staying.id}/surface/external"
                ).status_code
                == 404
            )
    finally:
        asyncio.run(built.aclose())


def test_get_surface_with_a_launcher_says_supported(harness) -> None:
    """装了 Launcher：同一个形状，``supported`` 为真。"""
    with _client(harness) as client:
        body = client.get(f"/api/conversations/{harness.staying.id}/surface").json()

    assert body["supported"] is True
    assert body["surface"] == "card"
    assert body["lease"] is None and body["launch"] is None


def test_surface_card_response_carries_the_full_entries(harness) -> None:
    """``POST /surface/card`` 的响应与 ``history.reconciled`` 的 data 同形。"""
    conversation_id = harness.leaving.id
    with _client(harness) as client:
        client.post(f"/api/conversations/{conversation_id}/surface/external")
        body = client.post(
            f"/api/conversations/{conversation_id}/surface/card?force=1"
        ).json()

    events = asyncio.run(harness.host.event_store.replay(conversation_id))
    reconciled = [
        envelope.event
        for envelope in events
        if envelope.event.type == "extension.event"
        and envelope.event.namespace == KAUS_NAMESPACE
        and envelope.event.name == "history.reconciled"
    ]
    assert len(reconciled) == 1
    # SSE 断了也拿得到同一份校准结果：两条路送的是同一批条目。
    assert body["entries"] == reconciled[0].data["entries"]
    assert body["entryCount"] == len(body["entries"])
    assert body["lastEntryId"] == reconciled[0].data["lastEntryId"]


def test_lease_taken_over_carries_a_human_readable_previous_owner(harness) -> None:
    """强制接管：``previousOwnerLabel`` 是人话，机器字段原样保留。"""
    conversation_id = harness.staying.id
    asyncio.run(
        harness.host.leases.acquire_card(conversation_id, owner_id="runtime:1")
    )
    with _client(harness) as client:
        response = client.post(
            f"/api/conversations/{conversation_id}/surface/external?force=1"
        )
        assert response.status_code == 200, response.text

    events = asyncio.run(harness.host.event_store.replay(conversation_id))
    taken_over = [
        envelope.event
        for envelope in events
        if envelope.event.type == "extension.event"
        and envelope.event.namespace == KAUS_NAMESPACE
        and envelope.event.name == LEASE_TAKEN_OVER_NAME
    ]
    assert len(taken_over) == 1
    data = taken_over[0].data
    assert data["previousOwner"] == "card"
    assert data["previousOwnerLabel"] == "站内卡片"
    assert data["previousOwnerId"] == "runtime:1"
