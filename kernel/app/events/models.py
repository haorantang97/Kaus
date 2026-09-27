"""Event Store 的领域模型（v1.0 §11.1 ``event_store``）。

职责
----
把一条 :class:`~runtime.event_envelope.AgentEventEnvelope` 包成可持久化的一行，
并给它一个**必填的过期时间**。

为什么 ``expires_at`` 必填
--------------------------
v1.0 §8.5 明确要求：「必须有显式保留策略，不允许『不要无期限保存』这种无策略
表述」。因此过期时间不是可空字段、也没有「永不过期」的取值——想留久一点只能
把 ``expires_at`` 往后放，不能不填。AD-13 进一步把 Group 时间线从这套保留期里
摘了出去（Group 消息归 Group 自己，见
:class:`~app.collaboration.models.CollaborationMessage`），所以本表过期删除
不会削掉 Group 的历史。

对应规范
--------
- v1.0 §11.1 ``event_store``：event_id（去重键）/ conversation_id / sequence /
  collaboration_session_id / envelope_json / native_event_id / expires_at /
  created_at。
- v1.0 §8.5：保留期基线 —— run 结束后默认 7 天；诊断类原始事件默认 24 小时。
- D-16 / AD-13 / v1.0 §11.3：Event Store 是短期重放缓冲 + 渲染缓存，
  永不构成第二本权威账，不参与冲突仲裁。
- v1.0 §8.5：写入前脱敏，不得存入 Secret（本模块不做脱敏本身——那是写入方的
  职责——但把这条约束写进契约，见 :meth:`StoredEvent.from_envelope` 的注意事项）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, Final, Self

from pydantic import Field, model_validator

from app.base import DomainModel
from app.ids import CollaborationId, ConversationId
from runtime.event_envelope import AgentEventEnvelope

DEFAULT_RETENTION: Final[timedelta] = timedelta(days=7)
"""v1.0 §8.5 基线：run 结束后 N 天过期，默认 7 天（可配置）。"""

DIAGNOSTIC_RETENTION: Final[timedelta] = timedelta(hours=24)
"""v1.0 §8.5 基线：诊断类原始事件保留期更短，默认 24 小时。"""

#: 走短保留期的事件类型。其余按 :data:`DEFAULT_RETENTION`。
DIAGNOSTIC_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"diagnostic.notice", "extension.event"}
)

#: 产品自己的扩展事件 namespace（AD-86 / AD-143）。
PRODUCT_NAMESPACE: Final[str] = "kaus"

#: :data:`PRODUCT_NAMESPACE` 下**属于时间线正文**的扩展事件名（AD-143）。
#:
#: 信封 v1.1 冻结之后，产品自己要落进时间线的东西只能走 ``extension.event``
#: （``kaus/user.message`` 是 AD-86 定的第一条）。但 ``extension.event`` 整类
#: 此前被算作「无法锚定原生消息的短期诊断数据」，于是**用户自己说的那句话**
#: 按 24 小时清理，而同一轮的 ``run.*`` / ``message.*`` 留 7 天——超过一天再
#: 打开这条会话，就只剩助手在自言自语。这不是保留期该表达的意思：正文就是正文，
#: 走哪个信封字段是编码细节，不改变它的保留期。
#:
#: AD-155（批次三十一）把 ``model.adopted`` 也放进来：它记的是「这条会话从这一刻
#: 起改按引擎的当前模型跑」——和用户那句话一样是**正文**，一天以后重开会话仍该
#: 看得见，否则时间线上只剩一个无从解释的模型变化。
#: R6（批次三十七）把 ``user.message.failed`` 也放进来：它是**那条用户消息的状态**，
#: 不是一条诊断。按诊断期清掉，「刷新之后仍然看得见这句话没被处理」这件事就只成立
#: 一天，而它要解释的正是「这条历史为什么和别的不一样」。
PRODUCT_CONTENT_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {"user.message", "model.adopted", "user.message.failed"}
)


#: 时间线**正文**类核心事件的类型前缀（AD-162）。
#:
#: 这三类合起来就是「重开这条会话时用户要看见的东西」：助手正文（``message.*``）、
#: 工具卡（``tool.*``）与回合的生命周期（``run.*``，少了它时间线连一轮的边界都画
#: 不出来）。其余核心事件（``usage.*`` / ``permission.*`` / ``diagnostic.*`` …）
#: 不在此列——它们要么是过程数据，要么早就闭合了。
CONTENT_EVENT_TYPE_PREFIXES: Final[tuple[str, ...]] = ("message.", "tool.", "run.")

#: 「留到会话本身消失为止」的过期时刻（AD-162）。
#:
#: v1.0 §8.5 不接受「无保留策略」这种表述，所以这里**不是**把 ``expires_at`` 改成
#: 可空，而是给出一个明确的、写得进库也读得回来的时刻：这批事件的保留期就是
#: **这条 Conversation 的生命周期**，它由 ``DELETE /api/conversations/{id}``
#: （AD-105 的 ``purge_conversation``）与归档路径负责回收，不由 TTL 回收。
RETAIN_UNTIL_CONVERSATION_GONE: Final[datetime] = datetime(
    9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc
)


def is_content_event(
    event_type: str, *, namespace: str | None = None, name: str | None = None
) -> bool:
    """这条事件是不是「时间线正文」（AD-162）。

    正文 = 助手说的话、工具卡、回合边界，加上产品自己那两条借道 ``extension.event``
    的正文（``kaus/user.message`` / ``kaus/model.adopted``，AD-86 / AD-155）。
    诊断与 Backend 私有的未知扩展帧**不算**——它们照旧按诊断期过期。
    """
    if event_type.startswith(CONTENT_EVENT_TYPE_PREFIXES):
        return True
    return (
        event_type == "extension.event"
        and namespace == PRODUCT_NAMESPACE
        and name in PRODUCT_CONTENT_EVENT_NAMES
    )


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def retention_for(event_type: str, *, namespace: str | None = None,
                  name: str | None = None) -> timedelta:
    """v1.0 §8.5 的保留期基线：诊断类更短，其余走默认。

    ``namespace`` / ``name`` 只对 ``extension.event`` 有意义：产品自己的正文类
    扩展事件（见 :data:`PRODUCT_CONTENT_EVENT_NAMES`）按普通保留期，其余扩展
    事件仍按诊断期——那些是 Backend 私有的未知帧，正是 v1.0 §8.5 说的短期诊断
    数据。不给这两个参数时按老口径（整类 ``extension.event`` 走诊断期）。
    """
    if (
        event_type == "extension.event"
        and namespace == PRODUCT_NAMESPACE
        and name in PRODUCT_CONTENT_EVENT_NAMES
    ):
        return DEFAULT_RETENTION
    return (
        DIAGNOSTIC_RETENTION
        if event_type in DIAGNOSTIC_EVENT_TYPES
        else DEFAULT_RETENTION
    )


def retention_for_envelope(envelope: AgentEventEnvelope) -> timedelta:
    """按信封判保留期——比只看 ``event.type`` 多认得出正文类扩展事件。"""
    event = envelope.event
    return retention_for(
        event.type,
        namespace=getattr(event, "namespace", None),
        name=getattr(event, "name", None),
    )


class StoredEvent(DomainModel):
    """Event Store 的一行。

    ``event_id`` / ``conversation_id`` / ``sequence`` / ``native_event_id``
    是从 Envelope 里提出来的**索引列**（v1.0 §11.1 就是这么列的），
    因此模型层强制它们与 ``envelope`` 一致——避免出现「索引列说 A、
    envelope 里写 B」的两套真相。
    """

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"event_id", "conversation_id", "sequence"}
    )

    #: 去重键（v1.0 §11.1）：重放同一条事件必须幂等。
    event_id: str = Field(min_length=1)
    conversation_id: ConversationId
    #: conversation 内单调递增；WS ``?after=<sequence>`` 的续传游标。
    sequence: int = Field(ge=0)
    #: 可空；Group 时间线聚合多条 Conversation 的事件时使用（v1.0 §11.1）。
    collaboration_session_id: CollaborationId | None = None
    envelope: AgentEventEnvelope
    #: 可空；用于锚定原生消息。无法锚定的事件按 v1.0 §8.5 视为短期诊断数据。
    native_event_id: str | None = None
    #: **必填**：v1.0 §8.5 不接受「无保留策略」。
    expires_at: datetime
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_matches_envelope(self) -> Self:
        if self.event_id != self.envelope.event_id:
            raise ValueError("event_id 必须与 envelope.eventId 一致（v1.0 §11.1）")
        if self.conversation_id != self.envelope.conversation_id:
            raise ValueError("conversation_id 必须与 envelope.conversationId 一致")
        if self.sequence != self.envelope.sequence:
            raise ValueError("sequence 必须与 envelope.sequence 一致")
        if self.native_event_id != self.envelope.native_event_id:
            raise ValueError("native_event_id 必须与 envelope.nativeEventId 一致")
        return self

    @classmethod
    def from_envelope(
        cls,
        envelope: AgentEventEnvelope,
        *,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        collaboration_session_id: str | None = None,
    ) -> StoredEvent:
        """按 v1.0 §8.5 的保留期基线包一条 Envelope。

        注意：**脱敏是写入方的职责**（v1.0 §8.5：写入前执行与日志同级的脱敏，
        不得存入 Secret）。本方法原样收下传进来的 Envelope，不做也不假装做脱敏。
        """
        at = now or _now()
        return cls(
            event_id=envelope.event_id,
            conversation_id=envelope.conversation_id,
            sequence=envelope.sequence,
            collaboration_session_id=collaboration_session_id,
            envelope=envelope,
            native_event_id=envelope.native_event_id,
            expires_at=expires_at or (at + retention_for_envelope(envelope)),
            created_at=at,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.expires_at

    @property
    def is_anchored_to_native(self) -> bool:
        """v1.0 §8.5：能锚定原生消息 ID 的事件才不算「短期诊断数据」。"""
        return self.native_event_id is not None


__all__ = [
    "CONTENT_EVENT_TYPE_PREFIXES",
    "DEFAULT_RETENTION",
    "DIAGNOSTIC_EVENT_TYPES",
    "DIAGNOSTIC_RETENTION",
    "RETAIN_UNTIL_CONVERSATION_GONE",
    "is_content_event",
    "PRODUCT_CONTENT_EVENT_NAMES",
    "PRODUCT_NAMESPACE",
    "StoredEvent",
    "retention_for",
    "retention_for_envelope",
]
