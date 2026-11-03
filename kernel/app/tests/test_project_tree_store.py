"""Project tree store: tree rows, instruction rows and archives on the domain database."""
from __future__ import annotations

from pathlib import Path

from app.persistence.sqlite import SqliteDatabase, build_repository_set
from app.projects.tree_store import ARCHIVED_FLAG, ProjectTreeStore


def open_store(tmp_path: Path) -> ProjectTreeStore:
    store = ProjectTreeStore(SqliteDatabase(tmp_path / "domain.sqlite3"))
    store.ensure_root("Root")
    return store


def test_root_is_created_once_and_every_write_bumps_the_revision(tmp_path):
    store = open_store(tmp_path)
    assert store.ensure_root("Other") is False
    assert store.nodes()["default"]["display_name"] == "Root"
    first = store.revision()
    store.create("design", display_name="Design", description="d")
    assert store.revision() == first + 1
    store.set_parent("design", "default")
    store.set_display("design", "Design 2")
    store.set_flag("design", "killed", True)
    assert store.revision() == first + 4
    node = store.nodes()["design"]
    assert (node["parent"], node["display_name"], node["draft"], node["killed"], node["description"]) == (
        "default", "Design 2", False, True, "d")
    # The rows are ordinary Project rows for the repositories too.
    import asyncio
    project = asyncio.run(build_repository_set(store.db).projects.get("project:design"))
    assert project.parent_project_id == "project:default" and project.status == "active"
    assert project.metadata["ui_state"] == {"draft": False, "pinned": False, "killed": True}


def test_drafts_have_no_parent_until_placed(tmp_path):
    store = open_store(tmp_path)
    store.create("draft-one", draft=True)
    node = store.nodes()["draft-one"]
    assert node["draft"] and node["parent"] is None and node["status"] == "draft"
    store.set_parent("draft-one", "default")
    assert store.nodes()["draft-one"]["draft"] is False


def test_pinned_order_is_the_order_of_pinning(tmp_path):
    store = open_store(tmp_path)
    for slug in ("b", "a", "c"):
        store.create(slug, draft=False, parent="default")
        store.set_flag(slug, "pinned", True)
    assert store.pinned() == ["b", "a", "c"]
    store.set_flag("a", "pinned", False)
    store.set_flag("a", "pinned", True)
    assert store.pinned() == ["b", "c", "a"]


def test_archive_keeps_rows_and_restore_brings_the_project_back(tmp_path):
    store = open_store(tmp_path)
    store.create("design", display_name="Design", draft=False, parent="default", description="d")
    store.put_row("design", "instructions", "project-persona", "local", {"body": "persona"})
    archive_id = store.archive("design", {"parent": "default", "label": "Design", "batchId": "b1"})
    assert "design" not in store.nodes()
    disabled = store.node("design")
    assert disabled["status"] == "disabled" and disabled["metadata"][ARCHIVED_FLAG] is True
    assert store.rows("design") and store.latest_archive("design")["id"] == archive_id
    store.restore("design", archive_id, parent="default", display_name="Design", pinned=True)
    node = store.nodes()["design"]
    assert node["parent"] == "default" and node["pinned"] and ARCHIVED_FLAG not in node["metadata"]
    assert node["description"] == "d"
    assert store.latest_archive("design") is None
    assert store.rows("design")[0]["config"] == {"body": "persona"}


def test_reusing_a_deleted_slug_snapshots_and_clears_its_rows(tmp_path):
    store = open_store(tmp_path)
    store.create("design", draft=False, parent="default")
    store.put_row("design", "instructions", "project-persona", "local", {"body": "old persona"})
    store.put_row("design", "mcp", "mcp_servers", "block", {})
    archive_id = store.archive("design", {"parent": "default"})
    fresh = store.create("design", display_name="New")
    assert fresh["draft"] and fresh["display_name"] == "New"
    assert store.rows("design") == []
    archived = store.latest_archive("design")
    assert archived["id"] == archive_id
    cleared = {(r["capability_type"], r["capability_id"]) for r in archived["snapshot"]["clearedRows"]}
    assert cleared == {("instructions", "project-persona"), ("mcp", "mcp_servers")}
    # Deleting the new one and restoring the older archive brings the cleared rows back.
    store.archive("design", {"parent": None})
    newer = store.latest_archive("design")
    assert newer["id"] != archive_id
    store.restore("design", archive_id, parent="default")
    assert {r["capability_id"] for r in store.rows("design")} == {"project-persona", "mcp_servers"}


def test_replace_rows_only_touches_the_selected_ids(tmp_path):
    store = open_store(tmp_path)
    store.create("design", draft=False, parent="default")
    store.put_row("design", "instructions", "project-persona", "local", {"body": "persona"})
    store.put_row("design", "instructions", "rule-a", "local", {"body": "a"})
    store.replace_rows("design", "instructions",
                       [{"capability_id": "rule-b", "assignment_mode": "block", "config": {}}],
                       select=lambda cid: cid.startswith("rule-"), metadata={"layout": {"rule-b": 1}})
    rows = {r["capability_id"]: r for r in store.rows("design", "instructions")}
    assert set(rows) == {"project-persona", "rule-b"}
    assert rows["rule-b"]["assignment_mode"] == "block"
    assert store.node("design")["metadata"]["layout"] == {"rule-b": 1}
    assert store.block_rows() == [("design", "instructions", "rule-b")]


def test_ensure_node_adds_a_row_and_a_default_binding_for_an_unknown_slug(tmp_path):
    store = open_store(tmp_path)
    assert store.ensure_node("native", parent="default", binding=("engine", "native")) is True
    assert store.ensure_node("native", parent="default", binding=("engine", "native")) is False
    row = store.db.query_one("SELECT * FROM agent_bindings WHERE project_id = 'project:native'")
    assert row["id"] == "binding:native:engine" and row["is_default"] == 1 and row["native_scope_ref"] == "native"
    assert store.nodes()["native"]["parent"] == "default"
