"""Event Store 的 Repository 接口与内存参考实现。

对应规范
--------
- v1.0 §11.1 ``event_store``。
- v1.0 §8.5 / D-16 / AD-13：短期重放缓冲。接口层因此长这样：
  * 只有 ``append`` 没有 ``update`` —— 事件流是追加的，改历史不是这张表的职能；
  * ``append`` 按 ``event_id`` 幂等 —— 断线重放同一条事件不产生第二行；
  * 必须有 :meth:`EventStoreRepository.purge_expired` 与
    :meth:`EventStoreRepository.delete_for_conversation` —— 「全表可删除且不影响
    对话内容的可恢复性」是这张表的定义性质，不是可选功能；
  * 没有任何「按内容查询 / 全文检索」形态的方法 —— 那会诱使它变成第二本账
    （v1.0 §17 风险 9）。
- v1.0 §8.5：``?after=<sequence>`` 断线续传 → :meth:`list_after`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from app.errors import DomainInvariantError
from app.events.models import StoredEvent


@runtime_checkable
class EventStoreRepository(Protocol):
    """v1.0 §11.1 ``event_store``。"""

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

    async def latest_sequence(self, conversation_id: str) -> int | None:
        """该 Conversation 已缓冲到的最大 sequence；空缓冲返回 ``None``。"""
        ...

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """v1.0 §8.5 保留期：删除已过期的行，返回删除条数。"""
        ...

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """丢弃某条 Conversation 的全部缓冲；返回删除条数。

        v1.0 §11.3：删除后重新打开卡片从原生历史重建，对话内容不受影响。
        """
        ...


class InMemoryEventStoreRepository:
    """参考实现。"""

    def __init__(self, events: Sequence[StoredEvent] = ()) -> None:
        self._by_id: dict[str, StoredEvent] = {}
        for event in events:
            self._by_id[event.event_id] = event

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

    async def latest_sequence(self, conversation_id: str) -> int | None:
        sequences = [
            event.sequence
            for event in self._by_id.values()
            if event.conversation_id == conversation_id
        ]
        return max(sequences) if sequences else None

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        doomed = [
            event.event_id
            for event in self._by_id.values()
            if event.is_expired(now)
        ]
        for event_id in doomed:
            del self._by_id[event_id]
        return len(doomed)

    async def delete_for_conversation(self, conversation_id: str) -> int:
        doomed = [
            event.event_id
            for event in self._by_id.values()
            if event.conversation_id == conversation_id
        ]
        for event_id in doomed:
            del self._by_id[event_id]
        return len(doomed)


__all__ = ["EventStoreRepository", "InMemoryEventStoreRepository"]
