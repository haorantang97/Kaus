"""本地会话记录的 Repository 接口与内存参考实现。

事件按 ID 幂等追加、按 sequence 重放。期限由写入层决定，过期清理只应影响
临时事件；明确删除会话可移除全部记录。引擎原生文件不在本接口的管理范围内。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from app.errors import DomainInvariantError
from app.events.models import StoredEvent


@runtime_checkable
class EventStoreRepository(Protocol):
    """v1.0 §11.1 ``event_store``。"""

    async def get_native_storage(self, conversation_id: str) -> dict | None: ...

    async def save_native_storage(self, conversation_id: str, state: dict) -> None: ...

    async def replace_with_native_references(self, events: Sequence[StoredEvent]) -> int:
        """Atomically replace verified text with references; keep event identity."""
        ...

    async def get(self, event_id: str) -> StoredEvent | None: ...

    async def append(self, event: StoredEvent) -> StoredEvent:
        """追加一条。按 ``event_id`` 幂等：重复投递返回已存在的那条，不报错。

        同一 conversation 内 ``sequence`` 唯一；**不同** ``event_id`` 抢同一个
        sequence 是数据错误，抛 :class:`~app.errors.DomainInvariantError`。
        """
        ...

    async def append_many(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        """批量追加（一次网络往返写入一批事件）。语义同 :meth:`append`。"""
        ...

    async def list_after(
        self,
        conversation_id: str,
        *,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> Sequence[StoredEvent]:
        """按 ``sequence`` 升序返回；``after_sequence`` 即 WS ``?after=`` 游标。"""
        ...

    async def list_for_collaboration(
        self, collaboration_id: str, *, after_sequence: int | None = None
    ) -> Sequence[StoredEvent]:
        """Group 时间线聚合（v1.0 §11.1 ``collaboration_session_id``）。"""
        ...

    async def message_receipt(self, conversation_id: str, client_ref: str) -> Sequence[StoredEvent]:
        """Latest matching user receipt and any subsequent rejection, at most two rows."""
        ...

    async def latest_sequence(self, conversation_id: str) -> int | None:
        """该 Conversation 已缓冲到的最大 sequence；空缓冲返回 ``None``。"""
        ...

    async def purge_expired(self, *, now: datetime | None = None, exclude_conversations: Sequence[str] = ()) -> int:
        """v1.0 §8.5 保留期：删除已过期的行，返回删除条数。"""
        ...

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """明确删除某条 Conversation 的全部本地记录；返回删除条数。"""
        ...


class InMemoryEventStoreRepository:
    """参考实现。"""

    def __init__(self, events: Sequence[StoredEvent] = ()) -> None:
        self._native_storage: dict[str, dict] = {}
        self._by_id: dict[str, StoredEvent] = {}
        for event in events:
            self._by_id[event.event_id] = event

    async def get_native_storage(self, conversation_id: str) -> dict | None:
        state = self._native_storage.get(conversation_id)
        return dict(state) if state else None

    async def save_native_storage(self, conversation_id: str, state: dict) -> None:
        self._native_storage[conversation_id] = dict(state)

    async def replace_with_native_references(self, events: Sequence[StoredEvent]) -> int:
        for event in events:
            old = self._by_id.get(event.event_id)
            if old is None or old.native_reference is not None:
                raise DomainInvariantError("native_reference_conflict")
        for event in events:
            self._by_id[event.event_id] = event
        return len(events)

    async def get(self, event_id: str) -> StoredEvent | None:
        return self._by_id.get(event_id)

    async def append(self, event: StoredEvent) -> StoredEvent:
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            # 去重键命中：重放幂等（v1.0 §11.1 / N §7.3 规则 9）。
            return existing
        for other in self._by_id.values():
            if (
                other.conversation_id == event.conversation_id
                and other.sequence == event.sequence
            ):
                raise DomainInvariantError(
                    "同一 Conversation 内 sequence 必须唯一："
                    f"{event.conversation_id!r}#{event.sequence}"
                )
        self._by_id[event.event_id] = event
        return event

    async def append_many(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        return tuple([await self.append(event) for event in events])

    async def list_after(
        self,
        conversation_id: str,
        *,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> Sequence[StoredEvent]:
        selected = sorted(
            (
                event
                for event in self._by_id.values()
                if event.conversation_id == conversation_id
                and (after_sequence is None or event.sequence > after_sequence)
            ),
            key=lambda event: event.sequence,
        )
        return tuple(selected if limit is None else selected[:limit])

    async def list_for_collaboration(
        self, collaboration_id: str, *, after_sequence: int | None = None
    ) -> Sequence[StoredEvent]:
        selected = sorted(
            (
                event
                for event in self._by_id.values()
                if event.collaboration_session_id == collaboration_id
                and (after_sequence is None or event.sequence > after_sequence)
            ),
            key=lambda event: (event.sequence, event.conversation_id),
        )
        return tuple(selected)

    async def message_receipt(self, conversation_id: str, client_ref: str) -> Sequence[StoredEvent]:
        rows=[]
        for row in self._by_id.values():
            event=row.envelope.event
            if (row.conversation_id == conversation_id and event.type == "extension.event"
                and getattr(event, "namespace", None) == "kaus"
                and getattr(event, "name", None) in {"user.message", "user.message.failed"}
                and isinstance(getattr(event, "data", None), dict)
                and event.data.get("clientRef") == client_ref):
                rows.append(row)
        return latest_message_receipt(rows)

    async def latest_sequence(self, conversation_id: str) -> int | None:
        sequences = [
            event.sequence
            for event in self._by_id.values()
            if event.conversation_id == conversation_id
        ]
        return max(sequences) if sequences else None

    async def purge_expired(self, *, now: datetime | None = None, exclude_conversations: Sequence[str] = ()) -> int:
        retained = frozenset(exclude_conversations)
        doomed = [
            event.event_id
            for event in self._by_id.values()
            if event.is_expired(now) and event.conversation_id not in retained
        ]
        for event_id in doomed:
            del self._by_id[event_id]
        return len(doomed)

    async def delete_for_conversation(self, conversation_id: str) -> int:
        self._native_storage.pop(conversation_id, None)
        doomed = [
            event.event_id
            for event in self._by_id.values()
            if event.conversation_id == conversation_id
        ]
        for event_id in doomed:
            del self._by_id[event_id]
        return len(doomed)


def latest_message_receipt(rows: Sequence[StoredEvent]) -> tuple[StoredEvent, ...]:
    """An earlier rejected attempt must not override a later retry with this ref."""
    user=max((row for row in rows if row.envelope.event.name == "user.message"),
             key=lambda row: row.sequence, default=None)
    if user is None:
        return ()
    failed=max((row for row in rows if row.envelope.event.name == "user.message.failed" and row.sequence > user.sequence),
               key=lambda row: row.sequence, default=None)
    return (user, failed) if failed else (user,)


__all__ = ["EventStoreRepository", "InMemoryEventStoreRepository"]
