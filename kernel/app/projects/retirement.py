"""Deleting a Project retires it; its conversations are kept.

Product rule: conversations are removed only by the user. Deleting a Project
archives its configuration and marks the domain row ``disabled``. Its
conversations are archived alongside (``archived_at`` set; rows, events and
native sessions untouched) so they appear in the user's archive list, where
they can be read, deleted, moved to another Project (same engine) or restored
with their Project. A disabled Project accepts no new runs. A delete confirmed by
the user (``?force=1``) stops running conversations first. Restoring or
re-creating the Project never unarchives anything by itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from app.api.api_errors import ApiError

PROJECT_DELETED = "project_deleted"
PROJECT_HAS_ACTIVE_RUNTIME = "project_has_active_runtime"


def is_retired(project: Any) -> bool:
    return project is not None and getattr(project, "status", None) == "disabled"


def project_deleted_error() -> ApiError:
    return ApiError(409, PROJECT_DELETED, "所属 Project 已删除。")


async def require_open_project(repositories: Any, project_id: str) -> None:
    """Refuse new work in a deleted Project. Unknown Projects are left to the caller."""
    if is_retired(await repositories.projects.get(project_id)):
        raise project_deleted_error()


async def _owned_conversation_ids(repositories: Any, project_id: str) -> Sequence[str]:
    rows = await repositories.conversations.list_for_project(
        project_id, include_group_only=True, archived=False
    )
    archived = await repositories.conversations.list_for_project(
        project_id, include_group_only=True, archived=True
    )
    return [row.id for row in (*rows, *archived)]


async def blocking_conversation_ids(
    repositories: Any, session_host: Any, project_id: str
) -> list[str]:
    """Conversations of this Project that still own a runtime or a held lease.

    A stale card lease is recoverable and does not block. An external terminal
    lease blocks even when stale: a missed heartbeat does not prove the native
    process exited.
    """
    blocking: list[str] = []
    for conversation_id in await _owned_conversation_ids(repositories, project_id):
        if session_host is not None and session_host.is_active(conversation_id):
            blocking.append(conversation_id)
            continue
        if session_host is not None:
            lease = await session_host.describe_runtime_owner(conversation_id)
            stale = lease is not None and lease.is_stale
        else:
            lease = await repositories.leases.get(conversation_id)
            stale = lease is not None and lease.is_expired()
        if lease is None:
            continue
        if lease.owner_type == "external-cli" or not stale:
            blocking.append(conversation_id)
    return blocking


async def stop_conversations(
    repositories: Any, session_host: Any, surface_coordinator: Any, conversation_ids: Sequence[str]
) -> None:
    """Stop the given conversations before a confirmed delete.

    A terminal-held conversation is returned to the card the same way "return to
    conversation" does with ``force`` (ends the terminal agent, reconciles native
    history, releases the lease). A card runtime then goes through the normal stop
    path. Failures are left for the caller's re-check to report.
    """
    if session_host is None:
        return
    for conversation_id in conversation_ids:
        try:
            owner = await session_host.describe_runtime_owner(conversation_id)
            if owner is not None and owner.owner_type == "external-cli" and surface_coordinator is not None:
                conversation = await repositories.conversations.get(conversation_id)
                if conversation is not None:
                    await surface_coordinator.to_card(conversation, force=True)
            if session_host.is_active(conversation_id):
                await session_host.stop_runtime(conversation_id)
        except Exception:  # noqa: BLE001 - the re-check reports what is still running
            import logging
            logging.getLogger(__name__).warning(
                "Stopping %s before Project delete failed", conversation_id, exc_info=True)


async def archive_project_conversations(
    repositories: Any, project_id: str, *, at: datetime | None = None
) -> list[str]:
    """Archive every unarchived conversation of a retired Project. Idempotent."""
    now = at or datetime.now(timezone.utc)
    archived: list[str] = []
    for conversation in await repositories.conversations.list_for_project(
        project_id, include_group_only=True, archived=False
    ):
        await repositories.conversations.save(
            conversation.evolve(archived_at=now, updated_at=now)
        )
        archived.append(conversation.id)
    return archived


async def archive_orphaned_conversations(repositories: Any) -> list[str]:
    """Archive unarchived conversations left in Projects deleted before this rule."""
    archived: list[str] = []
    for project in await repositories.projects.list_all():
        if is_retired(project):
            archived.extend(await archive_project_conversations(repositories, project.id))
    return archived
