from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.projects.models import Project
from app.workbench.module_mcp import LAUNCHER, ModuleMcpRegistry
from app.workbench.tests.test_workbench import package, world


async def _rows(repos, module_id="notes"):
    projects = await repos.projects.list_all()
    by_project = await repos.capabilities.list_for_projects([p.id for p in projects])
    return [(row.project_id, row.config["value"]) for rows in by_project.values() for row in rows
            if row.capability_type == "mcp" and row.capability_id == f"module-{module_id}"]


def _mcp_package(tmp_path):
    root = package(tmp_path, "notes", ["mcp:serve", "data:read"], mcp={"entry": "mcp/server.mjs"})
    (root / "mcp").mkdir()
    (root / "mcp/server.mjs").write_text("console.log('mcp')")
    return root


async def test_granted_module_mcp_follows_project_enable_and_uninstall(tmp_path):
    async with world(tmp_path) as (service, _host, repos, _driver, _client, binding):
        registry = ModuleMcpRegistry(service, repos, node="/usr/bin/node")
        await service.install(str(_mcp_package(tmp_path)))
        assert await _rows(repos) == []  # 还没授权、没选项目
        await service.patch_module("notes", {"projectId": binding.project_id, "grants": ["mcp:serve"]})
        [(project_id, server)] = await _rows(repos)
        assert project_id == binding.project_id
        assert server["command"] == "/usr/bin/node"
        assert server["args"][0] == str(LAUNCHER)
        assert server["args"][2:] == ["mcp/server.mjs", str(service.root / "module-data" / "notes"), "notes"]
        other = await repos.projects.save(Project.create(slug="other", display_name="Other"))
        await service.patch_module("notes", {"projectId": other.id})
        assert [row[0] for row in await _rows(repos)] == [other.id]
        await service.patch_module("notes", {"enabled": False})
        assert await _rows(repos) == []
        await service.patch_module("notes", {"enabled": True})
        assert len(await _rows(repos)) == 1
        await service.patch_module("notes", {"grants": []})
        assert await _rows(repos) == []
        await service.patch_module("notes", {"grants": ["mcp:serve"]})
        await registry.sync_all()
        assert len(await _rows(repos)) == 1
        await service.uninstall("notes")
        assert await _rows(repos) == []


def test_manifest_mcp_requires_its_permission(tmp_path):
    from app.workbench.models import Manifest
    with pytest.raises(ValueError):
        Manifest.model_validate({"id": "notes", "name": "N", "version": "1", "permissions": [], "mcp": {"entry": "mcp/server.mjs"}})


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_launcher_passes_the_module_data_directory(tmp_path):
    root = tmp_path / "module"
    (root / "mcp").mkdir(parents=True)
    out = tmp_path / "seen.json"
    (root / "mcp/server.mjs").write_text(
        "import fs from 'node:fs';"
        f"fs.writeFileSync({json.dumps(str(out))}, JSON.stringify({{data: process.env.KAUS_MODULE_DATA_DIR, id: process.env.KAUS_MODULE_ID, cwd: process.cwd(), argv: process.argv.slice(1)}}));")
    data = tmp_path / "data"
    done = subprocess.run([shutil.which("node"), str(LAUNCHER), str(root), "mcp/server.mjs", str(data), "notes"], capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    seen = json.loads(out.read_text())
    assert seen["data"] == str(data) and seen["id"] == "notes"
    assert seen["cwd"] == str(root.resolve()) and seen["argv"] == [str((root / "mcp/server.mjs").resolve())]
    escaped = subprocess.run([shutil.which("node"), str(LAUNCHER), str(root), "../outside.mjs", str(data), "notes"], capture_output=True, timeout=20)
    assert escaped.returncode == 2


async def test_module_job_can_use_another_agent_of_its_project_only(tmp_path):
    from app.api.api_errors import ApiError
    from app.projects.models import AgentBinding
    async with world(tmp_path) as (service, _host, repos, _driver, client, binding):
        project = await repos.projects.get(binding.project_id)
        second = await repos.bindings.save(AgentBinding.create(project=project, backend="mock", display_name="Mock 2", discriminator="second"))
        stranger_project = await repos.projects.save(Project.create(slug="far", display_name="Far"))
        stranger = await repos.bindings.save(AgentBinding.create(project=stranger_project, backend="mock", display_name="Far mock"))
        listed = (await client.get("/api/workbench/modules/automation/agents")).json()["agents"]
        assert {(a["id"], a["default"]) for a in listed} == {(binding.id, True), (second.id, False)}
        response = await client.post("/api/workbench/modules/automation/jobs", json={"prompt": "hi", "idempotencyKey": "k1", "bindingId": second.id})
        assert response.status_code == 202, response.text
        job = service._job_record("automation", response.json()["id"])
        assert job["config"]["bindingId"] == second.id and job["config"]["modelId"] is None and job["agentOverride"]
        await service._assert_job_scope(job)  # 换了 Agent 也不算「配置已变更」
        refused = await client.post("/api/workbench/modules/automation/jobs", json={"prompt": "hi", "idempotencyKey": "k2", "bindingId": stranger.id})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "binding_project_mismatch"
        same = await client.post("/api/workbench/modules/automation/jobs", json={"prompt": "hi", "idempotencyKey": "k3", "bindingId": binding.id})
        assert same.status_code == 202 and not service._job_record("automation", same.json()["id"])["agentOverride"]
        with pytest.raises(ApiError, match="授权"):
            await service.module_agents("content-workbench")  # 没授权 agent:run 的模块看不到 Agent


async def test_bundled_examples_are_listed_and_install_in_one_step(tmp_path):
    from pathlib import Path
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        assert (await client.get("/api/workbench/examples")).json() == {"examples": []}  # 没配置示例目录
        service.examples_root = Path(__file__).resolve().parents[4] / "examples"
        listed = {item["id"]: item for item in (await client.get("/api/workbench/examples")).json()["examples"]}
        assert {"workbench-note", "workflow-studio"} <= set(listed) and not listed["workbench-note"]["installed"]
        response = await client.post("/api/workbench/examples/workbench-note/install")
        assert response.status_code == 201, response.text
        assert response.json()["grants"] == []  # 权限仍要用户在模块配置里给
        assert {item["id"]: item for item in (await client.get("/api/workbench/examples")).json()["examples"]}["workbench-note"]["installed"]
        assert (await client.post("/api/workbench/examples/nope/install")).status_code == 404
