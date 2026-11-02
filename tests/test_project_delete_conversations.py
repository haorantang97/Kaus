"""Deleting a Project through the tree editor keeps its conversations.

In-memory repositories, a fake Session Host and a temporary state tree only.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "kernel"):
    sys.path.insert(0, str(path))

import project_config_store as store  # noqa: E402
import project_config_sync as sync  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.memory import in_memory_repository_set  # noqa: E402
from app.projects.models import Project  # noqa: E402


def host(tmp_path):
    state = tmp_path / "dashboard"
    state.mkdir()
    home = tmp_path / "native"
    (home / "profiles").mkdir(parents=True)
    (home / "config.yaml").write_text("model:\n  default: native-model\n")
    for name, value in {"hierarchy": {}, "labels": {"default": "X"}, "drafts": [],
                        "pinned": [], "killed": [], "skill_inherit_off": []}.items():
        (state / (name + ".json")).write_text(json.dumps(value))
    return SimpleNamespace(HERE=state, HERMES_HOME=home, PROFILES_DIR=home / "profiles",
        _profile_dir=lambda name: store.directory(state, name) if store.contains(state, name)
            else home if name == "default" else home / "profiles" / name)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


class FakeHost:
    def __init__(self):
        self.active: set[str] = set()
        self.owners: dict[str, SimpleNamespace] = {}
        self.stopped: list[str] = []

    def is_active(self, conversation_id):
        return conversation_id in self.active

    async def describe_runtime_owner(self, conversation_id):
        return self.owners.get(conversation_id)

    async def stop_runtime(self, conversation_id):
        self.stopped.append(conversation_id)
        self.active.discard(conversation_id)
        self.owners.pop(conversation_id, None)


class FakeCoordinator:
    """Stands in for SurfaceCoordinator.to_card(force=True): ends the terminal writer."""

    def __init__(self, host):
        self.host, self.returned = host, []

    async def to_card(self, conversation, *, force=False):
        assert force
        self.returned.append(conversation.id)
        self.host.owners.pop(conversation.id, None)


def build(tmp_path):
    fixture = host(tmp_path)
    repos = in_memory_repository_set()
    asyncio.run(repos.projects.save(Project.create(slug="default")))
    store.create(fixture.HERE, "design")
    (fixture.HERE / "hierarchy.json").write_text('{"design": "default"}')
    asyncio.run(sync.apply_delta(repos, {"projects": {}, "capabilities": {}}, sync.snapshot(fixture)))
    conversations = [
        asyncio.run(repos.conversations.save(Conversation.create(
            project_id="project:design", agent_binding_id="binding:design:mock", title=title,
            native_session_id=f"native-{index}")))
        for index, title in enumerate(("one", "two"))
    ]
    app = fastapi.FastAPI()

    @app.delete("/api/agent/{name}")
    def delete(name: str):
        store.archive(fixture.HERE, name)
        return {"ok": True, "deleted": name}

    @app.post("/api/agent/create-empty")
    def create():
        store.create(fixture.HERE, "design")
        return {"ok": True}

    fake = FakeHost()
    fake.coordinator = FakeCoordinator(fake)
    sync.install(app, fixture, repos, session_host=fake, surface_coordinator=fake.coordinator)
    return fixture, repos, fake, app, conversations


def test_delete_is_refused_while_a_conversation_runs(tmp_path):
    fixture, repos, fake, app, conversations = build(tmp_path)
    fake.active.add(conversations[0].id)
    with TestClient(app) as client:
        response = client.delete("/api/agent/design")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "project_has_active_runtime"
    assert body["error"]["activeConversationIds"] == [conversations[0].id]
    assert body["detail"]
    assert store.contains(fixture.HERE, "design")
    assert asyncio.run(repos.projects.get("project:design")).status == "active"
    assert all(asyncio.run(repos.conversations.get(c.id)).archived_at is None for c in conversations)


def test_delete_archives_conversations_and_recreate_keeps_them_archived(tmp_path):
    fixture, repos, fake, app, conversations = build(tmp_path)
    with TestClient(app) as client:
        assert client.delete("/api/agent/design").status_code == 200
        project = asyncio.run(repos.projects.get("project:design"))
        assert project.status == "disabled" and project.metadata["configurationArchived"]
        for original in conversations:
            kept = asyncio.run(repos.conversations.get(original.id))
            assert kept.archived_at is not None
            assert kept.native_session_id == original.native_session_id
        # Re-creating the same slug reopens the Project, not its old conversations.
        assert client.post("/api/agent/create-empty").status_code == 200
    project = asyncio.run(repos.projects.get("project:design"))
    assert project.status == "active" and "configurationArchived" not in project.metadata
    assert all(asyncio.run(repos.conversations.get(c.id)).archived_at is not None for c in conversations)


def test_startup_archives_conversations_of_projects_deleted_earlier(tmp_path):
    fixture, repos, fake, app, conversations = build(tmp_path)
    project = asyncio.run(repos.projects.get("project:design"))
    asyncio.run(repos.projects.save(project.evolve(status="disabled")))
    with TestClient(app):
        pass
    assert all(asyncio.run(repos.conversations.get(c.id)).archived_at is not None for c in conversations)


def test_confirmed_delete_stops_card_and_terminal_conversations_then_archives(tmp_path):
    fixture, repos, fake, app, conversations = build(tmp_path)
    card, terminal = conversations
    fake.active.add(card.id)
    fake.owners[card.id] = SimpleNamespace(owner_type="card", is_stale=False)
    fake.owners[terminal.id] = SimpleNamespace(owner_type="external-cli", is_stale=True)
    with TestClient(app) as client:
        refused = client.delete("/api/agent/design")
        assert refused.status_code == 409
        assert sorted(refused.json()["error"]["activeConversationIds"]) == sorted([card.id, terminal.id])
        assert fake.stopped == [] and fake.coordinator.returned == []
        assert client.delete("/api/agent/design?force=1").status_code == 200
    assert fake.coordinator.returned == [terminal.id]
    assert fake.stopped == [card.id]
    assert not store.contains(fixture.HERE, "design")
    assert asyncio.run(repos.projects.get("project:design")).status == "disabled"
    for original in conversations:
        kept = asyncio.run(repos.conversations.get(original.id))
        assert kept.archived_at is not None and kept.native_session_id == original.native_session_id


def test_confirmed_delete_still_refuses_what_cannot_be_stopped(tmp_path):
    fixture, repos, fake, app, conversations = build(tmp_path)
    fake.owners[conversations[0].id] = SimpleNamespace(owner_type="external-cli", is_stale=False)

    async def stuck(conversation, *, force=False):
        raise RuntimeError("terminal did not exit")
    fake.coordinator.to_card = stuck
    with TestClient(app) as client:
        response = client.delete("/api/agent/design?force=1")
    assert response.status_code == 409
    assert store.contains(fixture.HERE, "design")
    assert asyncio.run(repos.conversations.get(conversations[0].id)).archived_at is None


@pytest.fixture()
def server_fixture(tmp_path, monkeypatch):
    import capability_import
    monkeypatch.setenv("HERMES_HOME", "/nonexistent/kaus-tests")
    monkeypatch.setenv("DASH_FEATURE_SESSION_HOST_V1", "0")
    monkeypatch.setenv("DASH_FEATURE_PROJECT_DOMAIN_V1", "0")
    server = capability_import.load_server_module()
    fixture = host(tmp_path)
    with capability_import.bound_server(server, state_root=fixture.HERE, hermes_home=fixture.HERMES_HOME):
        calls = []
        # Tree reads may list native profiles; no native profile is created or deleted.
        monkeypatch.setattr(server, "run_hermes", lambda args, **k: calls.append(args) or (1, "", "unavailable"))
        server._invalidate_profile_list_cache()
        yield server, fixture
        assert all(args[:2] == ["profile", "list"] for args in calls), calls


def _code(response):
    return json.loads(response.body)["error"]["code"]


def test_restore_returns_deleted_project_to_its_original_position(server_fixture):
    server, fixture = server_fixture
    for name, display in (("parent", "Parent"), ("design", "Design")):
        server.api_create_empty(server.CreateEmptyReq(name=name, display=display))
    server.api_move(server.MoveReq(node="parent", new_parent="default"))
    server.api_move(server.MoveReq(node="design", new_parent="parent"))
    (store.directory(fixture.HERE, "design") / "SOUL.md").write_text("Kept persona")
    server.api_delete_agent("design")
    assert not store.contains(fixture.HERE, "design")
    assert "design" not in server._load_hierarchy()
    result = server.api_restore_deleted_agent("design")
    assert result["ok"] and result["parent"] == "parent"
    assert server._load_hierarchy()["design"] == "parent"
    assert server._label("design") == "Design"
    assert (store.directory(fixture.HERE, "design") / "SOUL.md").read_text() == "Kept persona"
    assert not (store.directory(fixture.HERE, "design") / store.DELETED_RECORD).exists()
    assert store.latest_archive(fixture.HERE, "design") is None
    assert _code(server.api_restore_deleted_agent("design")) == "project_exists"


def test_restore_without_parent_or_archive_returns_a_code(server_fixture):
    server, fixture = server_fixture
    for name in ("parent", "design"):
        server.api_create_empty(server.CreateEmptyReq(name=name))
    server.api_move(server.MoveReq(node="parent", new_parent="default"))
    server.api_move(server.MoveReq(node="design", new_parent="parent"))
    server.api_delete_agent("design")
    server.api_delete_agent("parent")
    missing_parent = server.api_restore_deleted_agent("design")
    assert missing_parent.status_code == 409 and _code(missing_parent) == "project_parent_missing"
    assert store.latest_archive(fixture.HERE, "design") is not None  # nothing moved
    assert _code(server.api_restore_deleted_agent("never-existed")) == "project_archive_missing"


# --------------------------------------------------------------------------- #
# Cascade delete and anchored restore (real server handlers, temporary state)
# --------------------------------------------------------------------------- #
def _tree(server, edges):
    """Create Projects and place them: ``edges`` is [(name, parent), ...] top-down."""
    for name, parent in edges:
        server.api_create_empty(server.CreateEmptyReq(name=name, display=name.title()))
        server.api_move(server.MoveReq(node=name, new_parent=parent))


def _record(fixture, name):
    return store.deleted_record(store.latest_archive(fixture.HERE, name))


TREE = [("parent", "default"), ("design", "parent"), ("child", "design"),
        ("grand", "child"), ("grand2", "child"), ("other", "parent")]


def test_cascade_delete_archives_each_descendant_with_a_shared_batch(server_fixture):
    server, fixture = server_fixture
    _tree(server, TREE)
    preview = server.api_delete_preview("design")
    assert [d["name"] for d in preview["descendants"]] == ["child", "grand", "grand2"]
    result = server.api_delete_agent("design")
    assert result["ok"] and result["deleted"] == "design"
    assert result["cascade"] == ["grand2", "grand", "child"]  # deepest first
    for name in ("design", "child", "grand", "grand2"):
        assert not store.contains(fixture.HERE, name)
        assert name not in server._load_hierarchy()
    assert store.contains(fixture.HERE, "parent") and store.contains(fixture.HERE, "other")
    records = {name: _record(fixture, name) for name in ("design", "child", "grand", "grand2")}
    assert len({r["batchId"] for r in records.values()}) == 1
    assert all(r["batchRoot"] == "design" for r in records.values())
    assert records["design"]["parent"] == "parent" and records["design"]["ancestors"] == ["default", "parent"]
    assert records["grand"]["parent"] == "child"
    assert records["grand"]["ancestors"] == ["default", "parent", "design", "child"]
    assert records["child"]["label"] == "Child" and records["child"]["inHierarchy"]
    stamps = {name: store.latest_archive(fixture.HERE, name).name.rsplit("-", 1)[1] for name in records}
    assert stamps["grand"] < stamps["child"] < stamps["design"]


def test_default_still_cannot_be_deleted(server_fixture):
    server, _ = server_fixture
    with pytest.raises(server.HTTPException) as refused:
        server.api_delete_agent("default")
    assert refused.value.status_code == 400


def test_restore_refuses_when_the_parent_moved(server_fixture):
    server, fixture = server_fixture
    _tree(server, [("parent", "default"), ("design", "parent"), ("elsewhere", "default")])
    server.api_delete_agent("design")
    server.api_move(server.MoveReq(node="parent", new_parent="elsewhere"))
    moved = server.api_restore_deleted_agent("design")
    assert moved.status_code == 409 and _code(moved) == "project_anchor_moved"
    assert store.latest_archive(fixture.HERE, "design") is not None  # nothing moved
    assert not store.contains(fixture.HERE, "design")
    # Back at the recorded path, the restore goes through.
    server.api_move(server.MoveReq(node="parent", new_parent="default"))
    assert server.api_restore_deleted_agent("design")["parent"] == "parent"


def test_record_without_a_path_keeps_the_parent_exists_rule(server_fixture):
    server, fixture = server_fixture
    _tree(server, [("parent", "default"), ("design", "parent"), ("elsewhere", "default")])
    server.api_delete_agent("design")
    archived = store.latest_archive(fixture.HERE, "design")
    record = store.deleted_record(archived)
    for key in ("ancestors", "batchId", "batchRoot"):
        record.pop(key)
    (archived / store.DELETED_RECORD).write_text(json.dumps(record))
    server.api_move(server.MoveReq(node="parent", new_parent="elsewhere"))
    result = server.api_restore_deleted_agent("design")
    assert result["ok"] and result["parent"] == "parent" and result["chain"] == []


def test_restore_inside_a_cascade_brings_back_the_ancestor_chain_only(server_fixture):
    server, fixture = server_fixture
    _tree(server, TREE)
    server.api_delete_agent("design")
    result = server.api_restore_deleted_agent("grand")
    assert result["ok"] and result["parent"] == "child" and result["chain"] == ["design", "child"]
    h = server._load_hierarchy()
    assert (h["design"], h["child"], h["grand"]) == ("parent", "design", "child")
    assert server._label("design") == "Design"
    assert not store.contains(fixture.HERE, "grand2")  # siblings stay deleted
    assert store.latest_archive(fixture.HERE, "grand2") is not None
    # The sibling now restores alone against its restored parent.
    assert server.api_restore_deleted_agent("grand2")["chain"] == []


def test_chain_restore_stops_at_the_first_failing_step(server_fixture):
    server, fixture = server_fixture
    _tree(server, TREE + [("elsewhere", "default")])
    server.api_delete_agent("design")
    server.api_move(server.MoveReq(node="parent", new_parent="elsewhere"))
    refused = server.api_restore_deleted_agent("grand")
    body = json.loads(refused.body)["error"]
    assert refused.status_code == 409 and body["code"] == "project_anchor_moved"
    assert body["project"] == "design" and body["restored"] == []
    for name in ("design", "child", "grand"):
        assert not store.contains(fixture.HERE, name) and store.latest_archive(fixture.HERE, name) is not None


def test_restore_refuses_a_name_in_use_and_keeps_the_archive(server_fixture):
    server, fixture = server_fixture
    _tree(server, TREE)
    server.api_delete_agent("design")
    _tree(server, [("child", "default")])
    assert _code(server.api_restore_deleted_agent("child")) == "project_exists"
    # A deleted descendant whose parent name is taken by a new Project is not re-anchored there.
    assert _code(server.api_restore_deleted_agent("grand")) == "project_anchor_moved"
    assert store.latest_archive(fixture.HERE, "child") is not None


def _cascade_app(server, fixture):
    repos = in_memory_repository_set()
    asyncio.run(repos.projects.save(Project.create(slug="default")))
    asyncio.run(sync.apply_delta(repos, {"projects": {}, "capabilities": {}}, sync.snapshot(server)))
    app = fastapi.FastAPI()
    app.delete("/api/agent/{name}")(server.api_delete_agent)
    fake = FakeHost()
    fake.coordinator = FakeCoordinator(fake)
    sync.install(app, server, repos, session_host=fake, surface_coordinator=fake.coordinator)
    return repos, fake, app


def _conversation(repos, slug, title):
    return asyncio.run(repos.conversations.save(Conversation.create(
        project_id=f"project:{slug}", agent_binding_id=f"binding:{slug}:mock", title=title,
        native_session_id=f"native-{slug}-{title}")))


def test_cascade_delete_covers_running_conversations_of_the_subtree(server_fixture):
    server, fixture = server_fixture
    _tree(server, TREE)
    repos, fake, app = _cascade_app(server, fixture)
    top, deep, sibling = (_conversation(repos, "design", "top"), _conversation(repos, "grand", "deep"),
                          _conversation(repos, "other", "sibling"))
    fake.active.add(deep.id)
    fake.owners[deep.id] = SimpleNamespace(owner_type="card", is_stale=False)
    fake.active.add(sibling.id)  # outside the subtree: neither blocks nor is stopped
    with TestClient(app) as client:
        refused = client.delete("/api/agent/design")
        assert refused.status_code == 409
        assert refused.json()["error"]["activeConversationIds"] == [deep.id]
        assert store.contains(fixture.HERE, "grand") and fake.stopped == []
        assert client.delete("/api/agent/design?force=1").status_code == 200
    assert fake.stopped == [deep.id]
    for slug in ("design", "child", "grand", "grand2"):
        assert asyncio.run(repos.projects.get(f"project:{slug}")).status == "disabled"
    for conversation in (top, deep):
        assert asyncio.run(repos.conversations.get(conversation.id)).archived_at is not None
    assert asyncio.run(repos.conversations.get(sibling.id)).archived_at is None
    assert asyncio.run(repos.projects.get("project:other")).status != "disabled"
