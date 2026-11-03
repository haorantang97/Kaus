"""Project deletion guard for the tree editor.

The Project tree and its configuration live in the domain database (see
``app.projects.tree_store``), so there is nothing to mirror from files any
more. What remains here is the part that needs the Session Host:

- ``DELETE /api/agent/{name}`` is refused (409) while a conversation of the
  Project or its subtree is running; ``?force=1`` stops them first;
- after a successful delete the conversations of the deleted subtree are
  archived (never deleted);
- at startup, conversations of Projects deleted earlier are archived too.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.projects import retirement


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


async def archive_deleted(repositories, project_ids, *, at=None):
    """Archive the conversations of every listed Project that is now deleted."""
    now = at or datetime.now(timezone.utc)
    archived = []
    for project_id in project_ids:
        project = await repositories.projects.get(project_id)
        if project is None or project.status != "disabled":
            continue
        archived.extend(await retirement.archive_project_conversations(repositories, project_id, at=now))
    return archived


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

    @app.middleware("http")
    async def guard_project_delete(request, call_next):
        if request.method != "DELETE" or not request.url.path.startswith("/api/agent/"):
            return await call_next(request)
        async with lock:
            project_ids = _deleted_project_ids(server, request)
            if not project_ids:
                return await call_next(request)

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
            response = await call_next(request)
            # A cascade that failed part-way has still deleted some Projects; archive theirs too.
            try:
                await archive_deleted(repositories, project_ids)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Archiving conversations of deleted Projects failed")
            return response
