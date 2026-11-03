"""Project 领域库接入层（`domain_bootstrap`）的测试。

覆盖：

1. **端点形状**：`/api/projects`、`/api/projects/{id}`、`/api/projects/{id}/bindings`、
   `/api/backends`、`/api/bindings/{id}` 的返回结构；
2. **对账端点**：领域库是唯一来源，`GET /api/_domain/diff` 回 503；
3. **flag 关闭时端点不存在**：`attach_domain_api` 返回 None，路由表零新增，
   且不创建数据库文件；flag 开时旧文件一次性导入并移走；
4. 公共层纯净性。

隔离：全部用 `tmp_path` 造的 fixture 目录（旧状态 JSON + 假的原生目录，只读），
不碰用户真实目录，不导入 `server.py`。

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

import domain_bootstrap  # noqa: E402


# --------------------------------------------------------------------------- #
# fixture：旧状态 JSON + 假的原生目录（只读）
# --------------------------------------------------------------------------- #


HIERARCHY = {
    "coding": "default",
    "dev": "coding",
    "pronto": "dev",
    "media": "default",
}
LABELS = {"default": "X", "coding": "编码", "pronto": "Pronto"}


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """返回 ``(状态目录, 假的原生目录)``。原生目录里多一个不在树里的目录。"""
    home = tmp_path / "native-home"
    (home / "profiles" / "stray").mkdir(parents=True)
    (home / "constitution.md").write_text("root rule\n", encoding="utf-8")

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


def paired_db(state: Path) -> Path:
    """The default database location under a state root: the only one the one-time
    file import runs on automatically."""
    return state / "state" / "domain.sqlite3"


# --------------------------------------------------------------------------- #
# 1. 端点形状
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path):
    state, home = make_workspace(tmp_path)
    runtime = open_runtime(state, home, paired_db(state))
    from app.projects.models import AgentBinding, Backend
    repositories = runtime.unit_of_work.repositories
    asyncio.run(repositories.backends.save(Backend.create(key="mock", display_name="Mock", driver_kind="mock")))
    asyncio.run(repositories.bindings.save(AgentBinding.create(
        project="project:coding", backend="backend:mock", display_name="coding", native_scope_ref="coding",
        is_default=True)))
    application = fastapi.FastAPI()
    application.include_router(runtime.router)
    with TestClient(application) as test_client:
        test_client.state_root = state
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
    assert by_id["project:coding"]["metadata"]["ui_state"]["pinned"] is True
    # 原生目录里多出来的目录不是 Project。
    assert "project:stray" not in by_id


def test_get_project_accepts_bare_slug_and_lists_children(client):
    full = client.get("/api/projects/project:default").json()
    bare = client.get("/api/projects/default").json()
    assert full == bare
    assert set(full["childProjectIds"]) == {"project:coding", "project:media"}


def test_get_project_404(client):
    response = client.get("/api/projects/nope")
    assert response.status_code == 404


def test_project_bindings_are_what_the_connection_flow_created(client):
    """导入不造任何 Agent Binding：Project 的 Binding 只来自接入流程。"""
    body = client.get("/api/projects/coding/bindings").json()
    assert [b["backendId"] for b in body["bindings"]] == ["backend:mock"]
    assert body["count"] == 1
    assert client.get("/api/projects/default/bindings").json()["count"] == 0


def test_backends_endpoint(client):
    body = client.get("/api/backends").json()
    assert body["count"] == 1
    backend = body["backends"][0]
    assert backend["id"] == "backend:mock"
    assert backend["driverKind"] == "mock"


def test_get_backend_endpoint_outputs_two_layer_capabilities(client):
    """AD-71：``GET /api/backends/{id}`` 的能力分两层——``ui`` 只有取值，``detail`` 才带 note/verification。"""
    full = client.get("/api/backends/backend:mock").json()
    bare = client.get("/api/backends/mock").json()
    assert full == bare
    assert full["id"] == "backend:mock"

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
    binding_id = client.get("/api/projects/coding/bindings").json()["bindings"][0]["id"]
    body = client.get(f"/api/bindings/{binding_id}").json()
    assert body["projectId"] == "project:coding"
    assert body["isDefault"] is True
    assert client.get("/api/bindings/binding:nope:mock").status_code == 404


# --------------------------------------------------------------------------- #
# 2. 对账端点
# --------------------------------------------------------------------------- #


def test_diff_is_unavailable_because_the_database_is_the_only_source(client):
    response = client.get("/api/_domain/diff")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "reconciliation_unavailable"


def test_diff_without_a_source_is_503_not_a_fake_in_sync(tmp_path):
    from app.api.router import build_domain_router
    from app.persistence.memory import in_memory_repository_set

    application = fastapi.FastAPI()
    application.include_router(build_domain_router(in_memory_repository_set()))
    with TestClient(application) as test_client:
        assert test_client.get("/api/_domain/diff").status_code == 503


# --------------------------------------------------------------------------- #
# 3. feature flag
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
            "/api/bindings/binding:default:mock",
            "/api/_domain/diff",
        ):
            assert test_client.get(path).status_code == 404


def test_attach_mounts_everything_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    state, home = make_workspace(tmp_path)
    db = paired_db(state)

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
    # The legacy files were moved aside (never deleted) and the database took over.
    assert not (state / "hierarchy.json").exists()
    legacy = [p for p in (state / "state").iterdir() if p.name.startswith("legacy-project-files-")]
    assert len(legacy) == 1 and (legacy[0] / "hierarchy.json").is_file()
    # The native home was only read (root rule layer), never written.
    assert sorted(p.name for p in home.rglob("*")) == ["constitution.md", "profiles", "stray"]
    try:
        with TestClient(application) as test_client:
            assert test_client.get("/api/projects").json()["count"] == 5
            assert test_client.get("/api/_domain/diff").status_code == 503
            root = test_client.get("/api/projects/default/capabilities").json()
            assert [a["capabilityId"] for a in root["assignments"]] == ["constitution-default"]
    finally:
        runtime.close()


def test_attach_skips_the_import_for_a_custom_db(tmp_path, monkeypatch):
    """A custom DASH_DOMAIN_DB is not paired with the state root: nothing is imported,
    nothing is moved, and the marker records why."""
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    state, home = make_workspace(tmp_path)
    db = tmp_path / "elsewhere.sqlite3"
    messages: list[str] = []
    runtime = domain_bootstrap.attach_domain_api(
        fastapi.FastAPI(), state_root=state, hermes_home=home, db_path=db,
        config_loader=lambda: {}, log=messages.append,
    )
    assert runtime is not None and runtime.imported_on_start is False
    try:
        import project_file_import
        assert project_file_import.marker(runtime.unit_of_work.database)["status"] == "skipped:not_paired"
        projects = asyncio.run(runtime.unit_of_work.repositories.projects.list_all())
        assert [p.id for p in projects] == ["project:default"]
    finally:
        runtime.close()
    assert (state / "hierarchy.json").is_file()
    assert any("import_project_files.py" in m for m in messages)


def test_import_runs_once_and_later_files_are_only_moved(tmp_path, monkeypatch):
    monkeypatch.setenv(domain_bootstrap.FEATURE_ENV_VAR, "1")
    state, home = make_workspace(tmp_path)
    db = paired_db(state)

    first = open_runtime(state, home, db)
    assert first.imported_on_start is True
    first.close()
    assert not (state / "labels.json").exists()

    # Files written later are not a source any more: they are moved aside, not imported.
    write_state(state, labels={**LABELS, "coding": "别的名字"}, pinned=["coding"], killed=["media"])
    second = open_runtime(state, home, db)
    try:
        assert second.imported_on_start is False
        coding = asyncio.run(second.unit_of_work.repositories.projects.get("project:coding"))
        assert coding.display_name == "编码"
    finally:
        second.close()
    assert not (state / "labels.json").exists()
    moved = [f for d in (state / "state").iterdir() if d.name.startswith("legacy-project-files-")
             for f in d.iterdir() if f.name.startswith("labels.json")]
    assert len(moved) == 2


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
# 4. 公共层纯净性（app/api/ 受 N §3 约束）
# --------------------------------------------------------------------------- #


def test_new_public_api_module_has_no_backend_private_tokens():
    import re

    forbidden = ("hermes", "profile", "codex", "claude", "xterm")
    for path in sorted((KERNEL_ROOT / "app" / "api").rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in lowered, f"{path.name} 含 Backend 私有名词 {token!r}"
        assert not re.search(r"(?<![a-z])pty(?![a-z])", lowered)
