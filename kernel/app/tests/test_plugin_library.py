"""能力库插件（设计稿第 6 / 7 节）：安装、目录项、分配、卸载与接口。全部在 ``tmp_path`` 里，不联网。"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.api.api_errors import ApiError
from app.capabilities.effective import resolve_effective_for_binding, resolve_effective_for_project
from app.capabilities.plugin_library import PluginLibrary
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.tests.test_plugin_packages import claude_plugin, codex_plugin, gemini_extension


@pytest.fixture
def world(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "state" / "domain.sqlite3")
    for slug, parent in (("default", None), ("work", "default"), ("leaf", "work")):
        asyncio.run(uow.repositories.projects.save(Project.create(
            slug=slug, display_name=slug, parent_project_id=f"project:{parent}" if parent else None)))
    asyncio.run(uow.repositories.backends.save(Backend.create(key="alpha", display_name="Alpha", driver_kind="acp")))
    tree = SkillTreeService(uow.database)
    library = PluginLibrary(tmp_path / "state" / "capability-library", skill_tree=tree,
                            allowed_git_schemes=("https", "ssh", "file"))
    tree.discover = library.discover
    yield library, tree, uow, tmp_path
    uow.close()


def _git_repo(path: Path) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "HOME": str(path.parent), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def test_local_install_copies_the_package_and_registers_a_plugin_kind(world):
    library, tree, uow, tmp_path = world
    source = claude_plugin(tmp_path / "src" / "review-kit", hooks=True)
    record = library.install(str(source))
    assert record["format"] == "claude" and record["name"] == "review-kit" and record["version"] == "1.2.0"
    assert record["source"] == {"type": "local", "location": str(source)}
    assert record["contents"] == {"skills": 2, "mcpServers": ["docs"], "hooks": True, "commands": False, "agents": False}
    assert record["capabilityType"] == "plugins" and record["capabilityId"] == record["id"]
    package = library.package_dir(record["id"])
    assert (package / ".claude-plugin/plugin.json").is_file() and (package / "skills/changelog/SKILL.md").is_file()
    assert json.loads((library.plugin_dir(record["id"]) / "record.json").read_text())["id"] == record["id"]
    assert library.list()["plugins"] == [record]
    view = tree.snapshot()
    item = next(s for s in view["skills"] if s["id"] == record["id"])
    assert item["kind"] == "plugin" and item["format"] == "claude" and item["sourcePath"] == str(package)
    assert not [a for a in view["assignments"] if a["skillId"] == record["id"]]  # not auto-assigned
    assert item["pendingAssignment"] is True and item["pluginId"] == record["id"]  # 挂在根下待分配，可以拖走
    # The source folder was only read.
    assert not (source / "record.json").exists()


def test_assignment_inherits_and_hydrates_only_at_the_binding_boundary(world):
    library, tree, uow, tmp_path = world
    record = library.install(str(gemini_extension(tmp_path / "src" / "gem")))
    view = tree.snapshot()
    assigned = tree.act({"revision": view["revision"], "action": "assign", "skillId": record["id"],
                         "targetProjectId": "project:work"})
    leaf = [e for e in assigned["entries"] if e["projectId"] == "project:leaf" and e["skillId"] == record["id"]]
    assert leaf and leaf[0]["inherited"] and leaf[0]["sourceProjectId"] == "project:work"
    assert next(x for x in assigned["skills"] if x["id"] == record["id"])["pendingAssignment"] is False
    # A plugin does not switch the Agent to the managed skill selection.
    assert tree.db.query_all("SELECT value_json FROM app_settings WHERE key='skill_tree.managed_projects.v1'") in ([], [("{}",)]) \
        or json.loads(tree.db.query_all("SELECT value_json FROM app_settings WHERE key='skill_tree.managed_projects.v1'")[0][0]) == {}
    project_view = asyncio.run(resolve_effective_for_project(uow.repositories, "project:leaf"))
    row = project_view.get("plugins", record["id"])
    assert row is not None and row.config == {}  # paths never persist in capability config
    project = asyncio.run(uow.repositories.projects.get("project:leaf"))
    binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend="alpha")))
    effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
    hydrated = effective.get("plugins", record["id"])
    assert hydrated.config["sourcePath"] == str(library.package_dir(record["id"]))
    assert hydrated.config["format"] == "gemini" and hydrated.config["name"] == "gem-ext"
    assert hydrated.config["libraryPath"] == str(library.plugin_dir(record["id"]))
    assert effective.get("skill-selection", "default") is None


def test_git_source_is_shallow_cloned_without_git_dir_and_records_the_commit(world):
    library, tree, uow, tmp_path = world
    repo = codex_plugin(tmp_path / "repo" / "codex-kit")
    commit = _git_repo(repo)
    url = repo.as_uri()
    inspected = library.inspect(url)
    assert inspected["kind"] == "plugin"
    assert inspected["plugin"]["source"] == {"type": "git", "location": url, "commit": commit}
    assert "id" not in inspected["plugin"] and "installedAt" not in inspected["plugin"]
    assert inspected["plugin"]["contents"]["skills"] == 1
    record = library.install(url)
    assert record["source"] == {"type": "git", "location": url, "commit": commit}
    assert not (library.package_dir(record["id"]) / ".git").exists()
    assert not any((library.root / ".staging").iterdir())
    with pytest.raises(ApiError) as caught:
        library.install(url)
    assert caught.value.code == "plugin_already_installed" and caught.value.extra["pluginId"] == record["id"]


def test_marketplace_lists_plugins_and_installs_one_by_name(world):
    library, tree, uow, tmp_path = world
    market = tmp_path / "market"
    claude_plugin(market / "plugins" / "one", name="one")
    gemini_extension(market / "plugins" / "two")
    (market / ".claude-plugin").mkdir()
    (market / ".claude-plugin/marketplace.json").write_text(json.dumps({"name": "acme", "plugins": [
        {"name": "one", "source": "./plugins/one", "description": "First"},
        {"name": "two", "source": "./plugins/two"},
        {"name": "gone", "source": "./plugins/missing"}]}))
    listed = library.inspect(str(market))
    assert listed == {"kind": "marketplace", "name": "acme", "plugins": [
        {"name": "one", "description": "First", "source": "./plugins/one"},
        {"name": "two", "description": "", "source": "./plugins/two"},
        {"name": "gone", "description": "", "source": "./plugins/missing"}]}
    with pytest.raises(ApiError) as caught:
        library.install(str(market))
    assert caught.value.code == "plugin_required"
    with pytest.raises(ApiError) as caught:
        library.install(str(market), "nope")
    assert caught.value.code == "plugin_not_in_marketplace"
    with pytest.raises(ApiError) as caught:
        library.install(str(market), "gone")
    assert caught.value.code == "source_not_found"
    record = library.install(str(market), "two")
    assert record["format"] == "gemini" and record["source"] == {"type": "local", "location": str(market), "plugin": "two"}
    commit = _git_repo(market)
    cloned = library.install(market.as_uri(), "one")
    assert cloned["format"] == "claude" and cloned["source"] == {"type": "git", "location": market.as_uri(),
                                                                 "commit": commit, "plugin": "one"}
    assert {p["name"] for p in library.list()["plugins"]} == {"gem-ext", "one"}


def test_source_errors_have_stable_codes(world):
    library, *_ = world
    for source, code in (("", "source_required"), ("relative/dir", "source_unsupported"),
                         ("http://example.test/repo.git", "source_unsupported"),
                         (str(Path("/nonexistent") / "kaus-plugin-test"), "source_not_found")):
        with pytest.raises(ApiError) as caught:
            library.inspect(source)
        assert caught.value.code == code, source
    plain = library.root.parent / "plain"
    plain.mkdir(parents=True)
    with pytest.raises(ApiError) as caught:
        library.inspect(str(plain))
    assert caught.value.code == "not_a_plugin" and caught.value.status_code == 422
    with pytest.raises(ApiError) as caught:
        library.install("file:///nonexistent/kaus-plugin-test.git")
    assert caught.value.code == "clone_failed"
    assert not (library.root / ".staging").exists() or not any((library.root / ".staging").iterdir())
    with pytest.raises(ApiError) as caught:
        library.uninstall("plugin-000000000000")
    assert caught.value.code == "plugin_not_found"


def test_uninstall_removes_catalogue_rows_order_and_files(world):
    library, tree, uow, tmp_path = world
    record = library.install(str(claude_plugin(tmp_path / "src" / "kit")))
    view = tree.snapshot()
    view = tree.act({"revision": view["revision"], "action": "assign", "skillId": record["id"], "targetProjectId": "project:default"})
    tree.act({"revision": view["revision"], "action": "block", "skillId": record["id"], "targetProjectId": "project:leaf"})
    assert uow.database.query_all("SELECT project_id FROM project_capabilities WHERE capability_type='plugins'")
    result = library.uninstall(record["id"])
    assert result == {"deleted": True, "id": record["id"]}
    assert not library.plugin_dir(record["id"]).exists()
    assert uow.database.query_all("SELECT project_id FROM project_capabilities WHERE capability_type='plugins'") == []
    after = tree.snapshot()
    assert record["id"] not in {s["id"] for s in after["skills"]}
    assert not [e for e in after["entries"] if e["skillId"] == record["id"]]
    order = json.loads(uow.database.query_all("SELECT value_json FROM app_settings WHERE key='skill_tree.order.v1'")[0][0])
    assert record["id"] not in {s for listed in order.values() for s in listed}


def test_router_requires_auth_and_returns_the_contract_shapes(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.plugin_library import build_plugin_library_router
    from app.api.session_auth import SessionAuthPolicy
    library, tree, uow, tmp_path = world
    source = claude_plugin(tmp_path / "src" / "kit", commands=True)
    app = FastAPI()
    app.include_router(build_plugin_library_router(library, auth_policy=SessionAuthPolicy(lambda: "lib-test")))
    with TestClient(app) as client:
        assert client.get("/api/library/plugins").status_code == 401
        headers = {"Authorization": "Bearer lib-test"}
        assert client.get("/api/library/plugins", headers=headers).json() == {"plugins": []}
        inspected = client.post("/api/library/plugins/inspect", headers=headers, json={"source": str(source)})
        assert inspected.status_code == 200 and inspected.json()["kind"] == "plugin"
        assert set(inspected.json()["plugin"]) == {"name", "version", "description", "format", "source", "contents", "capabilityType"}
        missing = client.post("/api/library/plugins/inspect", headers=headers, json={"source": str(tmp_path / "nope")})
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "source_not_found"
        created = client.post("/api/library/plugins", headers=headers, json={"source": str(source)})
        assert created.status_code == 201
        record = created.json()
        assert set(record) == {"id", "name", "version", "description", "format", "source", "contents", "installedAt",
                               "capabilityType", "capabilityId"}
        assert record["contents"]["commands"] is True
        assert client.get("/api/library/plugins", headers=headers).json() == {"plugins": [record]}
        duplicate = client.post("/api/library/plugins", headers=headers, json={"source": str(source)})
        assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "plugin_already_installed"
        extra = client.post("/api/library/plugins", headers=headers, json={"source": str(source), "plugin": "x", "bogus": 1})
        assert extra.status_code == 422
        deleted = client.delete(f"/api/library/plugins/{record['id']}", headers=headers)
        assert deleted.status_code == 200 and deleted.json() == {"deleted": True, "id": record["id"]}
        assert client.delete(f"/api/library/plugins/{record['id']}", headers=headers).status_code == 404
        assert client.get("/api/library/plugins", headers=headers).json() == {"plugins": []}
