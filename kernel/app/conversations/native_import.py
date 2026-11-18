"""Import an existing native conversation as an ordinary Kaus conversation (2026-11-18).

Product decision「原生历史导入侧栏」: a conversation added through「添加已有对话」
appears in the sidebar like any other. It is bound to the original native session
(``native_session_id``), so the next turn continues in that session when the
engine supports it, and its full native history is written once as a
``history.reconciled`` event marked ``imported``. Later terminal reconciliation
continues from that event's ``lastEntryId``.

Returns ``None`` when the source engine is not connected to Kaus or cannot read
its history; the caller then keeps the read-only archive record.
"""
from __future__ import annotations

import logging
from typing import Any

from app.conversations.models import Conversation
from app.persistence.base import RepositorySet
from app.projects.binding_snapshot import register_binding_snapshot
from drivers.base import UnsupportedCapabilityError
from runtime.event_envelope import ExtensionEvent
from runtime.surface_handoff import HISTORY_RECONCILED_NAME, KAUS_NAMESPACE, _diff_history

LOGGER = logging.getLogger(__name__)


def backend_matches(source: str, backend_id: str) -> bool:
    """Archive source ids are engine keys; a driver's source module may declare more."""
    from drivers.archive_sources import backend_keys
    return backend_id.removeprefix("backend:") in backend_keys(source)


def first_prompt(history: Any, limit: int = 60) -> str | None:
    """Title fallback when the engine keeps no title index: the first thing the user asked."""
    for entry in history.entries:
        if entry.role == "user" and (entry.text or "").strip():
            text = " ".join(entry.text.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return None


def native_title(title: str | None, path: str | None) -> str | None:
    """A discovered title that is only the transcript's file name is no title."""
    from pathlib import Path
    if not title or (path and title == Path(path).stem):
        return None
    return title


class NativeConversationImporter:
    def __init__(self, repositories: RepositorySet, host: Any, registry: Any) -> None:
        self._repositories = repositories
        self._host = host
        self._registry = registry

    async def existing(self, project_id: str, source: str, native_id: str) -> Conversation | None:
        for archived in (False, True):
            for conversation in await self._repositories.conversations.list_for_project(project_id, archived=archived):
                if conversation.native_session_id != native_id:
                    continue
                binding = await self._repositories.bindings.get(conversation.agent_binding_id)
                if binding is not None and backend_matches(source, binding.backend_id):
                    return conversation
        return None

    async def _binding(self, project_id: str, source: str):
        from app.api.binding_router import ensure_project_binding
        from app.api.api_errors import ApiError
        candidates = [backend.id for backend in await self._repositories.backends.list_all()
                      if backend_matches(source, backend.id) and self._registry.try_get(backend.id) is not None]
        for backend_id in candidates:
            try:
                return await ensure_project_binding(self._repositories, self._registry, project_id, backend_id)
            except ApiError:
                continue
        return None

    async def adopt(self, project_id: str, source: str, native_id: str, title: str | None) -> Conversation | None:
        found = await self.existing(project_id, source, native_id)
        if found is not None:
            return found
        binding = await self._binding(project_id, source)
        if binding is None:
            return None
        driver = self._registry.get(binding.backend_id)
        await register_binding_snapshot(driver, binding, self._repositories.projects)
        try:
            history = await driver.load_native_history(binding, native_id)
        except UnsupportedCapabilityError:
            return None
        except Exception:
            LOGGER.warning("Native history for %s could not be read; kept the archive record", native_id, exc_info=True)
            return None
        if history.native_session_id != native_id:
            return None
        conversation = await self._repositories.conversations.save(Conversation.create(
            project_id=project_id,
            agent_binding_id=binding.id,
            title=(title or "").strip() or first_prompt(history) or binding.display_name,
            native_session_id=native_id,
        ))
        # Same order as terminal reconciliation: the watermark first, so later
        # inbound text may use native references, then the history itself.
        await self._host.event_store.native_storage.begin(conversation.id, native_id)
        normalized = await self._host.normalize_native_history(conversation.id, history)
        entries, complete, gaps = _diff_history(normalized, seen=None)
        await self._host.emit_conversation_event(conversation, ExtensionEvent(
            namespace=KAUS_NAMESPACE,
            name=HISTORY_RECONCILED_NAME,
            data={
                "entries": list(entries),
                "complete": complete,
                "gaps": list(gaps),
                "lastEntryId": normalized.entries[-1].entry_id if normalized.entries else None,
                "nativeSessionId": native_id,
                "imported": True,
            },
        ))
        await self._host.compact_native_text(conversation)
        return await self._repositories.conversations.get(conversation.id) or conversation
