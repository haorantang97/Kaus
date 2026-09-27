"""Event Store 服务：Session Host 与 ``event_store`` 表之间的一层薄门面。

职责
----
把 :class:`~runtime.event_envelope.AgentEventEnvelope` 变成带**显式保留期**的
:class:`~app.events.models.StoredEvent` 写进
:class:`~app.events.repository.EventStoreRepository`，并提供三件事：

1. **追加**（按 ``eventId`` 幂等）——断线重连会把同一段事件再推一遍，这在这张表
   里是正常流量而不是错误（v1.0 §8.5 / N §7.3 规则 9）；
2. **按 ``(conversationId, sequence)`` 范围读取**——WebSocket/SSE 的
   ``?after=<sequence>`` 续传游标就是这个（v1.0 §8.5）；
3. **按 ``expiresAt`` 清理**——保留期是这张表的定义性质，不是可选运维脚本。

不做什么
--------
- 不提供任何按内容检索的方法：那会诱使它变成第二本账（v1.0 §17 风险 9）。
  权威账本是 Backend 的原生 session 存储（D-16 / R-02）。
- 不做脱敏：写入方（Driver / Session Host）负责在入库前做与日志同级的脱敏，
  本层原样收下，不假装做过（v1.0 §8.5）。
- 不承诺内容可恢复性：整表可删，删完重开卡片从原生历史重建（v1.0 §11.3）。

保留期口径
----------
v1.0 §8.5 给的是**基线**：普通事件默认 7 天（:data:`~app.events.models.DEFAULT_RETENTION`），
诊断类事件更短（默认 24 小时）。本服务把它做成可配置项
（:attr:`EventStore.retention` / :attr:`EventStore.diagnostic_retention`），
默认值按基线 = **7 天**（AD-39；初版曾取 24 小时），诊断类 24 小时，且诊断类永远
不会比普通事件留得更久（取两者较小值）。想留更久只能把保留期往后放，**没有「永不过期」的取值**。
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

#: 本服务的默认保留期。v1.0 §8.5 / AD-39：普通事件 run 结束后默认 7 天，可配置；
#: 诊断类 24 小时（见 app.events.models.DIAGNOSTIC_RETENTION）。
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
        """这条事件什么时候过期。

        AD-162：保留期是 **(事件类别, 这条 Binding 的 backend 有没有
        ``sessions.history``)** 的函数，而不只是事件类别的函数。

        ``history_recoverable`` 为假时（引擎自己**不提供**可回放的原生历史，
        ACP 全家就是这样：Driver 对 ``load_native_history`` 明确抛
        ``UnsupportedCapabilityError``），站内事件是这条会话**唯一**的历史来源，
        按「可重建的缓存」清掉它等于删掉用户的历史。因此正文类事件
        （:func:`~app.events.models.is_content_event`）改为留到会话本身被删除或
        归档为止；**诊断类照旧过期**——这一条不是「什么都不删了」。

        ``None`` = 不知道（没接线的调用方、老代码路径），按老口径走 TTL。
        """
        event = envelope.event
        namespace = getattr(event, "namespace", None)
        name = getattr(event, "name", None)
        if history_recoverable is False and is_content_event(
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

        ``history_recoverable``（AD-162）：这条会话背后的引擎有没有可回放的原生
        历史。为假时正文类事件不再按 TTL 过期，见 :meth:`expires_at_for`。
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
        """按去重键取一行；不存在返回 ``None``。"""
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
        return tuple(selected)

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
        return tuple(
            await self._repository.list_for_collaboration(
                collaboration_id, after_sequence=after_sequence
            )
        )

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """v1.0 §8.5 保留期清理，返回删除条数。"""
        return await self._repository.purge_expired(now=now or self._clock())

    async def purge_conversation(self, conversation_id: str) -> int:
        """丢弃一条 Conversation 的全部缓冲，返回删除条数。

        两个用处：v1.0 §11.3「整表可删、内容仍可从原生历史重建」的按会话版本；
        以及 ``DELETE /api/conversations/{id}`` —— 会话都不在了，它的重放缓冲
        留着只会让 ``?after=`` 重放出一条已删会话的历史。

        与 :meth:`purge_expired` 同一个动词：两者都是「按某个条件把行删掉」，
        叫法一致才不会让人以为其中一个是软删除。
        """
        return await self._repository.delete_for_conversation(conversation_id)


__all__ = ["AppendOutcome", "DEFAULT_RETENTION", "EventStore"]
