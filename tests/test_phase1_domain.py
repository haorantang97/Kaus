"""Phase 1「换地基（界面不动）」的测试。

覆盖：

1. **导入幂等**：同一份 fixture 连跑两次 `--apply`，第二次一次写都不发；
   改一处旧状态后重跑，只更新受影响的行且 `created_at` 保持；
2. **对账无差异**：导入后 `GET /api/_domain/diff` 报 in-sync；
   手改旧状态或直接改库 → 差异被逐字段列出来；
3. **端点形状**：`/api/projects`、`/api/projects/{id}`、`/api/projects/{id}/bindings`、
   `/api/backends`、`/api/bindings/{id}` 的返回结构与 R-09（多 Binding）语义；
4. **flag 关闭时端点不存在**：`attach_domain_api` 返回 None，路由表零新增，
   且不创建数据库文件。

隔离：全部用 `tmp_path` 造的 fixture 目录（假 HERMES_HOME + 状态 JSON），
不碰用户真实 `~/.hermes`，不导入 `server.py`。

运行：``python3 -m pytest tests/test_phase1_domain.py -q``
（需 fastapi / httpx / pydantic）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import domain_apply  # noqa: E402
import domain_bootstrap  # noqa: E402


# --------------------------------------------------------------------------- #
# fixture：假的 HERMES_HOME + 旧状态 JSON
# --------------------------------------------------------------------------- #


HIERARCHY = {
    "coding": "default",
    "dev": "coding",
    "pronto": "dev",
    "media": "default",
    "architect": "default",
}
PROFILES = ["coding", "dev", "pronto", "media"]
LABELS = {"default": "X", "coding": "编码", "pronto": "Pronto"}


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """返回 ``(状态目录, 假 HERMES_HOME)``。architect 做成 twin。"""
    home = tmp_path / "hermes-home"
    (home / "memories").mkdir(parents=True)
    (home / "profiles").mkdir()
    for name in PROFILES:
        (home / "profiles" / name).mkdir()
    twin = home / "profiles" / "architect"
    twin.mkdir()
    (twin / "memories").symlink_to(home / "memories")
    (twin / "SOUL.md").write_text("【配置模式】分身 高权限", encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir()
    write_state(state, pinned=["coding"], killed=["media"], drafts=[])
    return state, home


def write_state(
    state: Path,
    *,
    hierarchy: dict | None = None,
    labels: dict | None = None,
    pinned: list | None = None,
    killed: list | None = None,
    drafts: list | None = None,
) -> None:
    payload = {
        "hierarchy.json": HIERARCHY if hierarchy is None else hierarchy,
        "labels.json": LABELS if labels is None else labels,
        "pinned.json": pinned or [],
        "killed.json": killed or [],
        "drafts.json": drafts or [],
        "skill_inherit_off.json": [],
    }
    for name, value in payload.items():
        (state / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def open_runtime(state: Path, home: Path, db: Path, **kwargs):
    return domain_bootstrap.open_domain_runtime(
        state_root=state, hermes_home=home, db_path=db, **kwargs
    )


def plan_for(state: Path, home: Path) -> dict:
    return domain_bootstrap.build_plan(state_root=state, hermes_home=home)


def apply_twice(state: Path, home: Path, db: Path):
    from app.persistence.sqlite import SqliteUnitOfWork

    results = []
    for _ in range(2):
        with SqliteUnitOfWork(db) as uow:
            results.append(
                asyncio.run(domain_apply.apply_plan(uow.repositories, plan_for(state, home)))
            )
    return results


# --------------------------------------------------------------------------- #
# 1. 导入与幂等
# --------------------------------------------------------------------------- #


def test_import_creates_projects_bindings_and_backend(tmp_path):
    state, home = make_workspace(tmp_path)
    first, _ = apply_twice(state, home, tmp_path / "domain.sqlite3")

    assert first.backend_written is True
    # 5 个非 twin 节点（default + 4 个 profile 目录）；architect 是 twin，不成 Project。
    assert len(first.projects_created) == 5
    assert "project:architect" not in first.projects_created
    # 每个 Project 一条默认 Binding + twin 的额外 Binding。
    assert len(first.bindings_created) == 6
    assert "binding:default:hermes:architect" in first.bindings_created


def test_second_run_writes_nothing(tmp_path):
    state, home = make_workspace(tmp_path)
    _, second = apply_twice(state, home, tmp_path / "domain.sqlite3")

    assert second.wrote_anything is False
    assert second.projects_created == [] and second.projects_updated == []
    assert second.bindings_created == [] and second.bindings_updated == []
    assert len(second.projects_unchanged) == 5
    assert len(second.bindings_unchanged) == 6


def test_changed_label_updates_only_that_row_and_keeps_created_at(tmp_path):
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_twice(state, home, db)

    with SqliteUnitOfWork(db) as uow:
        before = asyncio.run(uow.repositories.projects.get("project:coding"))

    write_state(state, labels={**LABELS, "coding": "编码组"}, pinned=["coding"], killed=["media"])
    with SqliteUnitOfWork(db) as uow:
        result = asyncio.run(
            domain_apply.apply_plan(uow.repositories, plan_for(state, home))
        )
        after = asyncio.run(uow.repositories.projects.get("project:coding"))

    assert result.projects_updated == ["project:coding"]
    assert result.projects_created == []
    assert after.display_name == "编码组"
    # R-05：slug 不变；created_at 是「这条记录何时进库」，更新不得重置它。
    assert after.slug == before.slug == "coding"
    assert after.created_at == before.created_at
    assert after.updated_at > before.updated_at


def test_ui_state_and_twin_mode_land_where_the_rulings_say(tmp_path):
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_twice(state, home, db)

    with SqliteUnitOfWork(db) as uow:
        coding = asyncio.run(uow.repositories.projects.get("project:coding"))
        media = asyncio.run(uow.repositories.projects.get("project:media"))
        twin = asyncio.run(
            uow.repositories.bindings.get("binding:default:hermes:architect")
        )
        default_binding = asyncio.run(
            uow.repositories.bindings.get("binding:default:hermes")
        )

    # §11.1：pinned / killed / drafts 落 metadata_json。
    assert coding.metadata["ui_state"] == {"pinned": True, "killed": False, "draft": False}
    assert media.metadata["ui_state"]["killed"] is True
    # AD-03：twin_mode 落 runtime_config_json；AD-02：字段名是 native_scope_ref。
    assert twin.runtime_config == {"twin_mode": "配置模式 · 高权限"}
    assert twin.native_scope_ref == "architect"
    assert twin.is_default is False
    # AD-01：twin 才有第四段判别符，常态 Binding 保持三段。
    assert twin.discriminator == "architect"
    assert default_binding.discriminator is None
    assert default_binding.runtime_config == {}


def test_no_sessions_are_imported(tmp_path):
    """R-03：Phase 1 不导入任何存量会话。"""
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_twice(state, home, db)
    with SqliteUnitOfWork(db) as uow:
        rows = uow.database.query_all("SELECT COUNT(*) AS n FROM conversations")
    assert rows[0]["n"] == 0


def test_apply_rejects_a_plan_carrying_sessions(tmp_path):
    from app.persistence.memory import in_memory_repository_set

    plan = {"schema_version": domain_apply.SUPPORTED_PLAN_SCHEMA, "conversations": []}
    with pytest.raises(ValueError, match="R-03"):
        asyncio.run(domain_apply.apply_plan(in_memory_repository_set(), plan))


def test_apply_rejects_unknown_schema():
    from app.persistence.memory import in_memory_repository_set

    with pytest.raises(ValueError, match="schema"):
        asyncio.run(
            domain_apply.apply_plan(in_memory_repository_set(), {"schema_version": "nope"})
        )


# --------------------------------------------------------------------------- #
# 2. 端点形状
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path):
    state, home = make_workspace(tmp_path)
    runtime = open_runtime(state, home, tmp_path / "domain.sqlite3")
    application = fastapi.FastAPI()
    application.include_router(runtime.router)
    with TestClient(application) as test_client:
        test_client.state_root = state  # 供个别用例改旧状态
        test_client.home = home
        yield test_client
    runtime.close()


def test_list_projects_shape(client):
    body = client.get("/api/projects").json()
    assert body["count"] == 5
    assert body["roots"] == ["project:default"]
    by_id = {p["id"]: p for p in body["projects"]}
    assert by_id["project:coding"]["parentProjectId"] == "project:default"
    # wire 是驼峰（与 v1.0 §4.1 的 TS 接口一致），且不含蛇形残留。
    assert "displayName" in by_id["project:default"]
    assert "display_name" not in by_id["project:default"]
    assert by_id["project:default"]["displayName"] == "X"
    assert "project:architect" not in by_id


def test_get_project_accepts_bare_slug_and_lists_children(client):
    full = client.get("/api/projects/project:default").json()
    bare = client.get("/api/projects/default").json()
    assert full == bare
    assert set(full["childProjectIds"]) == {"project:coding", "project:media"}


def test_get_project_404(client):
    response = client.get("/api/projects/nope")
    assert response.status_code == 404


def test_project_bindings_include_the_twin_binding(client):
    body = client.get("/api/projects/default/bindings").json()
    ids = [b["id"] for b in body["bindings"]]
    # R-09：同一 Project 同一 Backend 两条 Binding。
    assert ids == ["binding:default:hermes", "binding:default:hermes:architect"]
    assert body["count"] == 2
    twin = body["bindings"][1]
    assert twin["nativeScopeRef"] == "architect"
    assert twin["runtimeConfig"] == {"twin_mode": "配置模式 · 高权限"}
    assert twin["discriminator"] == "architect"


def test_backends_endpoint(client):
    body = client.get("/api/backends").json()
    assert body["count"] == 1
    backend = body["backends"][0]
    assert backend["id"] == "backend:hermes"
    assert backend["driverKind"] == "native"


def test_get_backend_endpoint_outputs_two_layer_capabilities(client):
    """AD-71：``GET /api/backends/{id}`` 的能力分两层——``ui`` 只有取值，``detail`` 才带 note/verification。"""
    full = client.get("/api/backends/backend:hermes").json()
    bare = client.get("/api/backends/hermes").json()
    assert full == bare
    assert full["id"] == "backend:hermes"

    capabilities = full["capabilities"]
    assert set(capabilities) >= {"ui", "detail", "unknownCount"}
    assert capabilities["unknownCount"] == full["unknownCount"]

    def leaves(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from leaves(value)
        else:
            yield node

    # ui 层：叶子全是字符串取值，结构上带不了 note。
    for leaf in leaves(capabilities["ui"]):
        assert isinstance(leaf, str) and leaf
    # detail 层：每个叶子是 {value, status, verification, note?}。
    def detail_leaves(node):
        if isinstance(node, dict) and "value" in node and "status" in node:
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from detail_leaves(value)
    seen = list(detail_leaves(capabilities["detail"]))
    assert seen
    for state in seen:
        assert state["status"] in ("supported", "unsupported", "unknown")
        assert state["verification"] in ("declared", "bench", "live")


def test_get_backend_endpoint_404(client):
    assert client.get("/api/backends/nope").status_code == 404


def test_get_binding_and_404(client):
    body = client.get("/api/bindings/binding:coding:hermes").json()
    assert body["projectId"] == "project:coding"
    assert body["isDefault"] is True
    assert client.get("/api/bindings/binding:nope:hermes").status_code == 404


# --------------------------------------------------------------------------- #
# 3. 对账
# --------------------------------------------------------------------------- #


def test_diff_is_empty_right_after_import(client):
    body = client.get("/api/_domain/diff").json()
    assert body["inSync"] is True
    assert body["counts"]["expected"] == body["counts"]["actual"]
    assert body["projects"]["missingInDb"] == []
    assert body["projects"]["unexpectedInDb"] == []
    assert body["projects"]["changed"] == []
    assert body["bindings"]["inSync"] is True


def test_diff_reports_a_renamed_node(client):
    write_state(
        client.state_root,
        labels={**LABELS, "coding": "改过的名字"},
        pinned=["coding"],
        killed=["media"],
    )
    body = client.get("/api/_domain/diff").json()
    assert body["inSync"] is False
    changed = {entry["id"]: entry["fields"] for entry in body["projects"]["changed"]}
    assert changed["project:coding"]["display_name"] == {
        "expected": "改过的名字",
        "actual": "编码",
    }


def test_diff_reports_a_new_node_as_missing_in_db(client):
    (client.home / "profiles" / "newbie").mkdir()
    body = client.get("/api/_domain/diff").json()
    assert body["projects"]["missingInDb"] == ["project:newbie"]
    assert body["bindings"]["missingInDb"] == ["binding:newbie:hermes"]
    assert body["inSync"] is False


def test_diff_reports_a_removed_node_as_unexpected_in_db(client):
    import shutil

    shutil.rmtree(client.home / "profiles" / "media")
    body = client.get("/api/_domain/diff").json()
    assert "project:media" in body["projects"]["unexpectedInDb"]
    assert "binding:media:hermes" in body["bindings"]["unexpectedInDb"]


def test_diff_reports_ui_state_drift(client):
    write_state(client.state_root, pinned=[], killed=["media"])
    body = client.get("/api/_domain/diff").json()
    changed = {entry["id"]: entry["fields"] for entry in body["projects"]["changed"]}
    assert changed["project:coding"]["metadata"]["expected"]["ui_state"]["pinned"] is False
    assert changed["project:coding"]["metadata"]["actual"]["ui_state"]["pinned"] is True


def test_diff_without_a_source_is_503_not_a_fake_in_sync(tmp_path):
    from app.api.router import build_domain_router
    from app.persistence.memory import in_memory_repository_set

    application = fastapi.FastAPI()
    application.include_router(build_domain_router(in_memory_repository_set()))
    with TestClient(application) as test_client:
        assert test_client.get("/api/_domain/diff").status_code == 503


def test_rerunning_apply_reconciles_the_diff(client):
    write_state(
        client.state_root,
        labels={**LABELS, "coding": "改过的名字"},
        pinned=["coding"],
        killed=["media"],
    )
    assert client.get("/api/_domain/diff").json()["inSync"] is False

    from app.persistence.sqlite import SqliteUnitOfWork

    db = Path(client.state_root).parent / "domain.sqlite3"
    with SqliteUnitOfWork(db) as uow:
        asyncio.run(
            domain_apply.apply_plan(
                uow.repositories, plan_for(client.state_root, client.home)
            )
        )
    assert client.get("/api/_domain/diff").json()["inSync"] is True


# --------------------------------------------------------------------------- #
# 4. feature flag
# --------------------------------------------------------------------------- #


def test_flag_defaults_to_off():
    assert domain_bootstrap.feature_enabled({}, env={}) is False
    assert domain_bootstrap.feature_enabled(None, env={}) is False


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("on", True),
                                            ("0", False), ("false", False), ("", False)])
def test_env_var_controls_the_flag(value, expected):
    assert domain_bootstrap.feature_enabled({}, env={domain_bootstrap.FEATURE_ENV_VAR: value}) is expected


def test_config_file_controls_the_flag():
    nested = {"features": {"project_domain_v1": True}}
    flat = {"project_domain_v1": True}
    assert domain_bootstrap.feature_enabled(nested, env={}) is True
    assert domain_bootstrap.feature_enabled(flat, env={}) is True
    assert domain_bootstrap.feature_enabled({"features": {}}, env={}) is False


def test_env_var_overrides_the_config_file():
    config = {"features": {"project_domain_v1": True}}
    env = {domain_bootstrap.FEATURE_ENV_VAR: "0"}
    assert domain_bootstrap.feature_enabled(config, env=env) is False


def test_attach_is_a_noop_and_endpoints_absent_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv(domain_bootstrap.FEATURE_ENV_VAR, raising=False)
    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"

    application = fastapi.FastAPI()
    routes_before = {r.path for r in application.routes}
    runtime = domain_bootstrap.attach_domain_api(
        application,
        state_root=state,
        hermes_home=home,
        db_path=db,
        config_loader=lambda: {},
    )
    assert runtime is None
    assert {r.path for r in application.routes} == routes_before
    # 关掉 flag 连库文件都不该出现（回滚 = 关 flag + 删库文件）。
    assert not db.exists()

    with TestClient(application) as test_client:
        for path in (
            "/api/projects",
            "/api/backends",
            "/api/bindings/binding:default:hermes",
            "/api/_domain/diff",
        ):
            assert test_client.get(path).status_code == 404


def test_attach_mounts_everything_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"

    application = fastapi.FastAPI()
    runtime = domain_bootstrap.attach_domain_api(
        application,
        state_root=state,
        hermes_home=home,
        db_path=db,
        config_loader=lambda: {},
        log=lambda _message: None,
    )
    assert runtime is not None
    assert runtime.imported_on_start is True
    assert db.exists()
    try:
        with TestClient(application) as test_client:
            assert test_client.get("/api/projects").json()["count"] == 5
            assert test_client.get("/api/_domain/diff").json()["inSync"] is True
    finally:
        runtime.close()


def test_auto_import_only_happens_on_an_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    state, home = make_workspace(tmp_path)
    db = tmp_path / "domain.sqlite3"

    first = open_runtime(state, home, db)
    assert first.imported_on_start is True
    first.close()

    # 库非空 → 第二次启动不自动重跑；线上状态的收敛必须是显式操作。
    write_state(state, labels={**LABELS, "coding": "别的名字"}, pinned=["coding"], killed=["media"])
    second = open_runtime(state, home, db)
    try:
        assert second.imported_on_start is False
        diff = asyncio.run(_diff_via_router(second))
        assert diff["inSync"] is False
    finally:
        second.close()


async def _diff_via_router(runtime) -> dict:
    from app.api.views import build_domain_diff

    plan = None
    for route in runtime.router.routes:
        if route.name == "domain_diff":
            plan = route.endpoint
    assert plan is not None
    return await plan()


def test_db_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(domain_bootstrap.DB_PATH_ENV_VAR, raising=False)
    assert domain_bootstrap.resolve_db_path(dashboard_dir=tmp_path, env={}) == (
        tmp_path / "state" / "domain.sqlite3"
    )
    assert domain_bootstrap.resolve_db_path(
        dashboard_dir=tmp_path, env={domain_bootstrap.DB_PATH_ENV_VAR: "other.db"}
    ) == (tmp_path / "other.db")
    absolute = tmp_path / "abs.db"
    assert domain_bootstrap.resolve_db_path(
        dashboard_dir=tmp_path, env={domain_bootstrap.DB_PATH_ENV_VAR: str(absolute)}
    ) == absolute


def test_attach_survives_a_broken_setup(tmp_path, monkeypatch):
    """领域库是旁路设施：装配失败不得让仪表盘起不来。"""
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    monkeypatch.setattr(
        domain_bootstrap,
        "open_domain_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    application = fastapi.FastAPI()
    messages: list[str] = []
    runtime = domain_bootstrap.attach_domain_api(
        application,
        state_root=tmp_path,
        hermes_home=tmp_path,
        config_loader=lambda: {},
        log=messages.append,
    )
    assert runtime is None
    assert messages and "boom" in messages[0]


# --------------------------------------------------------------------------- #
# 5. 公共层纯净性（本分支新增的 app/api/ 同样受 N §3 约束）
# --------------------------------------------------------------------------- #


def test_new_public_api_module_has_no_backend_private_tokens():
    import re

    forbidden = ("hermes", "profile", "codex", "claude", "xterm")
    for path in sorted((KERNEL_ROOT / "app" / "api").rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in lowered, f"{path.name} 含 Backend 私有名词 {token!r}"
        assert not re.search(r"(?<![a-z])pty(?![a-z])", lowered)
