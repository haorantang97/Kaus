"""Register a workbench module's own MCP server as a Project capability.

A module declares ``mcp`` in its manifest. Once the user grants ``mcp:serve`` and
chooses the module's Project, the server becomes the ``mcp`` capability
``module-<id>`` of that Project, inherited down the tree like any other MCP.
Agents start it through a small Node launcher that passes the module data
directory, so the MCP server and the module backend share the same data.
Disabling, uninstalling, revoking the grant or moving the module to another
Project removes the row again.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.capabilities.models import ProjectCapability

LOGGER = logging.getLogger(__name__)

CAPABILITY_TYPE = "mcp"
LAUNCHER = Path(__file__).with_name("module_mcp_launcher.mjs")


def capability_id(module_id: str) -> str:
    return f"module-{module_id}"


class ModuleMcpRegistry:
    def __init__(self, service, repositories, *, node: str | None = None):
        self.service = service
        self.repositories = repositories
        self.node = node
        service.module_change_listeners.append(self.changed)

    def _desired(self, module_id: str) -> tuple[str, dict] | None:
        """``(project_id, config)`` the row should have now, or ``None`` for no row."""
        module = self.service.store.get("modules", module_id)
        if not module or not module.get("installed", True) or not module.get("enabled"):
            return None
        mcp, project_id = module.get("mcp"), module.get("projectId")
        root = Path(module["assetRoot"]) if module.get("assetRoot") else None
        node = self.node or shutil.which("node")
        if not mcp or not project_id or "mcp:serve" not in module.get("grants", []) or root is None or node is None:
            return None
        entry = (root / mcp["entry"]).resolve()
        if not entry.is_relative_to(root.resolve()) or not entry.is_file():
            return None
        data = self.service.root / "module-data" / module_id
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        server = {"command": node, "args": [str(LAUNCHER), str(root.resolve()), mcp["entry"], str(data), module_id]}
        return project_id, {"value": server}

    async def changed(self, module_id: str) -> None:
        cap_id = capability_id(module_id)
        desired = self._desired(module_id)
        projects = await self.repositories.projects.list_all()
        by_project = await self.repositories.capabilities.list_for_projects([p.id for p in projects])
        rows = [row for assignments in by_project.values() for row in assignments
                if row.capability_type == CAPABILITY_TYPE and row.capability_id == cap_id]
        for row in rows:
            if desired is None or row.project_id != desired[0] or dict(row.config) != desired[1]:
                await self.repositories.capabilities.delete(row.id)
        if desired is not None and not any(row.project_id == desired[0] and dict(row.config) == desired[1] for row in rows):
            await self.repositories.capabilities.save(ProjectCapability.create(
                project_id=desired[0], capability_type=CAPABILITY_TYPE, capability_id=cap_id,
                assignment_mode="local", config=desired[1]))

    async def sync_all(self) -> None:
        """Startup: the package path or Node location may have changed since last run."""
        for module in self.service.store.all("modules"):
            try:
                await self.changed(module["id"])
            except Exception:  # noqa: BLE001 - one broken module must not block the others
                LOGGER.warning("模块 MCP 登记失败：%s", module.get("id"), exc_info=True)


__all__ = ["ModuleMcpRegistry", "capability_id"]
