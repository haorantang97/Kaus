"""Deleting a Project keeps its conversations (``app/projects/retirement.py``).

SQLite in ``tmp_path`` and MockDriver only: no real Agent, model or credential.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.router import build_domain_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects import retirement  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from app.runtimes.models import RuntimeLease  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TOKEN = "test-token-retirement-0123"


class Harness:
    def __init__(self, tmp_path):
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.registry = BackendDriverRegistry([MockDriver()])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.app = fastapi.FastAPI()
        self.app.include_router(build_domain_router(self.repositories))
        self.app.include_router(build_session_router(
            session_host=self.host, repositories=self.repositories, registry=self.registry,
            auth_policy=SessionAuthPolicy(lambda: TOKEN), run_id_timeout=5.0,
        ))

    async def seed(self):
        repos = self.repositories
        await repos.backends.save(Backend.create(key="mock", display_name="Mock", driver_kind="mock"))
        project = await repos.projects.save(Project.create(slug="design", display_name="Design"))
        await repos.projects.save(Project.create(slug="kept", display_name="Kept"))
        self.binding = await repos.bindings.save(AgentBinding.create(
            project=project, backend="mock", display_name="Mock", is_default=True))
        self.project_id = project.id

    async def retire(self):
        """What project_config_sync does after the configuration directory is archived."""
        project = await self.repositories.projects.get(self.project_id)
        await self.repositories.projects.save(project.evolve(
            status="disabled", metadata={**project.metadata, "configurationArchived": True}))
        return await retirement.archive_project_conversations(self.repositories, self.project_id)

    def new_conversation(self, client, title="Kept history"):
        response = client.post(f"/api/projects/{self.project_id}/conversations",
                               json={"bindingId": self.binding.id, "title": title})
        assert response.status_code == 201, response.text
        return response.json()["id"]


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
    with TestClient(harness.app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        yield test_client


def test_live_runtime_blocks_project_delete_until_stopped(harness, client):
    ident = harness.new_conversation(client)
    assert client.post(f"/api/conversations/{ident}/messages", json={"text": "hi"}).status_code == 202
    assert harness.host.is_active(ident)
    blocking = asyncio.run(retirement.blocking_conversation_ids(
        harness.repositories, harness.host, harness.project_id))
    assert blocking == [ident]
    assert client.post(f"/api/conversations/{ident}/stop").status_code == 200
    assert asyncio.run(retirement.blocking_conversation_ids(
        harness.repositories, harness.host, harness.project_id)) == []


def test_external_terminal_lease_blocks_even_when_stale(harness, client):
    ident = harness.new_conversation(client)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    asyncio.run(harness.repositories.leases.acquire(RuntimeLease(
        conversation_id=ident, owner_type="external-cli", owner_id="terminal",
        acquired_at=old, heartbeat_at=old)))
    assert asyncio.run(retirement.blocking_conversation_ids(
        harness.repositories, harness.host, harness.project_id)) == [ident]


def test_deleted_project_archives_conversations_and_refuses_new_work(harness, client):
    ident = harness.new_conversation(client)
    assert client.post(f"/api/conversations/{ident}/messages", json={"text": "hi"}).status_code == 202
    assert client.post(f"/api/conversations/{ident}/stop").status_code == 200
    events_before = client.get(f"/api/conversations/{ident}/events/snapshot").json()

    assert asyncio.run(harness.retire()) == [ident]

    # Kept: row, events and archive-list entry; nothing deleted.
    assert client.get("/api/conversations").json()["count"] == 0
    archived = client.get("/api/conversations?archived=true").json()["conversations"]
    assert [(row["id"], row["projectId"]) for row in archived] == [(ident, harness.project_id)]
    detail = client.get(f"/api/conversations/{ident}")
    assert detail.status_code == 200 and detail.json()["conversation"]["archivedAt"]
    assert client.get(f"/api/conversations/{ident}/events/snapshot").json() == events_before

    send = client.post(f"/api/conversations/{ident}/messages", json={"text": "again"})
    assert send.status_code == 409 and send.json()["error"]["code"] == "project_deleted"
    restore = client.patch(f"/api/conversations/{ident}/archive", json={"archived": False})
    assert restore.status_code == 409 and restore.json()["error"]["code"] == "project_deleted"
    assert asyncio.run(harness.repositories.conversations.get(ident)).archived_at is not None
    create = client.post(f"/api/projects/{harness.project_id}/conversations",
                         json={"bindingId": harness.binding.id})
    assert create.status_code == 409 and create.json()["error"]["code"] == "project_deleted"
    assert not harness.host.is_active(ident)

    # Manual removal stays with the user.
    assert client.delete(f"/api/conversations/{ident}").status_code == 200
    assert asyncio.run(harness.repositories.conversations.get(ident)) is None


def test_orphans_from_earlier_deletes_are_archived_once(harness, client):
    ident = harness.new_conversation(client)
    project = asyncio.run(harness.repositories.projects.get(harness.project_id))
    asyncio.run(harness.repositories.projects.save(project.evolve(status="disabled")))
    assert asyncio.run(retirement.archive_orphaned_conversations(harness.repositories)) == [ident]
    assert asyncio.run(retirement.archive_orphaned_conversations(harness.repositories)) == []


def test_project_list_hides_deleted_projects_unless_requested(harness, client):
    asyncio.run(harness.retire())
    listed = client.get("/api/projects").json()
    assert [row["id"] for row in listed["projects"]] == ["project:kept"]
    full = client.get("/api/projects?includeDisabled=true").json()
    assert {row["id"]: row["status"] for row in full["projects"]} == {
        "project:design": "disabled", "project:kept": "active"}
    assert client.get(f"/api/projects/{harness.project_id}").json()["status"] == "disabled"


def _kept_binding(harness, backend="mock", **project_changes):
    async def seed():
        repos = harness.repositories
        kept = await repos.projects.get("project:kept")
        if project_changes:
            kept = await repos.projects.save(kept.evolve(**project_changes))
        if backend != "mock" and await repos.backends.get(f"backend:{backend}") is None:
            await repos.backends.save(Backend.create(key=backend, display_name=backend, driver_kind="native"))
        return await repos.bindings.save(AgentBinding.create(project=kept, backend=backend, display_name=backend))
    return asyncio.run(seed())


def test_move_keeps_engine_and_unarchives_into_target(harness, client):
    ident = harness.new_conversation(client)
    assert client.post(f"/api/conversations/{ident}/messages", json={"text": "hi"}).status_code == 202
    assert client.post(f"/api/conversations/{ident}/stop").status_code == 200
    asyncio.run(harness.retire())
    before = asyncio.run(harness.repositories.conversations.get(ident))
    other_engine = _kept_binding(harness, backend="ghost")
    mismatch = client.post(f"/api/conversations/{ident}/move",
                           json={"projectId": "project:kept", "bindingId": other_engine.id})
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "engine_mismatch"
    target = _kept_binding(harness)
    moved = client.post(f"/api/conversations/{ident}/move",
                        json={"projectId": "project:kept", "bindingId": target.id})
    assert moved.status_code == 200, moved.text
    wire = moved.json()["conversation"]
    assert wire["projectId"] == "project:kept" and wire["archivedAt"] is None
    after = asyncio.run(harness.repositories.conversations.get(ident))
    assert after.agent_binding_id == target.id and after.native_session_id == before.native_session_id
    assert asyncio.run(harness.repositories.bindings.get(after.agent_binding_id)).backend_id == harness.binding.backend_id
    assert client.get("/api/conversations").json()["conversations"][0]["id"] == ident
    assert client.post(f"/api/conversations/{ident}/messages", json={"text": "again"}).status_code == 202


def test_move_to_another_folder_needs_confirmation(harness, client, tmp_path):
    created = client.post(f"/api/projects/{harness.project_id}/conversations",
                          json={"bindingId": harness.binding.id, "nativeSessionRef": "native-kept"})
    ident = created.json()["id"]
    asyncio.run(harness.retire())
    target = _kept_binding(harness, workspace_root=str(tmp_path))
    body = {"projectId": "project:kept", "bindingId": target.id}
    warned = client.post(f"/api/conversations/{ident}/move", json=body)
    assert warned.status_code == 409 and warned.json()["error"]["code"] == "workspace_changed"
    assert warned.json()["error"]["targetWorkspace"] == str(tmp_path)
    assert asyncio.run(harness.repositories.conversations.get(ident)).archived_at is not None
    confirmed = client.post(f"/api/conversations/{ident}/move", json={**body, "confirmWorkspaceChange": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["conversation"]["nativeSessionId"] == "native-kept"


def test_move_refuses_live_conversations_and_deleted_targets(harness, client):
    ident = harness.new_conversation(client)
    target = _kept_binding(harness)
    live = client.post(f"/api/conversations/{ident}/move", json={"projectId": "project:kept", "bindingId": target.id})
    assert live.status_code == 409 and live.json()["error"]["code"] == "conversation_not_archived"
    asyncio.run(harness.retire())
    kept = asyncio.run(harness.repositories.projects.get("project:kept"))
    asyncio.run(harness.repositories.projects.save(kept.evolve(status="disabled")))
    gone = client.post(f"/api/conversations/{ident}/move", json={"projectId": "project:kept", "bindingId": target.id})
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "project_deleted"


def test_continuation_may_target_another_project_only_from_a_deleted_one(harness, tmp_path):
    from app.conversations.continuation import ContinuationService
    service = ContinuationService(harness.repositories, harness.host, harness.registry, tmp_path / "c", tmp_path / "a")
    target = _kept_binding(harness)
    assert not asyncio.run(service._target_allowed(target, harness.project_id))
    asyncio.run(harness.retire())
    assert asyncio.run(service._target_allowed(target, harness.project_id))
    assert asyncio.run(service._target_allowed(harness.binding, harness.project_id))
