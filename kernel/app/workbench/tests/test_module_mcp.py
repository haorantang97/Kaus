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
