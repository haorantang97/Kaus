"""CollaborationSession / CollaborationMember 的 Repository 接口与内存参考实现。

对应规范
--------
- v1.0 §11.1 ``collaboration_sessions`` / ``collaboration_members``。
- N §9.4：UI 与 API 从第一天支持成员列表动态变化，不得把 Group 成员固化为
  创建时快照 → 成员是独立可增删的实体，没有任何「成员列表快照」形态的接口。
- N §9.9：一条 Conversation 可参加多个 Group →
  :meth:`CollaborationMemberRepository.list_for_conversation`。
- N §9.2：同一个 AgentBinding 可以在一个 Group 中同时启动多个 Conversation →
  唯一性只按 ``conversation_id`` 判定。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from app.collaboration.models import (
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
    assert_member_set_valid,
)
from app.errors import DomainInvariantError


@runtime_checkable
class CollaborationSessionRepository(Protocol):
    """v1.0 §11.1 ``collaboration_sessions``。"""

    async def get(self, collaboration_id: str) -> CollaborationSession | None: ...

    async def list_active(self) -> Sequence[CollaborationSession]: ...

    async def list_all(self) -> Sequence[CollaborationSession]:
        """含已关闭 / 已归档的全部 Group（``GET /api/groups?status=all``）。

        与 :meth:`list_active` 分成两个方法而不是加一个 ``include_closed`` 开关：
        右下角浮窗要的是「现在开着的组」，历史视图要的是「所有组」，两个问题
        各有各的天然形状，也各有各的排序（前者按创建时间，后者仍按创建时间但
        会带上一堆 ``closed_at``）。
        """
        ...

    async def list_for_project(self, project_id: str) -> Sequence[CollaborationSession]: ...

    async def save(self, session: CollaborationSession) -> CollaborationSession: ...


@runtime_checkable
class CollaborationMemberRepository(Protocol):
    """v1.0 §11.1 ``collaboration_members``。"""

    async def get(self, member_id: str) -> CollaborationMember | None: ...

    async def list_for_collaboration(
        self, collaboration_id: str, *, include_left: bool = False
    ) -> Sequence[CollaborationMember]: ...

    async def list_for_conversation(
        self, conversation_id: str
    ) -> Sequence[CollaborationMember]:
        """N §9.9：一条 Conversation 可参加多个 Group。"""
        ...

    async def save(self, member: CollaborationMember) -> CollaborationMember: ...

    async def delete(self, member_id: str) -> None:
        """N §9.4：成员移除后原 Conversation 与 Native Session 不被删除。"""
        ...


class InMemoryCollaborationSessionRepository:
    """参考实现。"""

    def __init__(self, sessions: Sequence[CollaborationSession] = ()) -> None:
        self._by_id: dict[str, CollaborationSession] = {s.id: s for s in sessions}

    async def get(self, collaboration_id: str) -> CollaborationSession | None:
        return self._by_id.get(collaboration_id)

    async def list_active(self) -> Sequence[CollaborationSession]:
        return tuple(
            sorted(
                (s for s in self._by_id.values() if s.status in ("active", "minimized")),
                key=lambda s: (s.created_at, s.id),
            )
        )

    async def list_all(self) -> Sequence[CollaborationSession]:
        return tuple(
            sorted(self._by_id.values(), key=lambda s: (s.created_at, s.id))
        )

    async def list_for_project(self, project_id: str) -> Sequence[CollaborationSession]:
        return tuple(s for s in self._by_id.values() if s.home_project_id == project_id)

    async def save(self, session: CollaborationSession) -> CollaborationSession:
        self._by_id[session.id] = session
        return session


class InMemoryCollaborationMemberRepository:
    """参考实现。"""

    def __init__(self, members: Sequence[CollaborationMember] = ()) -> None:
        self._by_id: dict[str, CollaborationMember] = {m.id: m for m in members}

    async def get(self, member_id: str) -> CollaborationMember | None:
        return self._by_id.get(member_id)

    async def list_for_collaboration(
        self, collaboration_id: str, *, include_left: bool = False
    ) -> Sequence[CollaborationMember]:
        return tuple(
            m
            for m in self._by_id.values()
            if m.collaboration_session_id == collaboration_id
            and (include_left or m.participation_state != "left")
        )

    async def list_for_conversation(
        self, conversation_id: str
    ) -> Sequence[CollaborationMember]:
        return tuple(
            m for m in self._by_id.values() if m.conversation_id == conversation_id
        )

    async def save(self, member: CollaborationMember) -> CollaborationMember:
        candidate = dict(self._by_id)
        candidate[member.id] = member
        assert_member_set_valid(
            [
                m
                for m in candidate.values()
                if m.collaboration_session_id == member.collaboration_session_id
                and m.participation_state != "left"
            ]
        )
        self._by_id = candidate
        return member

    async def delete(self, member_id: str) -> None:
        self._by_id.pop(member_id, None)


@runtime_checkable
class CollaborationMessageRepository(Protocol):
    """v1.0 §11.1 ``collaboration_messages``（AD-13：Group 拥有的时间线数据）。"""

    async def get(self, message_id: str) -> CollaborationMessage | None: ...

    async def list_for_collaboration(
        self,
        collaboration_id: str,
        *,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Sequence[CollaborationMessage]:
        """按 ``sequence`` 升序返回；``after_sequence`` 是浮窗续传游标。

        ``before_sequence``（批次二十六）是**开区间**上界，给
        ``GET /api/groups/{id}/messages?before=`` 用的向前翻页游标：时间线端点
        按时间倒序给最近 N 条，再往前翻就是「比这个 sequence 更早的那一页」。

        ``limit`` 的语义是「**最近**的 N 条」（sequence 最大的 N 条），返回时
        仍按升序排列。定成「最近」而不是「最早」，是因为它唯一的调用方是向前
        翻页的时间线——定成「最早的 N 条」会让每一页都从组的开头重新数一遍。
        """
        ...

    async def next_sequence(self, collaboration_id: str) -> int:
        """该 Group 下一条消息应使用的 sequence。"""
        ...

    async def append(self, message: CollaborationMessage) -> CollaborationMessage:
        """追加一条。同一 Group 内 ``sequence`` 唯一。"""
        ...

    async def delete_for_collaboration(self, collaboration_id: str) -> int:
        """Group 关闭并归档后清空其时间线；返回删除条数。"""
        ...


class InMemoryCollaborationMessageRepository:
    """参考实现。"""

    def __init__(self, messages: Sequence[CollaborationMessage] = ()) -> None:
        self._by_id: dict[str, CollaborationMessage] = {m.id: m for m in messages}

    async def get(self, message_id: str) -> CollaborationMessage | None:
        return self._by_id.get(message_id)

    async def list_for_collaboration(
        self,
        collaboration_id: str,
        *,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Sequence[CollaborationMessage]:
        selected = [
            m
            for m in self._by_id.values()
            if m.collaboration_session_id == collaboration_id
            and (after_sequence is None or m.sequence > after_sequence)
            and (before_sequence is None or m.sequence < before_sequence)
            and (kinds is None or m.kind in tuple(kinds))
        ]
        ordered = sorted(selected, key=lambda m: m.sequence)
        if limit is None:
            return tuple(ordered)
        # limit=0 就是「一条都不要」；`ordered[-0:]` 会给出整个列表，所以显式挡一道。
        return tuple(ordered[-limit:]) if limit > 0 else ()

    async def next_sequence(self, collaboration_id: str) -> int:
        existing = await self.list_for_collaboration(collaboration_id)
        return (existing[-1].sequence + 1) if existing else 0

    async def append(self, message: CollaborationMessage) -> CollaborationMessage:
        for existing in self._by_id.values():
            if (
                existing.collaboration_session_id == message.collaboration_session_id
                and existing.sequence == message.sequence
                and existing.id != message.id
            ):
                raise DomainInvariantError(
                    "同一 Group 内 sequence 必须唯一："
                    f"{message.collaboration_session_id!r}#{message.sequence}"
                )
        self._by_id[message.id] = message
        return message

    async def delete_for_collaboration(self, collaboration_id: str) -> int:
        doomed = [
            m.id
            for m in self._by_id.values()
            if m.collaboration_session_id == collaboration_id
        ]
        for message_id in doomed:
            del self._by_id[message_id]
        return len(doomed)


__all__ = [
    "CollaborationMemberRepository",
    "CollaborationMessageRepository",
    "CollaborationSessionRepository",
    "InMemoryCollaborationMemberRepository",
    "InMemoryCollaborationMessageRepository",
    "InMemoryCollaborationSessionRepository",
]
