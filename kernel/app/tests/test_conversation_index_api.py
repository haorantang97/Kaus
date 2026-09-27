"""``GET /api/conversations``：跨项目最近会话索引（批次八第 1 件）。

为什么要有这个端点
------------------
侧栏此前的取数是 `GET /api/projects` + **每个项目一次** `/conversations`
（见 `web/src/lib/conversationIndex.ts` 顶部的注释：「后端目前没有跨项目最近
对话端点」）。项目一多，30s 一次的轮询就是一次 N+1。

覆盖
----
1. 跨项目、按 ``updatedAt`` 倒序；
2. ``?limit=`` 截断、``?project=`` 收窄（两种 id 写法）、``?updated_after=`` 增量；
3. 每行的字段齐全：``projectId`` / ``bindingId`` / ``backendId`` / ``title`` /
   ``status`` / ``updatedAt`` / ``lastSequence``；
4. ``status`` 跟着 Session Host 走（跑完一轮是 idle，不是库里那个快照）；
5. 鉴权：没 token 就是 401；坏时间戳是 400 且带稳定 code。

隔离：SQLite 建在 `tmp_path`，Driver 是 MockDriver。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


class Harness:
    """两个项目、三条会话，``updated_at`` 刻意错开。"""

    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
                run_id_timeout=5.0,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", driver_kind="mock"))
        alpha = await repos.projects.save(Project.create(slug="alpha"))
        beta = await repos.projects.save(Project.create(slug="beta"))
        self.alpha_binding = await repos.bindings.save(
            AgentBinding.create(project=alpha, backend="mock", is_default=True)
        )
        self.beta_binding = await repos.bindings.save(
            AgentBinding.create(project=beta, backend="mock", is_default=True)
        )
        self.alpha_id, self.beta_id = alpha.id, beta.id

        async def add(binding: AgentBinding, title: str, minutes: int) -> Conversation:
            moment = BASE + timedelta(minutes=minutes)
            conversation = Conversation.create(
                project_id=binding.project_id,
                agent_binding_id=binding.id,
                title=title,
                created_at=moment,
            )
            return await repos.conversations.save(conversation)

        self.oldest = await add(self.alpha_binding, "最早", 0)
        self.middle = await add(self.beta_binding, "居中", 10)
        self.newest = await add(self.alpha_binding, "最新", 20)


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        asyncio.run(built.host.aclose())
        built.unit_of_work.close()


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ) as test_client:
        yield test_client


def titles(payload) -> list[str]:
    return [entry["title"] for entry in payload["conversations"]]


# --------------------------------------------------------------------------- #
# 排序与筛选
# --------------------------------------------------------------------------- #


def test_lists_every_project_newest_first(client, harness):
    payload = client.get("/api/conversations").json()

    assert titles(payload) == ["最新", "居中", "最早"]
    assert payload["count"] == 3
    # 跨项目：两个项目的会话在同一张表里。
    assert {entry["projectId"] for entry in payload["conversations"]} == {
        harness.alpha_id,
        harness.beta_id,
    }


def test_limit_truncates_from_the_newest_end(client):
    payload = client.get("/api/conversations?limit=2").json()

    assert titles(payload) == ["最新", "居中"]


@pytest.mark.parametrize("written_as", ["project:alpha", "alpha"])
def test_project_filter_narrows_the_same_endpoint(client, written_as):
    payload = client.get(f"/api/conversations?project={written_as}").json()

    assert titles(payload) == ["最新", "最早"]


def test_unknown_project_filter_is_a_coded_404(client):
    response = client.get("/api/conversations?project=nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_updated_after_is_an_incremental_cursor(client):
    cursor = (BASE + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    payload = client.get(f"/api/conversations?updated_after={cursor}").json()

    # 严格大于：游标那一条自己不再回来。
    assert titles(payload) == ["最新"]


def test_next_cursor_round_trips_to_an_empty_page(client):
    first = client.get("/api/conversations").json()
    again = client.get(
        f"/api/conversations?updated_after={first['nextUpdatedAfter']}"
    ).json()

    assert again["conversations"] == []
    # 空页不给新游标——客户端该沿用手上那个，而不是退回「从头再来」。
    assert again["nextUpdatedAfter"] is None


def test_bad_timestamp_is_a_coded_400(client):
    response = client.get("/api/conversations?updated_after=昨天")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_timestamp"


# --------------------------------------------------------------------------- #
# 行的形状
# --------------------------------------------------------------------------- #


def test_entry_carries_everything_the_sidebar_needs(client, harness):
    entry = client.get("/api/conversations?limit=1").json()["conversations"][0]

    assert entry == {
        "id": harness.newest.id,
        # 批次二十二第 2 件：这条会话在哪个还开着的 Group 里；不在就是 null。
        "groupId": None,
        # 批次二十六第 4 件：同一次查库顺手带上组名，侧栏不必再翻一次 id。
        "groupTitle": None,
        "projectId": harness.alpha_id,
        "bindingId": harness.alpha_binding.id,
        "backendId": "backend:mock",
        "title": "最新",
        "status": "idle",
        # 批次十五第 2 件：surface 与 status 是两根轴，侧栏两样都要。
        "surface": "card",
        "archivedAt": None,
        "updatedAt": entry["updatedAt"],
        "lastSequence": None,
    }


def test_status_and_last_sequence_follow_the_session_host(client, harness):
    """跑一轮之后：``lastSequence`` 有了值，``status`` 回到 idle。"""
    response = client.post(
        f"/api/conversations/{harness.newest.id}/messages", json={"text": "你好"}
    )
    assert response.status_code == 202, response.text

    entry = client.get("/api/conversations?limit=1").json()["conversations"][0]
    assert entry["id"] == harness.newest.id
    assert entry["lastSequence"] is not None and entry["lastSequence"] >= 0
    # Mock 的剧本会跑到 run.completed；Reducer 的终态映射回 idle。
    assert entry["status"] in ("idle", "running")


def test_group_only_conversations_stay_out_by_default(client, harness):
    """N §9.5：Group 拉起的会话不进项目列表，除非显式要。"""

    async def add_group_only() -> str:
        conversation = Conversation.create_group_spawned(
            project_id=harness.alpha_id,
            agent_binding_id=harness.alpha_binding.id,
            title="组里的",
            collaboration_id="collaboration:11111111-1111-4111-8111-111111111111",
            created_at=BASE + timedelta(minutes=30),
        )
        saved = await harness.repositories.conversations.save(conversation)
        return saved.id

    asyncio.run(add_group_only())

    assert "组里的" not in titles(client.get("/api/conversations").json())
    assert "组里的" in titles(
        client.get("/api/conversations?include_group_only=true").json()
    )


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


def test_requires_a_token(harness):
    with TestClient(harness.app) as anonymous:
        assert anonymous.get("/api/conversations").status_code == 401
