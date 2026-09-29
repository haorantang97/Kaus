"""Synchronize project configuration edits without changing Agent connections.

The original tree editor stores configuration in files. Its writes are mirrored
into the domain model as deltas so unrelated native defaults and domain edits
are not reimported on every save.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import domain_bootstrap
import domain_apply


def snapshot(server) -> dict[str, Any]:
    plan = domain_bootstrap.build_plan(state_root=server.HERE, hermes_home=server.HERMES_HOME,
                                      profiles_dir=server.PROFILES_DIR)
    capabilities = domain_bootstrap.build_capability_plan(plan, state_root=server.HERE,
        hermes_home=server.HERMES_HOME, profiles_dir=server.PROFILES_DIR)
    rows = {(r["project_id"], r["capability_type"], r["capability_id"]): r for r in capabilities["rows"]}
    for project in plan["projects"]:
        body_path = server._profile_dir(project["slug"]) / "SOUL.md"
        body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
        if body.strip():
            key = (project["id"], "instructions", "project-persona")
            rows[key] = {"project_id": key[0], "capability_type": key[1], "capability_id": key[2],
                         "assignment_mode": "local", "config": {"body": body}}
    return {"projects": {p["id"]: p for p in plan["projects"]}, "capabilities": rows}


async def apply_delta(repositories, before, after):
    from app.capabilities.models import ProjectCapability
    now = datetime.now(timezone.utc)
    for pid, row in after["projects"].items():
        existing = await repositories.projects.get(pid)
        if existing is not None and before["projects"].get(pid) == row:
            continue
        desired = domain_apply.desired_project(row, at=now)
        if existing is not None:
            desired = existing.evolve(display_name=desired.display_name,
                parent_project_id=desired.parent_project_id,
                status=desired.status if existing.status in ("active", "draft") else existing.status,
                metadata={**existing.metadata, **desired.metadata}, updated_at=now)
        await repositories.projects.save(desired)
    for pid in before["projects"].keys() - after["projects"].keys():
        existing = await repositories.projects.get(pid)
        if existing is not None:
            await repositories.projects.save(existing.evolve(status="disabled", metadata={**existing.metadata, "configurationArchived": True}, updated_at=now))
    changed = {key for key in before["capabilities"].keys() | after["capabilities"].keys()
               if before["capabilities"].get(key) != after["capabilities"].get(key)}
    for pid, kind, key in changed:
        if pid not in after["projects"]:
            continue
        existing = next((r for r in await repositories.capabilities.list_for_project(pid)
                         if r.capability_type == kind and r.capability_id == key), None)
        row = after["capabilities"].get((pid, kind, key))
        if row is None:
            if existing is not None:
                await repositories.capabilities.delete(existing.id)
            continue
        if existing is None:
            desired = ProjectCapability.create(project_id=pid, capability_type=kind, capability_id=key,
                assignment_mode=row["assignment_mode"], config=row["config"])
        else:
            desired = existing.evolve(assignment_mode=row["assignment_mode"], config=row["config"], updated_at=now)
        await repositories.capabilities.save(desired)


def install(app, server, repositories):
    import asyncio
    from starlette.responses import JSONResponse
    lock = asyncio.Lock()

    def matches(path):
        return path.startswith(("/api/agent/", "/api/agent-skills/", "/api/constitution/", "/api/skills/")) or (
            path.startswith("/api/profile/") and path.endswith("/restore")) or path in (
            "/api/move", "/api/kill", "/api/link", "/api/unlink", "/api/constitution")

    @app.middleware("http")
    async def sync_project_edit(request, call_next):
        if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not matches(request.url.path):
            return await call_next(request)
        async with lock:
            before = snapshot(server)
            response = await call_next(request)
            if response.status_code < 400:
                try:
                    await apply_delta(repositories, before, snapshot(server))
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Project configuration synchronization failed")
                    return JSONResponse({"error": {"code": "project_sync_failed",
                        "message": "配置已保存，同步失败。请重试。"}}, status_code=500)
            return response
