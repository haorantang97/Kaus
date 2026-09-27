"""Phase 4：Card ⇄ External CLI 双表面交接的接入层测试。

覆盖
----
1. 四个端点的正常路径：``POST /surface/external`` / ``POST /surface/card`` /
   ``GET /surface`` / ``GET /launches``；
2. 409 / 501 全表：``card_running`` / ``lease_held``（+ ``?force=1`` 接管）/
   ``external_active`` / ``surface_external_active``（发消息）/
   ``external_cli_unsupported`` / ``native_session_precreate_unsupported``；
3. 校准事件的形状：``kaus/history.reconciled`` 折成**一条**，``complete=false``
   时补一条 ``diagnostic.notice``；
4. Mock Driver 端到端：开终端（假启动器用 ``sh`` 真跑脚本）→ 监视器发现退出 →
   自动回 Card → 订阅端收到两次 ``surface.changed`` 与一次 ``history.reconciled``。

隔离：SQLite 与启动脚本都建在 ``tmp_path``；``open`` 一律被假启动器替掉，
既不碰 macOS 的终端，也不读任何凭据。
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.capabilities import DEFAULT_CAPABILITIES  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.mock.fixtures import EmitStep, HoldStep, MockScript  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.capability_matrix import (  # noqa: E402
    CapabilityState,
    ExternalCliCapabilities,
    declare,
)
from runtime.event_envelope import RunStarted  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.external_cli import (  # noqa: E402
    EXIT_FILENAME,
    ExternalCliMonitor,
    TerminalLauncher,
    launch_dir,
)
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402
from runtime.surface_handoff import (  # noqa: E402
    HISTORY_RECONCILED_NAME,
    INCOMPLETE_NOTICE,
    KAUS_NAMESPACE,
    SURFACE_CHANGED_NAME,
    SurfaceCoordinator,
)

PROJECT_SLUG = "workbench"
TEST_TOKEN = "test-token-0123456789"

#: 不支持站外 CLI 的引擎（501 external_cli_unsupported）。
NO_CLI_CAPABILITIES = DEFAULT_CAPABILITIES.model_copy(
    update={
        "external_cli": ExternalCliCapabilities(
            supported=CapabilityState.model_validate(declare("unsupported")),
            resume=CapabilityState.model_validate(declare("unsupported")),
        )
    }
)

#: 不能预创建原生 Session 的引擎（501 native_session_precreate_unsupported）。
NO_PRECREATE_CAPABILITIES = DEFAULT_CAPABILITIES.model_copy(
    update={
        "sessions": DEFAULT_CAPABILITIES.sessions.model_copy(
            update={"create": CapabilityState.model_validate(declare("unsupported"))}
        )
    }
)


class Harness:
    """库 + Registry + Session Host + Launcher + Coordinator + TestClient。"""

    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.no_cli_driver = MockDriver(
            backend_key="nocli", capabilities=NO_CLI_CAPABILITIES
        )
        self.no_precreate_driver = MockDriver(
            backend_key="noprecreate", capabilities=NO_PRECREATE_CAPABILITIES
        )
        self.registry = BackendDriverRegistry(
            [self.driver, self.no_cli_driver, self.no_precreate_driver]
        )
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
        #: 假启动器：默认只记一笔就说「开好了」，不真的开窗、不真的跑。
        self.opened: list[Path] = []
        self.processes: list[subprocess.Popen] = []
        self.run_script = False
        self.launcher = TerminalLauncher(
            root=tmp_path / "launches",
            app="fake-terminal",
            opener=self._open,
            # 不真跑脚本时 pid 永远不会出现，别让每个用例等 3 秒。
            pid_timeout=5.0,
        )
        self.coordinator = SurfaceCoordinator(
            session_host=self.host,
            repositories=self.repositories,
            registry=self.registry,
            launcher=self.launcher,
        )
        self.monitor = ExternalCliMonitor(
            leases=self.host.leases,
            terminal_launches=self.repositories.terminal_launches,
            root=self.launcher.root,
            on_exit=self.coordinator.on_external_exit,
        )
        self.auth_policy = SessionAuthPolicy(lambda: TEST_TOKEN)
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=self.auth_policy,
                run_id_timeout=1.0,
                surface_coordinator=self.coordinator,
            )
        )

    def _open(self, app: str, script_path: Path) -> tuple[bool, str | None]:
        self.opened.append(script_path)
        if self.run_script:
            self.processes.append(subprocess.Popen(["sh", str(script_path)]))
        else:
            # 没真跑就自己把 pid 补上：真实的 `open` 之后 pid 由脚本写，
            # 这里替它写一行，免得每个用例都为一个不存在的进程等满超时。
            (script_path.parent / "pid").write_text("1\n", encoding="utf-8")
        return True, None

    async def seed(self) -> None:
        repos = self.repositories
        for key, kind in (("mock", "mock"), ("nocli", "mock"), ("noprecreate", "mock")):
            await repos.backends.save(Backend.create(key=key, driver_kind=kind))
        project = await repos.projects.save(
            Project.create(slug=PROJECT_SLUG, display_name="工作台")
        )
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="mock", display_name="Mock 绑定", is_default=True
            )
        )
        self.no_cli_binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="nocli", display_name="不支持站外 CLI"
            )
        )
        self.no_precreate_binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="noprecreate", display_name="不能预创建会话"
            )
        )

    async def aclose(self) -> None:
        for process in self.processes:
            if process.poll() is None:  # pragma: no cover - 正常用例里都已退出
                process.kill()
        await self.host.aclose()
        self.unit_of_work.close()

    # --- 便捷 ----------------------------------------------------------- #

    def new_conversation(self, client: TestClient, *, binding_id: str | None = None) -> str:
        response = client.post(
            f"/api/projects/{self.project_id}/conversations",
            json={"bindingId": binding_id or self.binding.id},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def events_of(self, conversation_id: str) -> tuple:
        return await self.host.event_store.replay(conversation_id)

    async def extension_events(self, conversation_id: str, name: str) -> list:
        return [
            envelope.event
            for envelope in await self.events_of(conversation_id)
            if envelope.event.type == "extension.event"
            and envelope.event.namespace == KAUS_NAMESPACE
            and envelope.event.name == name
        ]


def _client(harness: Harness) -> TestClient:
    client = TestClient(harness.app)
    client.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
    return client


def _harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


async def test_open_external_switches_surface_and_records_launch(tmp_path: Path) -> None:
    """开终端：预创建原生会话 → 落 ``terminal_launches`` → 状态与 lease 都换手。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["surface"] == "external-cli"
            assert body["launched"] is True
            assert "sleep 1" in body["commandSummary"]
            assert body["launch"]["launcher"] == "fake-terminal"
            assert body["launch"]["envPassthrough"] == ["PATH", "HOME"]
            assert body["lease"]["owner"] == "external-cli"

            # v1.0 §8.7：命令里带的是**预创建**出来的确定 id，不是猜出来的。
            conversation = await harness.repositories.conversations.get(conversation_id)
            assert conversation is not None
            assert conversation.native_session_id
            assert conversation.native_session_id in body["commandSummary"]
            assert conversation.state == "running-external"
            assert conversation.preferred_surface == "external-cli"

            surface = client.get(f"/api/conversations/{conversation_id}/surface").json()
            assert surface["surface"] == "external-cli"
            assert surface["launch"]["id"] == body["launch"]["id"]
            assert surface["lease"]["stale"] is False
            assert surface["lease"]["expiresAt"] > surface["lease"]["heartbeatAt"]

            launches = client.get(f"/api/conversations/{conversation_id}/launches").json()
            assert launches["count"] == 1
            assert launches["launches"][0]["id"] == body["launch"]["id"]

            changed = await harness.extension_events(conversation_id, SURFACE_CHANGED_NAME)
            assert [event.data["surface"] for event in changed] == ["external-cli"]
            assert changed[0].data["launchId"] == body["launch"]["id"]
    finally:
        await harness.aclose()


async def test_return_to_card_reconciles_history_into_one_event(tmp_path: Path) -> None:
    """回站：新增的原生条目折成**一条** ``history.reconciled``，lease 归还。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            client.post(f"/api/conversations/{conversation_id}/surface/external")
            response = client.post(
                f"/api/conversations/{conversation_id}/surface/card?force=1"
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["surface"] == "card"
            assert body["reconciled"] is True
            assert body["complete"] is True
            # 批次十五第 4 件：响应带完整 entries（与事件同形）+ 条数。
            assert body["entryCount"] == 2
            assert [entry["role"] for entry in body["entries"]] == [
                "user",
                "assistant",
            ]

            reconciled = await harness.extension_events(
                conversation_id, HISTORY_RECONCILED_NAME
            )
            assert len(reconciled) == 1
            data = reconciled[0].data
            assert [entry["role"] for entry in data["entries"]] == ["user", "assistant"]
            assert data["complete"] is True and data["gaps"] == []
            assert data["lastEntryId"] == data["entries"][-1]["entryId"]

            # 核心事件一条都没被伪造出来（信封 v1.1 冻结）。
            types = {
                envelope.event.type for envelope in await harness.events_of(conversation_id)
            }
            assert types <= {"extension.event", "diagnostic.notice"}

            conversation = await harness.repositories.conversations.get(conversation_id)
            assert conversation is not None and conversation.state == "idle"
            assert conversation.preferred_surface == "card"
            assert await harness.host.leases.describe(conversation_id) is None

            changed = await harness.extension_events(conversation_id, SURFACE_CHANGED_NAME)
            assert [event.data["surface"] for event in changed] == ["external-cli", "card"]
    finally:
        await harness.aclose()


async def test_incomplete_history_adds_diagnostic(tmp_path: Path) -> None:
    """``complete=false`` → 再发一条 warn：不承诺的东西就说不承诺。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            client.post(f"/api/conversations/{conversation_id}/surface/external")

            original = harness.driver.load_native_history

            async def partial(binding, native_session_id):
                history = await original(binding, native_session_id)
                return history.model_copy(
                    update={"complete": False, "missing": ("工具结果", "审批决策")}
                )

            harness.driver.load_native_history = partial  # type: ignore[method-assign]
            response = client.post(
                f"/api/conversations/{conversation_id}/surface/card?force=1"
            )
            assert response.status_code == 200
            assert response.json()["complete"] is False
            assert response.json()["gaps"] == ["工具结果", "审批决策"]

            notices = [
                envelope.event
                for envelope in await harness.events_of(conversation_id)
                if envelope.event.type == "diagnostic.notice"
            ]
            assert [notice.message for notice in notices] == [INCOMPLETE_NOTICE]
            assert notices[0].level == "warn"
    finally:
        await harness.aclose()


# --------------------------------------------------------------------------- #
# 409 / 501
# --------------------------------------------------------------------------- #


async def test_card_running_blocks_external(tmp_path: Path) -> None:
    """站内这一轮还在跑 → 409 ``card_running``（前端提示先停止）。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            harness.driver.set_script(
                conversation_id,
                MockScript(
                    name="hold",
                    steps=(EmitStep(event=RunStarted(run_id="run-hold")), HoldStep()),
                ),
            )
            accepted = client.post(
                f"/api/conversations/{conversation_id}/messages", json={"text": "跑起来"}
            )
            assert accepted.status_code == 202, accepted.text
            for _ in range(50):
                timeline = harness.host.timeline(conversation_id)
                if timeline is not None and timeline.run_state == "running":
                    break
                await asyncio.sleep(0.02)
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "card_running"
            # 拒绝之后 Card 还在跑：没有被顺手停掉。
            assert harness.host.is_active(conversation_id)
    finally:
        await harness.aclose()


async def test_lease_held_needs_force(tmp_path: Path) -> None:
    """已有活着的外部 lease → 409 ``lease_held``；``?force=1`` 才接管并广播。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            first = client.post(f"/api/conversations/{conversation_id}/surface/external")
            first_launch = first.json()["launch"]["id"]

            blocked = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert blocked.status_code == 409
            error = blocked.json()["error"]
            assert error["code"] == "lease_held"
            assert error["detail"]["owner"] == "external-cli"
            assert error["detail"]["launchId"] == first_launch
            assert error["detail"]["acquiredAt"]

            forced = client.post(
                f"/api/conversations/{conversation_id}/surface/external?force=1"
            )
            assert forced.status_code == 200, forced.text
            assert forced.json()["launch"]["id"] != first_launch

            taken = await harness.extension_events(conversation_id, "lease.taken_over")
            assert len(taken) == 1
            assert taken[0].data["previousOwnerId"] == first_launch
            assert taken[0].data["stale"] is False
            assert client.get(
                f"/api/conversations/{conversation_id}/launches"
            ).json()["count"] == 2
    finally:
        await harness.aclose()


async def test_external_active_blocks_return_to_card(tmp_path: Path) -> None:
    """外部还活着 → 回站要 ``?force=1``（409 ``external_active``）。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            client.post(f"/api/conversations/{conversation_id}/surface/external")
            blocked = client.post(f"/api/conversations/{conversation_id}/surface/card")
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "external_active"
            assert blocked.json()["error"]["detail"]["owner"] == "external-cli"
            # 拒绝没有顺手改状态。
            conversation = await harness.repositories.conversations.get(conversation_id)
            assert conversation is not None and conversation.state == "running-external"
    finally:
        await harness.aclose()


async def test_send_message_conflicts_while_external_active(tmp_path: Path) -> None:
    """AD-122：外部持有写权时 ``POST /messages`` → 409 ``surface_external_active``。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            launch_id = client.post(
                f"/api/conversations/{conversation_id}/surface/external"
            ).json()["launch"]["id"]
            response = client.post(
                f"/api/conversations/{conversation_id}/messages", json={"text": "喂"}
            )
            assert response.status_code == 409, response.text
            error = response.json()["error"]
            assert error["code"] == "surface_external_active"
            assert error["detail"]["owner"] == "external-cli"
            assert error["detail"]["launchId"] == launch_id
            assert not harness.host.is_active(conversation_id)
    finally:
        await harness.aclose()


async def test_external_cli_unsupported_driver(tmp_path: Path) -> None:
    """引擎不支持站外 CLI → 501 ``external_cli_unsupported``。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(
                client, binding_id=harness.no_cli_binding.id
            )
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 501, response.text
            assert response.json()["error"]["code"] == "external_cli_unsupported"
            assert harness.opened == []
    finally:
        await harness.aclose()


async def test_precreate_unsupported_driver(tmp_path: Path) -> None:
    """不能预创建原生 Session → 501，并明说不支持从站内开终端续接（禁止猜绑定）。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(
                client, binding_id=harness.no_precreate_binding.id
            )
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 501, response.text
            error = response.json()["error"]
            assert error["code"] == "native_session_precreate_unsupported"
            assert "续接" in error["message"]
            assert harness.opened == []
    finally:
        await harness.aclose()


async def test_surface_endpoints_reject_unknown_conversation(tmp_path: Path) -> None:
    """未知会话 → 404，四个端点同一形状。"""
    harness = _harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            missing = "conversation:99999999-9999-4999-8999-999999999999"
            for method, path in (
                (client.post, f"/api/conversations/{missing}/surface/external"),
                (client.post, f"/api/conversations/{missing}/surface/card"),
                (client.get, f"/api/conversations/{missing}/surface"),
                (client.get, f"/api/conversations/{missing}/launches"),
            ):
                response = method(path)
                assert response.status_code == 404, path
                assert response.json()["error"]["code"] == "conversation_not_found"
    finally:
        await harness.aclose()


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #


async def test_mock_driver_end_to_end_handoff(tmp_path: Path) -> None:
    """开终端 → 假启动器真跑脚本 → 进程退出 → 监视器自动回 Card。

    订阅端（SSE 走的是同一条订阅）应当收到两次 ``surface.changed``
    与一次 ``history.reconciled``。
    """
    harness = _harness(tmp_path)
    harness.run_script = True
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            subscription = harness.host.subscribe(conversation_id)
            opened = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert opened.status_code == 200, opened.text
            launch_id = opened.json()["launch"]["id"]
            assert opened.json()["launch"]["externalProcessRef"]

            # 脚本是 mock 给的 `sh -c 'sleep 1; echo done'`，等它自己退出。
            harness.processes[0].wait(timeout=30)
            exit_file = launch_dir(harness.launcher.root, launch_id) / EXIT_FILENAME
            for _ in range(100):
                if exit_file.exists():
                    break
                await asyncio.sleep(0.05)

            signals = await harness.monitor.poll_once()
            assert len(signals) == 1
            assert signals[0].source == "exit-file" and signals[0].exit_code == 0

            # 用户一下都没点，会话自己回到了 Card。
            conversation = await harness.repositories.conversations.get(conversation_id)
            assert conversation is not None and conversation.state == "idle"
            assert conversation.preferred_surface == "card"
            assert await harness.host.leases.describe(conversation_id) is None

            surface = client.get(f"/api/conversations/{conversation_id}/surface").json()
            assert surface["surface"] == "card"
            assert surface["launch"]["exitStatus"] == 0
            assert surface["launch"]["status"] == "exited"

            names = []
            for _ in range(3):
                envelope = await asyncio.wait_for(subscription.__anext__(), timeout=5)
                names.append(envelope.event.name)
            subscription.close()
            assert names.count(SURFACE_CHANGED_NAME) == 2
            assert names.count(HISTORY_RECONCILED_NAME) == 1

            # 站内立刻可以继续发消息（写权已经回来了）。
            resumed = client.post(
                f"/api/conversations/{conversation_id}/messages", json={"text": "继续"}
            )
            assert resumed.status_code == 202, resumed.text
    finally:
        await harness.aclose()


async def test_launch_failure_retains_card_surface_and_releases_reservation(tmp_path):
    harness = Harness(tmp_path)
    await harness.seed()
    harness.launcher._opener = lambda app, script: (False, "Terminal unavailable")
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 200, response.text
            assert response.json()["launched"] is False
            assert response.json()["surface"] == "card"
            assert response.json()["lease"] is None
            assert client.get(f"/api/conversations/{conversation_id}/surface").json()["surface"] == "card"
    finally:
        await harness.aclose()


async def test_concurrent_terminal_clicks_start_only_one_native_writer(tmp_path):
    from runtime.surface_handoff import SurfaceConflictError
    harness = Harness(tmp_path)
    await harness.seed()
    observed_owners = []
    launch = harness.launcher.launch
    async def observed_launch(spec, **kwargs):
        owner = await harness.host.leases.describe(kwargs["conversation_id"])
        observed_owners.append(owner)
        await asyncio.sleep(0.02)
        return await launch(spec, **kwargs)
    harness.launcher.launch = observed_launch
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
        conversation = await harness.repositories.conversations.get(conversation_id)
        results = await asyncio.gather(harness.coordinator.to_external(conversation),
                                       harness.coordinator.to_external(conversation), return_exceptions=True)
        assert len(harness.opened) == 1
        assert len(observed_owners) == 1 and observed_owners[0].owner_type == "external-cli"
        assert sum(isinstance(value, SurfaceConflictError) for value in results) == 1
        completed = next(value for value in results if not isinstance(value, Exception))
        assert observed_owners[0].owner_id == completed.launch.id
    finally:
        await harness.aclose()


async def test_archived_conversation_cannot_open_external_writer(tmp_path):
    harness = Harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
            conversation = await harness.repositories.conversations.get(conversation_id)
            await harness.repositories.conversations.save(conversation.evolve(archived_at=conversation.created_at))
            response = client.post(f"/api/conversations/{conversation_id}/surface/external")
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "conversation_archived"
            assert harness.opened == []
    finally:
        await harness.aclose()


async def test_pending_dispatch_cannot_be_stopped_by_terminal_handoff(tmp_path):
    from runtime.surface_handoff import SurfaceConflictError
    harness = Harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
        conversation = await harness.repositories.conversations.get(conversation_id)
        await harness.host.start_runtime(conversation)
        harness.host._runtimes[conversation_id].pending_dispatch = True
        with pytest.raises(SurfaceConflictError) as error:
            await harness.coordinator.to_external(conversation)
        assert error.value.code == "card_running"
        assert harness.host.is_active(conversation_id)
        assert harness.opened == []
    finally:
        await harness.aclose()


async def test_exit_from_old_terminal_cannot_reclaim_new_lease(tmp_path):
    from runtime.external_cli import ExitSignal
    harness = Harness(tmp_path)
    await harness.seed()
    try:
        with _client(harness) as client:
            conversation_id = harness.new_conversation(client)
        conversation = await harness.repositories.conversations.get(conversation_id)
        first = await harness.coordinator.to_external(conversation)
        second = await harness.coordinator.to_external(conversation, force=True)
        await harness.coordinator.on_external_exit(ExitSignal(conversation_id=conversation_id,
            launch=first.launch, exit_code=0, source="exit-file"))
        owner = await harness.host.leases.describe(conversation_id)
        assert owner is not None and owner.owner_id == second.launch.id
        assert (await harness.coordinator.describe(second.conversation))["surface"] == "external-cli"
    finally:
        await harness.aclose()
