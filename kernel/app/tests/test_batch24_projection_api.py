"""批次二十四第 3 件：``POST /bindings/{id}/materialize`` 与 ``GET /bindings/{id}/drift``。

覆盖
----
1. 默认 dry-run：不带 ``confirm=1`` 时**一个字节都不落盘**，也不留账；
2. ``confirm=1`` 落盘、带 ``backupPath``、并在 ``projection_results`` 留一行；
3. 该 Binding 有活跃会话时真写返回 409 ``binding_busy``——**dry-run 不受限**；
4. ``GET /drift`` 的四态（``in_sync`` / ``drifted`` / ``unmanaged`` / ``missing``）；
5. 400 码：Driver 没注册 / Driver 没有投射面；404：未知 Binding；
6. 记账里**只有键路径与计数，没有值**（§5.4）。

隔离：SQLite 在 ``tmp_path``，Mock Driver 的 home 也在 ``tmp_path``。本文件写的
唯一一个配置文件是 ``<tmp_path>/mock-home/mock-config.json``，与任何真实引擎
家目录无关。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.binding_router import _projection_summary, build_binding_write_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.capabilities.models import ProjectCapability  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import ProjectionEntry, ProjectionResult  # noqa: E402
from drivers.mock import projector as mock_projector  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
MCP_VALUE = {"demo": {"command": "echo"}}


class _FakeHost:
    """只回答「这条会话在不在跑」——端点要的就这一件事。

    批次二十七热修之后端点问的是两件事而不是一件：``is_active``（有没有活跃
    Runtime，删 Binding 用它）与 ``run_state_of``（这一轮跑没跑完，物化用它）。
    ``active`` 里的会话默认两者都真；把 id 放进 ``idle_runtimes`` 就是「runtime
    还活着但已经跑完了」——真机上物化被误挡的正是这一档。
    """

    def __init__(self) -> None:
        self.active: set[str] = set()
        self.idle_runtimes: set[str] = set()

    def is_active(self, conversation_id: str) -> bool:
        return conversation_id in self.active

    async def reconcile_conversation(self, conversation: Any) -> bool:
        return False

    async def run_state_of(self, conversation: Any) -> str:
        if conversation.id in self.idle_runtimes:
            return "idle"
        return "running" if conversation.id in self.active else "idle"


class Harness:
    def __init__(self, tmp_path: Path, *, with_driver: bool = True) -> None:
        self.home = tmp_path / "mock-home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver(home=self.home)
        self.host = _FakeHost()
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_binding_write_router(
                self.repositories,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                session_host=self.host,
                registry=BackendDriverRegistry([self.driver]) if with_driver else None,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        backend = await repos.backends.save(
            Backend.create(key="mock", driver_kind="mock", probe_state="available")
        )
        project = await repos.projects.save(Project.create(slug="workbench"))
        self.project = project
        self.binding = await repos.bindings.save(
            AgentBinding.create(project=project, backend=backend, is_default=True)
        )
        await repos.capabilities.save(
            ProjectCapability.create(
                project_id=project.id,
                capability_type="mcp",
                capability_id="demo",
                config={"value": MCP_VALUE},
            )
        )

    # --- 磁盘 --------------------------------------------------------------- #

    def config_path(self) -> Path:
        return mock_projector.config_path_of(self.home)

    def config(self) -> dict:
        path = self.config_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def key_path(self) -> str:
        return mock_projector.key_path_of("mcp", "demo")

    def close(self) -> None:
        self.unit_of_work.close()


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ) as test_client:
        yield test_client


def _materialize(client, harness, **params):
    return client.post(
        f"/api/bindings/{harness.binding.id}/materialize", params=params
    )


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #


def test_materialize_defaults_to_dry_run_and_writes_nothing(client, harness):
    """不带 confirm 就只算不写——默认必须是「不动用户的文件」。"""
    response = _materialize(client, harness)
    assert response.status_code == 200
    payload = response.json()
    assert payload["bindingId"] == harness.binding.id
    assert payload["dryRun"] is True
    assert payload["backupPath"] is None
    # 变更集算出来了……
    applied = payload["result"]["applied"]
    assert [e["keyPath"] for e in applied] == [harness.key_path()]
    assert applied[0]["action"] == "set"
    assert applied[0]["after"] == MCP_VALUE
    # ……但磁盘上什么都没有。
    assert not harness.config_path().exists()


def test_dry_run_leaves_no_record(client, harness):
    """dry-run 不是一次物化，不该在账上出现。"""
    _materialize(client, harness)
    rows = asyncio.run(
        harness.repositories.projections.list_for_binding(harness.binding.id)
    )
    assert rows == ()


def test_confirm_must_be_explicit(client, harness):
    """``confirm=maybe`` 不算真值——拿不准的取值一律从严。"""
    assert _materialize(client, harness, confirm="maybe").json()["dryRun"] is True
    assert not harness.config_path().exists()


# --------------------------------------------------------------------------- #
# confirm
# --------------------------------------------------------------------------- #


def test_confirm_writes_and_records(client, harness):
    """``confirm=1``：落盘 + 留一行账。"""
    payload = _materialize(client, harness, confirm="1").json()
    assert payload["dryRun"] is False
    assert harness.config()[harness.key_path()] == MCP_VALUE

    rows = asyncio.run(
        harness.repositories.projections.list_for_binding(harness.binding.id)
    )
    assert len(rows) == 1
    assert rows[0].project_id == harness.project.id
    assert rows[0].summary["keyPaths"] == [harness.key_path()]
    assert rows[0].summary["changedCount"] == 1


def test_second_confirm_is_unchanged_and_backs_up(client, harness):
    """第二次写：内容没变（幂等），但既有文件仍然先备份再动。"""
    _materialize(client, harness, confirm="1")
    payload = _materialize(client, harness, confirm="1").json()
    assert [e["action"] for e in payload["result"]["applied"]] == ["unchanged"]
    # 没有改动就没有写，也就没有新备份——这一条正是「幂等」的可观察形态。
    assert payload["backupPath"] is None
    assert harness.config()[harness.key_path()] == MCP_VALUE


def test_backup_path_appears_when_an_existing_file_is_rewritten(client, harness):
    """已有 config 被改写时，响应里给得出那份备份的路径（回滚就靠它）。"""
    harness.config_path().write_text(
        json.dumps({"other/key": {"keep": True}}) + "\n", encoding="utf-8"
    )
    payload = _materialize(client, harness, confirm="1", adopt="1").json()
    backup = payload["backupPath"]
    assert backup is not None and Path(backup).exists()
    assert "kaus-backup-" in Path(backup).name
    # 没被点名的键原样保留。
    assert harness.config()["other/key"] == {"keep": True}


def test_factory_protected_key_is_not_overwritten(client, harness):
    """AD-59：当前有值、又不是 Kaus 写下的键，不带 adopt 一律不覆盖。"""
    harness.config_path().write_text(
        json.dumps({harness.key_path(): {"手写的": True}}) + "\n", encoding="utf-8"
    )
    payload = _materialize(client, harness, confirm="1").json()
    reasons = [e["reason"] for e in payload["result"]["unsupported"]]
    assert reasons == ["factory_protected"]
    assert harness.config()[harness.key_path()] == {"手写的": True}


# --------------------------------------------------------------------------- #
# 409：跑着的时候不许写
# --------------------------------------------------------------------------- #


def _start_conversation(harness) -> str:
    from app.conversations.models import Conversation

    conversation = asyncio.run(
        harness.repositories.conversations.save(
            Conversation.create(
                project_id=harness.project.id,
                agent_binding_id=harness.binding.id,
                title="跑着的",
            )
        )
    )
    harness.host.active.add(conversation.id)
    return conversation.id


def test_confirm_is_blocked_while_a_conversation_is_running(client, harness):
    """引擎正在读那份配置，边跑边改是两本账 → 409 ``binding_busy``。"""
    conversation_id = _start_conversation(harness)
    response = _materialize(client, harness, confirm="1")
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "binding_busy"
    assert body["activeConversationIds"] == [conversation_id]
    assert not harness.config_path().exists()


def test_dry_run_is_allowed_while_running(client, harness):
    """「看看会改什么」在跑着的时候也该给看——它不动任何文件。"""
    _start_conversation(harness)
    response = _materialize(client, harness)
    assert response.status_code == 200
    assert response.json()["dryRun"] is True


# --------------------------------------------------------------------------- #
# drift 四态
# --------------------------------------------------------------------------- #


def _drift(client, harness):
    response = client.get(f"/api/bindings/{harness.binding.id}/drift")
    assert response.status_code == 200
    return response.json()


def test_drift_reports_unmanaged_before_anything_is_written(client, harness):
    """Kaus 没写过这个键 → ``unmanaged``，**不算漂移**。"""
    payload = _drift(client, harness)
    (item,) = payload["items"]
    assert item["state"] == "unmanaged"
    assert item["keyPath"] == harness.key_path()
    assert payload["driftedCount"] == 0
    assert payload["bindingId"] == harness.binding.id
    assert payload["checkedAt"]


def test_drift_reports_in_sync_right_after_a_write(client, harness):
    _materialize(client, harness, confirm="1")
    payload = _drift(client, harness)
    assert [i["state"] for i in payload["items"]] == ["in_sync"]
    assert payload["driftedCount"] == 0


def test_drift_reports_drifted_when_someone_edits_the_value(client, harness):
    _materialize(client, harness, confirm="1")
    current = harness.config()
    current[harness.key_path()] = {"别人改的": True}
    harness.config_path().write_text(json.dumps(current) + "\n", encoding="utf-8")
    payload = _drift(client, harness)
    (item,) = payload["items"]
    assert item["state"] == "drifted"
    assert item["expected"] == MCP_VALUE
    assert item["actual"] == {"别人改的": True}
    assert payload["driftedCount"] == 1


def test_drift_reports_missing_when_the_key_is_deleted(client, harness):
    _materialize(client, harness, confirm="1")
    harness.config_path().write_text("{}\n", encoding="utf-8")
    payload = _drift(client, harness)
    (item,) = payload["items"]
    assert item["state"] == "missing"
    assert payload["driftedCount"] == 1


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #


def test_unknown_binding_is_404_on_both_endpoints(client):
    for response in (
        client.post("/api/bindings/binding:nope:mock/materialize"),
        client.get("/api/bindings/binding:nope:mock/drift"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "binding_not_found"


def test_missing_registry_is_400_not_500(tmp_path):
    """宿主没装 Driver Registry 是装配态的事实，不是服务器内部错误。"""
    built = Harness(tmp_path, with_driver=False)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        ) as client:
            response = client.post(f"/api/bindings/{built.binding.id}/materialize")
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "driver_not_registered"
    finally:
        built.close()


def test_driver_without_a_projector_is_400(harness):
    """没有投射面的 Driver → 400 ``projection_unsupported``，公共层不替它编一个。"""

    class _NoProjector:
        backend_id = harness.driver.backend_id

        async def inspect_drift(self, *args, **kwargs):  # pragma: no cover - 不会被调到
            raise AssertionError

    # 一个只认这一个 Driver 的 Registry 替身。
    registry = type("R", (), {"get": staticmethod(lambda _id: _NoProjector())})()
    app = fastapi.FastAPI()
    app.include_router(
        build_binding_write_router(
            harness.repositories,
            auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
            registry=registry,
        )
    )
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        response = client.post(f"/api/bindings/{harness.binding.id}/materialize")
        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "projection_unsupported"
        assert body["missingMethod"] == "materialize_project_capabilities"


def test_both_endpoints_require_auth(harness):
    with TestClient(harness.app) as anonymous:
        assert (
            anonymous.post(
                f"/api/bindings/{harness.binding.id}/materialize"
            ).status_code
            == 401
        )
        assert (
            anonymous.get(f"/api/bindings/{harness.binding.id}/drift").status_code
            == 401
        )


# --------------------------------------------------------------------------- #
# 红线：账上没有值
# --------------------------------------------------------------------------- #


def test_projection_summary_carries_key_paths_and_counts_only():
    """§5.4：``projection_results.summary`` 里不得出现任何 before/after 的值。"""
    result = ProjectionResult(
        binding_id="binding:workbench:mock",
        dry_run=False,
        backup_path="/tmp/mock-config.json.kaus-backup-x",
        applied=(
            ProjectionEntry(
                capability_type="mcp",
                capability_id="demo",
                level="native",
                key_path="mcp/demo",
                before={"旧": "值"},
                after={"新": "值"},
                action="set",
            ),
        ),
        unsupported=(
            ProjectionEntry(
                capability_type="providers",
                capability_id="p1",
                level="native",
                reason="credential_bearing",
            ),
        ),
    )
    summary = _projection_summary(result)
    assert summary["keyPaths"] == ["mcp/demo"]
    assert summary["changedCount"] == 1
    assert summary["unsupportedCount"] == 1
    assert summary["unsupportedReasons"] == {"credential_bearing": 1}
    flat = json.dumps(summary, ensure_ascii=False)
    for forbidden in ("旧", "新", "before", "after"):
        assert forbidden not in flat
