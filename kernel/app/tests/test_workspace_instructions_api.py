"""接入已有文件夹（设计稿第 4 节）：发现与导入工作目录里已有的指令文件。

SQLite 与工作目录都在 ``tmp_path``；Driver 是只声明文件约定的假对象，不起进程。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.workspace_instructions import DECISIONS_KEY, build_workspace_instructions_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers import managed_block  # noqa: E402
from drivers.instructions_projection import INSTRUCTIONS_MARKER  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402

TEST_TOKEN = "test-token-0123456789"


class _FileDriver:
    """只回答「我读工作目录里的哪个文件、怎么送达」。"""

    driver_kind = "acp"

    def __init__(self, key: str, filename: str, channel: str) -> None:
        self.backend_id = f"backend:{key}"
        self._filename = filename
        self._channel = channel

    def workspace_conventions(self, binding=None):
        return {"instructionsFile": self._filename, "deliveryChannel": self._channel}


class Harness:
    def __init__(self, tmp_path: Path, *, channel: str = "workspace_file", workspace: bool = True) -> None:
        self.workspace = tmp_path / "work"
        self.workspace.mkdir()
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = _FileDriver("filer", "AGENTS.md", channel)
        self.app = fastapi.FastAPI()
        self.app.include_router(build_workspace_instructions_router(
            self.repositories, auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
            registry=BackendDriverRegistry([self.driver])))
        self.with_workspace = workspace

    async def seed(self) -> None:
        repos = self.repositories
        backend = await repos.backends.save(Backend.create(key="filer", driver_kind="acp", probe_state="available"))
        self.project = await repos.projects.save(Project.create(
            slug="media", workspace_root=str(self.workspace) if self.with_workspace else None))
        await repos.bindings.save(AgentBinding.create(project=self.project, backend=backend, is_default=True))

    def close(self) -> None:
        self.unit_of_work.close()


def _client(harness: Harness) -> TestClient:
    return TestClient(harness.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"})


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


def test_discovery_lists_declared_files_with_user_content(harness):
    (harness.workspace / "AGENTS.md").write_text("# Team rules\n\nBe brief.\n", encoding="utf-8")
    (harness.workspace / "OTHER.md").write_text("ignored", encoding="utf-8")
    with _client(harness) as client:
        body = client.get(f"/api/projects/{harness.project.id}/instructions/workspace-files").json()
    assert body["workspaceRoot"] == str(harness.workspace) and body["workspaceDenied"] is None
    (row,) = body["files"]
    assert row["file"] == "AGENTS.md" and row["backends"] == ["backend:filer"]
    assert row["deliveryChannel"] == "workspace_file"
    assert row["hasUserContent"] and row["pending"] and row["decision"] is None
    assert row["preview"].startswith("# Team rules")


def test_missing_workspace_is_a_localizable_409(tmp_path):
    built = Harness(tmp_path, workspace=False)
    asyncio.run(built.seed())
    try:
        with _client(built) as client:
            response = client.get(f"/api/projects/{built.project.id}/instructions/workspace-files")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "workspace_required"
    finally:
        built.close()


def test_import_moves_content_into_a_row_and_leaves_only_the_block(harness):
    target = harness.workspace / "AGENTS.md"
    target.write_text("# Team rules\n\nBe brief.\n", encoding="utf-8")
    with _client(harness) as client:
        response = client.post(f"/api/projects/{harness.project.id}/instructions/import",
                               json={"file": "AGENTS.md", "mode": "import"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["capabilityId"] == "imported-agents-md" and body["deliveryChannel"] == "workspace_file"
        assert body["backupPath"] and Path(body["backupPath"]).read_text(encoding="utf-8").startswith("# Team rules")
        rows = asyncio.run(harness.repositories.capabilities.list_for_project(harness.project.id))
        (row,) = [r for r in rows if r.capability_type == "instructions"]
        assert row.capability_id == "imported-agents-md"
        assert row.config == {"body": "# Team rules\n\nBe brief.", "order": 100, "title": "AGENTS.md"}
        text = target.read_text(encoding="utf-8")
        owner, block = managed_block.read_block(text, INSTRUCTIONS_MARKER)
        assert owner == harness.project.id and block == "## AGENTS.md\n\n# Team rules\n\nBe brief."
        assert not text.replace(managed_block.render_block(INSTRUCTIONS_MARKER, block, owner), "").strip()
        # 之后不再列为待处理，决定可查。
        listed = client.get(f"/api/projects/{harness.project.id}/instructions/workspace-files").json()
        (entry,) = listed["files"]
        assert entry["pending"] is False and entry["decision"]["mode"] == "import"
        assert entry["hasManagedBlock"] and not entry["hasUserContent"]
        # 再导一次：块外没有内容了。
        again = client.post(f"/api/projects/{harness.project.id}/instructions/import",
                            json={"file": "AGENTS.md", "mode": "import"})
        assert again.status_code == 409 and again.json()["error"]["code"] == "nothing_to_import"


def test_launch_channel_import_leaves_an_empty_block(tmp_path):
    built = Harness(tmp_path, channel="launch")
    asyncio.run(built.seed())
    try:
        target = built.workspace / "AGENTS.md"
        target.write_text("Be brief.\n", encoding="utf-8")
        with _client(built) as client:
            response = client.post(f"/api/projects/{built.project.id}/instructions/import",
                                   json={"file": "AGENTS.md", "mode": "import"})
        assert response.status_code == 200, response.text
        assert managed_block.read_block(target.read_text(encoding="utf-8"), INSTRUCTIONS_MARKER) == (built.project.id, "")
    finally:
        built.close()


def test_keep_records_the_decision_and_touches_nothing(harness):
    target = harness.workspace / "AGENTS.md"
    target.write_text("Be brief.\n", encoding="utf-8")
    with _client(harness) as client:
        response = client.post(f"/api/projects/{harness.project.id}/instructions/import",
                               json={"file": "AGENTS.md", "mode": "keep"})
        assert response.status_code == 200 and response.json()["mode"] == "keep"
        (entry,) = client.get(f"/api/projects/{harness.project.id}/instructions/workspace-files").json()["files"]
    assert entry["pending"] is False and entry["decision"]["mode"] == "keep"
    assert target.read_text(encoding="utf-8") == "Be brief.\n"
    decisions = asyncio.run(harness.repositories.settings.get(DECISIONS_KEY))
    assert decisions[harness.project.id]["AGENTS.md"]["mode"] == "keep"
    rows = asyncio.run(harness.repositories.capabilities.list_for_project(harness.project.id))
    assert not [r for r in rows if r.capability_type == "instructions"]


def test_undeclared_file_and_bad_body_are_400(harness):
    with _client(harness) as client:
        assert client.post(f"/api/projects/{harness.project.id}/instructions/import",
                           json={"file": "OTHER.md"}).json()["error"]["code"] == "file_not_declared"
        assert client.post(f"/api/projects/{harness.project.id}/instructions/import",
                           json={"file": "../x", "mode": "import"}).json()["error"]["code"] == "invalid_import_request"
        assert client.get("/api/projects/project:nope/instructions/workspace-files").status_code == 404
