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
from app.projects import retirement


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
            metadata = {**existing.metadata, **desired.metadata}
            status = desired.status if existing.status in ("active", "draft") else existing.status
            if existing.metadata.get("configurationArchived") and pid not in before["projects"]:
                # Re-created with a deleted Project's slug. Its old conversations
                # stay archived; restoring them is the user's choice.
                metadata.pop("configurationArchived", None)
                status = desired.status
            desired = existing.evolve(display_name=desired.display_name,
                parent_project_id=desired.parent_project_id,
                status=status, metadata=metadata, updated_at=now)
        await repositories.projects.save(desired)
    for pid in before["projects"].keys() - after["projects"].keys():
        existing = await repositories.projects.get(pid)
        if existing is not None:
            await repositories.projects.save(existing.evolve(status="disabled", metadata={**existing.metadata, "configurationArchived": True}, updated_at=now))
            # Deleting a Project never removes conversations: archive them so they
            # stay readable and deletable from the archive list.
            await retirement.archive_project_conversations(repositories, pid, at=now)
    changed = {key for key in before["capabilities"].keys() | after["capabilities"].keys()
               if before["capabilities"].get(key) != after["capabilities"].get(key)}
    for pid, kind, key in changed:
        if pid not in after["projects"]:
            continue
        existing = next((r for r in await repositories.capabilities.list_for_project(pid)
                         if r.capability_type == kind and r.capability_id == key), None)
        if existing is not None and existing.assignment_mode == "block":
            # A block is an explicit Kaus decision; the file tree has no way to express
            # it, so a file edit (e.g. saving the persona) must not silently undo it.
            continue
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


def _deleted_project_ids(server, request) -> list[str]:
    """Projects a ``DELETE /api/agent/<name>`` removes: the whole subtree, deepest first."""
    if request.method != "DELETE" or not request.url.path.startswith("/api/agent/"):
        return []
    name = request.url.path[len("/api/agent/"):]
    if not name or "/" in name:
        return []
    order = getattr(server, "_project_delete_order", None)
    names = order(name) if callable(order) else [name]
    return [f"project:{item}" for item in names]


def install(app, server, repositories, session_host=None, surface_coordinator=None):
    import asyncio
    from starlette.responses import JSONResponse
    lock = asyncio.Lock()

    async def archive_orphans():
        # Projects deleted before conversations were archived with them.
        try:
            await retirement.archive_orphaned_conversations(repositories)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Archiving conversations of deleted Projects failed")

    startup = getattr(getattr(app, "router", None), "on_startup", None)
    if isinstance(startup, list):
        startup.append(archive_orphans)
    # State the domain DB is known to hold. After a failed sync the files are ahead of
    # the DB; diffing the next write (including a retry) against this keeps that change.
    synced: dict[str, Any] = {}

    def matches(path):
        return path.startswith(("/api/agent/", "/api/agent-skills/", "/api/constitution/", "/api/skills/")) or path in (
            "/api/move", "/api/kill", "/api/link", "/api/unlink", "/api/constitution")

    @app.middleware("http")
    async def sync_project_edit(request, call_next):
        if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not matches(request.url.path):
            return await call_next(request)
        async with lock:
            project_ids = _deleted_project_ids(server, request)
            if project_ids:
                async def blocking():
                    found: list[str] = []
                    for project_id in project_ids:
                        found.extend(await retirement.blocking_conversation_ids(repositories, session_host, project_id))
                    return found
                active = await blocking()
                if active and request.query_params.get("force") in ("1", "true"):
                    # Confirmed by the user: stop first, then delete and archive as usual.
                    await retirement.stop_conversations(repositories, session_host, surface_coordinator, active)
                    active = await blocking()
                if active:
                    message = "此 Project 有对话正在运行，结束后再删除。"
                    return JSONResponse({"detail": message, "error": {
                        "code": retirement.PROJECT_HAS_ACTIVE_RUNTIME, "message": message,
                        "activeConversationIds": active}}, status_code=409)
            pending = synced.pop("pending", None)
            before = pending or snapshot(server)
            response = await call_next(request)
            # A cascade that failed part-way has still deleted some Projects; sync them.
            if response.status_code < 400 or pending is not None or project_ids:
                try:
                    await apply_delta(repositories, before, snapshot(server))
                except Exception:
                    synced["pending"] = before
                    import logging
                    logging.getLogger(__name__).exception("Project configuration synchronization failed")
                    return JSONResponse({"error": {"code": "project_sync_failed",
                        "message": "配置已保存，同步失败。请重试。"}}, status_code=500)
            return response
