"""Ordered, idempotent replay with verified local native text references.

Unverified/rich payloads and host metadata remain durable. After explicit opt-in,
completed text may be replaced with a native-source reference; replay reads the
exact local source and never restores text removed by the native engine.
Diagnostic/authentication records still use TTL. No native retention is changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.events.models import (
    DIAGNOSTIC_EVENT_TYPES,
    DIAGNOSTIC_RETENTION,
    PRODUCT_CONTENT_EVENT_NAMES,
    PRODUCT_NAMESPACE,
    RETAIN_UNTIL_CONVERSATION_GONE,
    StoredEvent,
    is_content_event,
)
from app.events.repository import EventStoreRepository
from runtime.event_envelope import AgentEventEnvelope
from runtime.native_storage import NativeTextStorage

#: 未分类临时事件的默认 TTL；诊断/认证默认 24 小时，正文长期保存。
DEFAULT_RETENTION: timedelta = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class AppendOutcome:
    """一次追加的结果。

    ``is_duplicate`` 为真表示这条 ``eventId`` 之前就在表里——调用方据此知道
    「这次重放没有产生新行」，从而不重复推给订阅者、也不消耗新的 sequence。
    """

    __slots__ = ("stored", "is_duplicate")

    def __init__(self, stored: StoredEvent, *, is_duplicate: bool) -> None:
        self.stored = stored
        self.is_duplicate = is_duplicate

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"AppendOutcome(event_id={self.stored.event_id!r}, "
            f"sequence={self.stored.sequence}, is_duplicate={self.is_duplicate})"
        )


class EventStore:
    """``event_store`` 表的服务层门面（v1.0 §11.1 / §8.5）。"""

    def __init__(
        self,
        repository: EventStoreRepository,
        *,
        retention: timedelta = DEFAULT_RETENTION,
        diagnostic_retention: timedelta | None = None,
        clock=_utcnow,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("保留期必须为正：v1.0 §8.5 不接受「无保留策略」")
        self._repository = repository
        self.native_storage = NativeTextStorage(repository)
        self.retention = retention
        #: 诊断类事件的保留期，默认取「普通保留期」与 24 小时中的较小值。
        self.diagnostic_retention = diagnostic_retention or min(
            retention, DIAGNOSTIC_RETENTION
        )
        if self.diagnostic_retention <= timedelta(0):
            raise ValueError("诊断类保留期必须为正（v1.0 §8.5）")
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 保留期
    # ------------------------------------------------------------------ #

    def retention_for(
        self, event_type: str, *, namespace: str | None = None, name: str | None = None
    ) -> timedelta:
        """v1.0 §8.5：诊断类事件走更短的保留期。

        AD-143：``kaus`` namespace 下的**正文类**扩展事件（``user.message``）不算
        诊断数据——它就是用户说的那句话，信封 v1.1 冻结才让它借道
        ``extension.event``。按诊断期清理会让一天以前的会话重开时只剩助手的话。
        """
        if (
            event_type == "extension.event"
            and namespace == PRODUCT_NAMESPACE
            and name in PRODUCT_CONTENT_EVENT_NAMES
        ):
            return self.retention
        return (
            self.diagnostic_retention
            if event_type in DIAGNOSTIC_EVENT_TYPES
            else self.retention
        )

    def expires_at_for(
        self,
        envelope: AgentEventEnvelope,
        *,
        now: datetime,
        history_recoverable: bool | None = None,
    ) -> datetime:
        """Preserve replay rows; a capability boolean never releases content.

        Native text externalization uses separate exact-source verification.
        ``history_recoverable`` remains only for call compatibility.
        """
        event = envelope.event
        namespace = getattr(event, "namespace", None)
        name = getattr(event, "name", None)
        if is_content_event(
            event.type, namespace=namespace, name=name
        ):
            return RETAIN_UNTIL_CONVERSATION_GONE
        return now + self.retention_for(event.type, namespace=namespace, name=name)

    # ------------------------------------------------------------------ #
    # 追加
    # ------------------------------------------------------------------ #

    async def append(
        self,
        envelope: AgentEventEnvelope,
        *,
        collaboration_session_id: str | None = None,
        now: datetime | None = None,
        history_recoverable: bool | None = None,
    ) -> AppendOutcome:
        """写一条。按 ``eventId`` 幂等：重复投递不产生第二行。

        ``history_recoverable`` 保留兼容，任何取值都不会缩短正文保留期。
        """
        at = now or self._clock()
        existing = await self._repository.get(envelope.event_id)
        if existing is not None:
            return AppendOutcome(existing, is_duplicate=True)
        stored = StoredEvent.from_envelope(
            envelope,
            expires_at=self.expires_at_for(
                envelope, now=at, history_recoverable=history_recoverable
            ),
            now=at,
            collaboration_session_id=collaboration_session_id,
        )
        return AppendOutcome(await self._repository.append(stored), is_duplicate=False)

    async def append_many(
        self,
        envelopes: Sequence[AgentEventEnvelope],
        *,
        collaboration_session_id: str | None = None,
        now: datetime | None = None,
        history_recoverable: bool | None = None,
    ) -> tuple[AppendOutcome, ...]:
        return tuple(
            [
                await self.append(
                    envelope,
                    collaboration_session_id=collaboration_session_id,
                    now=now,
                    history_recoverable=history_recoverable,
                )
                for envelope in envelopes
            ]
        )

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    async def find(self, event_id: str) -> StoredEvent | None:
        """At-rest row for identity/dedup checks; use replay for hydrated content."""
        return await self._repository.get(event_id)

    async def contains(self, event_id: str) -> bool:
        return await self.find(event_id) is not None

    async def read_range(
        self,
        conversation_id: str,
        *,
        after_sequence: int | None = None,
        until_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        """按 ``(conversationId, sequence)`` 升序取一段。

        ``after_sequence`` 是**开区间**下界（WS ``?after=`` 的语义：不含它自己），
        ``until_sequence`` 是**闭区间**上界。
        """
        rows = await self._repository.list_after(
            conversation_id, after_sequence=after_sequence, limit=None
        )
        selected = [
            row
            for row in rows
            if until_sequence is None or row.sequence <= until_sequence
        ]
        if limit is not None:
            selected = selected[:limit]
        return await self.native_storage.hydrate(selected)

    async def replay(
        self,
        conversation_id: str,
        *,
        after_sequence: int | None = None,
        until_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[AgentEventEnvelope, ...]:
        """同 :meth:`read_range`，但只给 Envelope（重放给 reducer / 前端用）。"""
        return tuple(
            row.envelope
            for row in await self.read_range(
                conversation_id,
                after_sequence=after_sequence,
                until_sequence=until_sequence,
                limit=limit,
            )
        )

    async def message_receipt(self, conversation_id: str, client_ref: str) -> tuple[AgentEventEnvelope, ...]:
        rows = await self._repository.message_receipt(conversation_id, client_ref)
        return tuple(row.envelope for row in await self.native_storage.hydrate(rows))

    async def latest_sequence(self, conversation_id: str) -> int | None:
        return await self._repository.latest_sequence(conversation_id)

    async def next_sequence(self, conversation_id: str) -> int:
        """下一个可用 sequence。空缓冲从 0 开始。"""
        latest = await self.latest_sequence(conversation_id)
        return 0 if latest is None else latest + 1

    async def list_for_collaboration(
        self, collaboration_id: str, *, after_sequence: int | None = None
    ) -> tuple[StoredEvent, ...]:
        """Group 的**实时聚合视图**（AD-13：Group 的持久化不靠这张表）。"""
        rows = await self._repository.list_for_collaboration(
            collaboration_id, after_sequence=after_sequence
        )
        return await self.native_storage.hydrate(rows)

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #

    async def purge_expired(self, *, now: datetime | None = None, exclude_conversations: Sequence[str] = ()) -> int:
        """v1.0 §8.5 保留期清理，返回删除条数。"""
        return await self._repository.purge_expired(now=now or self._clock(), exclude_conversations=exclude_conversations)

    async def purge_conversation(self, conversation_id: str) -> int:
        """明确删除会话时移除其全部本地记录；不删除引擎的原生会话。"""
        return await self._repository.delete_for_conversation(conversation_id)


__all__ = ["AppendOutcome", "DEFAULT_RETENTION", "EventStore"]
