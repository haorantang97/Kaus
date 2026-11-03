"""The domain database is the only source of the Project tree, instruction content and archives.

Covers the one-time import of the legacy files (``project_file_import``), the tree
editor endpoints writing only to the database, and the rule-layer round trip.
Temporary directories only; no Agent is started and no Agent directory is written.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "kernel"):
    sys.path.insert(0, str(path))

import project_file_import  # noqa: E402
import project_instructions  # noqa: E402
from app.persistence.sqlite import SqliteDatabase, build_repository_set  # noqa: E402
from app.projects.tree_store import ProjectTreeStore  # noqa: E402

CONST = project_instructions.CONSTITUTION_BEGIN + "\ninjected rules\n" + project_instructions.CONSTITUTION_END + "\n\n"
PROJECTED = "<!-- kaus:instructions:begin project=project:design -->\n## x\n\nold\n<!-- kaus:instructions:end -->\n"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def legacy_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A state root with every legacy file plus a fake native home (read-only in the import)."""
    state = tmp_path / "kaus"
    state.mkdir()
    home = tmp_path / "native"
    (home / "profiles" / "coding" / "skills" / "shared").mkdir(parents=True)
    (home / "profiles" / "sub" / "skills").mkdir(parents=True)
    (home / "profiles" / "stray").mkdir(parents=True)   # a native directory that is not a Project
    (home / "config.yaml").write_text("model:\n  default: root-model\ncompression:\n  enabled: true\n")
    (home / "constitution.md").write_text("# root rules\n### [rule:tone] Tone\nbe brief\n")
    (home / "profiles" / "coding" / "SOUL.md").write_text(CONST + "I am the coding agent\n")
    (home / "profiles" / "coding" / "constitution.md").write_text("coding layer\n")
    (home / "profiles" / "coding" / "config.yaml").write_text("compression:\n  enabled: false\n")
    write_json(state / "hierarchy.json", {"design": "coding", "sub": "coding", "loop-a": "loop-b", "loop-b": "loop-a"})
    write_json(state / "labels.json", {"default": "X", "design": "Design", "coding": "Coding", "stray": "Stray"})
    write_json(state / "drafts.json", ["sketch"])
    write_json(state / "pinned.json", ["design", "coding"])
    write_json(state / "killed.json", ["sub"])
    write_json(state / "skill_inherit_off.json", ["design"])
    write_json(state / "config_inherit_block.json", {"design": ["mcp_servers", "delegation"]})
    configs = state / "state" / "project-configs"
    for name, description in (("design", "The design project"), ("sketch", "")):
        directory = configs / name
        (directory / "skills" / "tool").mkdir(parents=True)
        (directory / "skills" / "tool" / "SKILL.md").write_text("a skill file")
        write_json(directory / ".kaus-project.json", {"description": description})
        (directory / "SOUL.md").write_text(CONST + PROJECTED + f"\n{name} persona\n")
        (directory / "constitution.md").write_text("design text\n### [override:tone] Tone\nbe long\n### [disable:old]\n" if name == "design" else "")
        (directory / "config.yaml").write_text("mcp_servers:\n  fs:\n    command: mcp-fs\nproviders:\n  p:\n    api_key: sk-testonly0000000000000000000000000000000000000000\n")
    archive = state / "state" / "project-config-archive" / "old-20260101T000000000000Z"
    archive.mkdir(parents=True)
    write_json(archive / ".kaus-project.json", {"description": "was deleted"})
    write_json(archive / ".kaus-deleted.json", {"inHierarchy": True, "parent": "default", "ancestors": ["default"],
                                                "batchId": "b1", "batchRoot": "old", "label": "Old"})
    (archive / "SOUL.md").write_text("old persona\n")
    (archive / "constitution.md").write_text("old layer\n")
    return state, home


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        digest.update(f"{path.relative_to(root)}|{stat.st_mode}|{stat.st_size}|{stat.st_mtime_ns}".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def paired_db(state: Path) -> Path:
    return state / "state" / "domain.sqlite3"


def seeded_store(state: Path) -> ProjectTreeStore:
    """A database that already holds rows (as a user's database would): a native persona row
    that must be dropped, and a capability row whose value must survive the import."""
    store = ProjectTreeStore(SqliteDatabase(paired_db(state)))
    store.ensure_root("Renamed root")
    store.create("coding", display_name="Old label", draft=False, parent="default")
    store.put_row("coding", "instructions", "project-persona", "local", {"body": "mirrored SOUL"})
    store.put_row("coding", "hermes:runtime-config", "compression", "local", {"value": {"enabled": "kept"}})
    return store


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #


def test_import_moves_everything_into_the_database_and_backs_up_files(tmp_path):
    state, home = legacy_workspace(tmp_path)
    store = seeded_store(state)
    before_home = tree_hash(home)
    originals = {p: p.read_bytes() for p in state.rglob("*") if p.is_file() and "domain.sqlite3" not in p.name}
    report = project_file_import.run_import(database=store.db, state_root=state, hermes_home=home)

    assert report.status == "imported" and report.imported
    assert tree_hash(home) == before_home  # native home untouched
    nodes = store.nodes()
    assert nodes["design"]["parent"] == "coding" and nodes["design"]["display_name"] == "Design"
    assert nodes["design"]["description"] == "The design project"
    assert nodes["coding"]["display_name"] == "Coding"   # file values win on the first import
    assert nodes["sub"]["parent"] == "coding" and nodes["sub"]["killed"]
    assert nodes["sketch"]["draft"] and nodes["sketch"]["parent"] is None
    # A native directory alone is not a Project; a cycle in hierarchy.json is cut.
    assert "stray" not in nodes
    assert nodes["loop-a"]["parent"] is None and nodes["loop-b"]["parent"] == "loop-a"
    assert any("cycle" in w for w in report.warnings)
    assert store.pinned() == ["design", "coding"]
    assert store.nodes()["default"]["display_name"] == "X"
    # No Agent binding is created by the import: binding is a connection-flow decision.
    assert store.db.query_one("SELECT 1 FROM agent_bindings") is None
    # Persona: only Kaus-owned directories, managed blocks stripped; native SOUL.md rows dropped.
    rows = {(r["capability_type"], r["capability_id"]): r for r in store.rows("design")}
    assert rows[("instructions", "project-persona")]["config"] == {"body": "design persona"}
    assert ("instructions", "project-persona") not in {(r["capability_type"], r["capability_id"]) for r in store.rows("coding")}
    assert report.dropped_personas == ["coding"]
    # Rule layers become rows and read back exactly (native constitution.md read once, read-only).
    layout = store.node("design")["metadata"][project_instructions.INSTRUCTION_BLOCKS_KEY]
    assert project_instructions.rows_to_text("design", store.rows("design", "instructions"), layout) == (
        "design text\n### [override:tone] Tone\nbe long\n### [disable:old]\n")
    assert rows[("instructions", "rule-old")]["assignment_mode"] == "block"
    assert project_instructions.rows_to_text("default", store.rows("default", "instructions"), None) == (
        "# root rules\n### [rule:tone] Tone\nbe brief\n")
    assert project_instructions.rows_to_text("coding", store.rows("coding", "instructions"), None) == "coding layer\n"
    # Blocks and own config of Kaus directories: only missing rows are added, existing values kept,
    # credentials redacted. Native config.yaml is the Agent's own and is not imported.
    assert rows[("mcp", "mcp_servers")]["assignment_mode"] == "local"
    assert rows[("delegation", "delegation")]["assignment_mode"] == "block"
    assert rows[("hermes:delegation-extras", "delegation")]["assignment_mode"] == "block"
    assert "sk-testonly" not in json.dumps(rows[("hermes:runtime-config", "providers")]["config"])
    coding_rows = {(r["capability_type"], r["capability_id"]): r for r in store.rows("coding")}
    assert coding_rows[("hermes:runtime-config", "compression")]["config"] == {"value": {"enabled": "kept"}}
    assert ("hermes:default-model", "model") not in {(r["capability_type"], r["capability_id"]) for r in store.rows("default")}
    # Skill files are only counted.
    assert report.skill_files == 2 and not any(r["capability_type"] == "skills" and r["assignment_mode"] == "local" for r in store.rows("design"))
    # Archives.
    archived = store.latest_archive("old")
    assert archived["record"]["label"] == "Old" and archived["batch_id"] == "b1"
    assert store.node("old")["status"] == "disabled"
    assert any(r["capability_id"] == "project-persona" and r["config"] == {"body": "old persona"} for r in archived["snapshot"]["clearedRows"])
    # Legacy files moved (never deleted), the database backed up, the marker written.
    legacy = [p for p in (state / "state").iterdir() if p.name.startswith("legacy-project-files-")]
    assert len(legacy) == 1
    for path, content in originals.items():
        assert not path.exists()
        relative = path.relative_to(state)
        moved = legacy[0] / (relative.relative_to("state") if relative.parts[0] == "state" else relative)
        assert moved.read_bytes() == content
    assert report.backup and Path(report.backup).is_file() and Path(report.backup).parent == paired_db(state).parent
    marker = project_file_import.marker(store.db)
    assert marker["status"] == "imported" and marker["legacyDir"] == str(legacy[0])


def test_import_is_idempotent_and_later_files_are_only_moved(tmp_path):
    state, home = legacy_workspace(tmp_path)
    store = seeded_store(state)
    project_file_import.run_import(database=store.db, state_root=state, hermes_home=home)
    revision = store.revision()
    again = project_file_import.run_import(database=store.db, state_root=state, hermes_home=home)
    assert again.status == "already_imported" and not again.imported and again.moved == []
    assert store.revision() == revision
    write_json(state / "labels.json", {"design": "Changed later"})
    third = project_file_import.run_import(database=store.db, state_root=state, hermes_home=home)
    assert third.status == "already_imported" and third.moved == [str(state / "labels.json")]
    assert store.nodes()["design"]["display_name"] == "Design"
    assert not (state / "labels.json").exists()


def test_import_only_runs_on_the_paired_database(tmp_path):
    state, home = legacy_workspace(tmp_path)
    other = SqliteDatabase(tmp_path / "elsewhere.sqlite3")
    messages: list[str] = []
    report = project_file_import.auto_import(database=other, db_path=tmp_path / "elsewhere.sqlite3",
                                             state_root=state, hermes_home=home, log=messages.append)
    assert report.status == "skipped:not_paired"
    assert project_file_import.marker(other)["status"] == "skipped:not_paired"
    assert (state / "hierarchy.json").is_file() and messages
    paired = SqliteDatabase(paired_db(state))
    assert project_file_import.auto_import(database=paired, db_path=paired_db(state), state_root=state,
                                           hermes_home=home).imported


def test_manual_import_script_imports_an_unpaired_database(tmp_path):
    import subprocess
    state, home = legacy_workspace(tmp_path)
    db = tmp_path / "elsewhere.sqlite3"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_project_files.py"), "--db", str(db),
         "--state-root", str(state), "--hermes-home", str(home)],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120)
    assert result.returncode == 0, result.stderr
    assert "status=imported" in result.stdout
    store = ProjectTreeStore(SqliteDatabase(db))
    assert store.nodes()["design"]["parent"] == "coding"
    assert project_file_import.marker(store.db)["status"] == "imported"
    assert not (state / "hierarchy.json").exists()


def test_nothing_to_import_writes_only_the_marker(tmp_path):
    state = tmp_path / "fresh"
    state.mkdir()
    store = ProjectTreeStore(SqliteDatabase(paired_db(state)))
    store.ensure_root("X")
    report = project_file_import.run_import(database=store.db, state_root=state, hermes_home=None)
    assert report.status == "nothing_to_import" and report.backup is None and report.moved == []
    assert project_file_import.marker(store.db)["status"] == "imported"
    assert not any(p.name.startswith("legacy-project-files-") for p in (state / "state").iterdir())
    assert store.db.query_one("SELECT 1 FROM agent_bindings") is None


# --------------------------------------------------------------------------- #
# server endpoints write only to the database
# --------------------------------------------------------------------------- #


def bind_server(monkeypatch, db_path: Path):
    """Import ``server`` with both feature flags off and point its tree store at ``db_path``."""
    monkeypatch.setenv("DASH_FEATURE_SESSION_HOST_V1", "0")
    monkeypatch.setenv("DASH_FEATURE_PROJECT_DOMAIN_V1", "0")
    import server
    monkeypatch.setattr(server, "_DOMAIN_RUNTIME", None)
    monkeypatch.setattr(server, "_SESSION_RUNTIME", None)
    monkeypatch.setattr(server, "TREE_DB_PATH", db_path)
    monkeypatch.setattr(server, "_TREE_STORE", {"key": None, "store": None, "db": None})
    return server


@pytest.fixture()
def bound(tmp_path, monkeypatch):
    state = tmp_path / "dashboard"
    state.mkdir()
    home = tmp_path / "native"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    server = bind_server(monkeypatch, tmp_path / "db" / "domain.sqlite3")
    yield server, state, home


def test_create_endpoint_writes_only_the_database(bound):
    server, state, home = bound
    result = server.api_create_empty(server.CreateEmptyReq(name="design", display="Design", description="d"))
    assert result["ok"] and result["is_draft"]
    assert server._tree_store().exists("design") and not (home / "profiles" / "design").exists()
    node = server.build_tree()["drafts"][0]
    assert node["name"] == "design" and node["label"] == "Design"
    assert set(node) == {"name", "label", "is_pinned", "parent", "killed", "effective_killed", "is_draft", "children"}
    assert not list(state.rglob("*")) and not list(home.rglob("*"))


def test_tree_edits_with_a_custom_db_write_nothing_into_the_code_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/nonexistent/kaus-tests")
    monkeypatch.setenv("DASH_DOMAIN_DB", str(tmp_path / "custom" / "domain.sqlite3"))
    server = bind_server(monkeypatch, None)
    here = Path(server.HERE)

    def listing() -> set[str]:
        names = {p.name for p in here.iterdir()}
        if (here / "state").is_dir():
            names |= {"state/" + p.name for p in (here / "state").iterdir()}
        return names

    before = listing()
    server.api_create_empty(server.CreateEmptyReq(name="iso-parent", display="P"))
    server.api_create_empty(server.CreateEmptyReq(name="iso-design", display="D"))
    server.api_move(server.MoveReq(node="iso-parent", new_parent="default"))
    server.api_move(server.MoveReq(node="iso-design", new_parent="iso-parent"))
    server.api_pin("iso-design", server.PinReq(pinned=True))
    server.api_rename("iso-design", server.RenameReq(display="D2"))
    server.api_constitution_save_node("iso-design", server.ConstLayerReq(text="### [rule:a] A\nbody\n"))
    server.api_fork("iso-design", server.ForkReq(new_name="iso-copy", new_display="Copy"))
    preview = server.api_reparent_preview(server.ReparentPreviewReq(node="iso-design", new_parent="default"))
    assert preview["from"]["parent"] == "iso-parent" and preview["to"]["ancestors"] == ["default"]
    assert server.api_delete_agent("iso-parent")["cascade"] == ["iso-design", "iso-copy"]
    assert server.api_restore_deleted_agent("iso-design")["chain"] == ["iso-parent"]
    assert listing() == before
    assert (tmp_path / "custom" / "domain.sqlite3").is_file()
    assert server._tree_store().nodes()["iso-design"]["parent"] == "iso-parent"


def test_rule_layer_round_trips_and_resolves_along_the_tree(bound):
    server, state, home = bound
    for name, parent in (("team", "default"), ("design", "team")):
        server.api_create_empty(server.CreateEmptyReq(name=name))
        server.api_move(server.MoveReq(node=name, new_parent=parent))
    root_text = "root free text\n\n### [rule:tone] Tone\nbe brief\n### [rule:scope] Scope\nstay small\n"
    team_text = "\n### [disable:scope]\n### [override:tone] Tone\n\nbe long\n"
    assert server.api_constitution_save_node("default", server.ConstLayerReq(text=root_text))["resynced"] == 3
    assert server.api_constitution_save_node("team", server.ConstLayerReq(text=team_text))["resynced"] == 2
    assert server.api_constitution_node("default")["own"] == root_text
    assert server.api_constitution_node("team")["own"] == team_text
    design = server.api_constitution_node("design")
    assert design["own"] == ""
    rules = {r["id"]: r for r in design["rules"]}
    assert rules["tone"]["op"] == "override" and rules["tone"]["node"] == "team"
    assert rules["scope"]["disabled"] is True
    assert "be long" in design["effective"] and "stay small" not in design["effective"] and "root free text" in design["effective"]
    assert design["governed"] is True
    overview = {n["name"]: n for n in server.api_constitution_overview()["nodes"]}
    assert overview["design"]["governed"] and not overview["design"]["has_layer"] and overview["team"]["has_layer"]
    # The same content is what the generic resolver sees (block rows block, overrides replace).
    from app.capabilities.effective import resolve_effective_for_project
    effective = asyncio.run(resolve_effective_for_project(build_repository_set(server._tree_store().db), "project:design"))
    entries = {(e.capability_type, e.capability_id): e for e in effective.entries}
    assert entries[("instructions", "rule-tone")].source_project_id == "project:team"
    assert ("instructions", "rule-scope") not in entries
    assert ("instructions", "constitution-default") in entries
    # Saving an empty layer removes the rows and the overrides disappear.
    server.api_constitution_save_node("team", server.ConstLayerReq(text=""))
    assert server.api_constitution_node("team")["own"] == ""
    assert "stay small" in server.api_constitution_node("design")["effective"]
    # Nothing was written to any directory: delivery is the drivers' job.
    assert not list(home.rglob("*")) and not list(state.rglob("*"))


def test_fork_copies_instruction_rows_and_safe_config_but_no_credentials(bound):
    server, state, home = bound
    server.api_create_empty(server.CreateEmptyReq(name="design", display="Design"))
    server.api_move(server.MoveReq(node="design", new_parent="default"))
    store = server._tree_store()
    store.put_row("design", "instructions", "project-persona", "local", {"body": "persona"})
    store.put_row("design", "mcp", "mcp_servers", "local", {"value": {"fs": {"command": "x"}}})
    store.put_row("design", "hermes:runtime-config", "providers", "local", {"value": {"p": {"api_key": "ref"}}})
    store.put_row("design", "hermes:default-model", "model", "local", {"value": {"default": "m"}})
    server.api_constitution_save_node("design", server.ConstLayerReq(text="own text\n### [disable:x]\n"))
    result = server.api_fork("design", server.ForkReq(new_name="copy", new_display="Copy"))
    assert result["parent"] == "default" and result["label"] == "Copy"
    keys = {(r["capability_type"], r["capability_id"]) for r in store.rows("copy")}
    assert keys == {("instructions", "project-persona"), ("instructions", "constitution-copy"),
                    ("instructions", "rule-x"), ("mcp", "mcp_servers")}
    assert server.api_constitution_node("copy")["own"] == "own text\n### [disable:x]\n"
    assert not (home / "profiles" / "copy").exists()


def test_recreating_a_deleted_slug_starts_clean_and_keeps_the_old_rows_in_its_archive(bound):
    server, state, home = bound
    server.api_create_empty(server.CreateEmptyReq(name="design"))
    server.api_move(server.MoveReq(node="design", new_parent="default"))
    store = server._tree_store()
    store.put_row("design", "instructions", "project-persona", "local", {"body": "first life"})
    server.api_delete_agent("design")
    server.api_create_empty(server.CreateEmptyReq(name="design", display="Second"))
    assert store.rows("design") == [] and server._label("design") == "Second"
    assert store.latest_archive("design")["snapshot"]["clearedRows"][0]["config"] == {"body": "first life"}
    assert json.loads(server.api_restore_deleted_agent("design").body)["error"]["code"] == "project_exists"


def test_a_native_directory_is_not_a_project(bound):
    """A directory under the native home of some Agent neither shows up in the tree nor can be
    moved; Projects exist only in the database."""
    server, state, home = bound
    from fastapi import HTTPException
    (home / "profiles" / "coding").mkdir(parents=True)
    (home / "profiles" / "coding" / "SOUL.md").write_text("native persona\n")
    before = tree_hash(home)
    assert "coding" not in server.build_tree()["nodes"] and not server._project_exists("coding")
    with pytest.raises(HTTPException) as refused:
        server.api_move(server.MoveReq(node="coding", new_parent="default"))
    assert refused.value.status_code == 404
    with pytest.raises(HTTPException):
        server.api_fork("coding", server.ForkReq(new_name="copy"))
    assert tree_hash(home) == before


def test_events_follow_the_tree_revision(bound):
    server, state, home = bound
    first = server._dashboard_state_signature()
    assert set(first) == {"project_tree"}
    server.api_create_empty(server.CreateEmptyReq(name="design"))
    second = server._dashboard_state_signature()
    assert first != second
    event = server._dashboard_event_from_signatures(first, second)
    assert event["type"] == "state_changed" and event["scopes"] == ["config", "network", "profiles"]
    assert server._dashboard_event_from_signatures(None, second)["scopes"] == ["network", "profiles", "config"]
