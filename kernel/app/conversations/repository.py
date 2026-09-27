"""Conversation 的 Repository 接口与内存参考实现。

对应规范
--------
- v1.0 §11.1 ``conversations``。
- N §9.5：``visibility`` 决定 Conversation 是否进入 Project Tree 的常用列表，
  因此 :meth:`ConversationRepository.list_for_project` 默认只返回
  ``project_visible``；Group 视图用 :meth:`list_for_collaboration`。
- R-02：仪表盘默认不存对话记录，原生 session 存储是唯一账本。因此本接口
  **只**管理 Conversation 元数据与原生 Session 映射，不提供任何消息读写方法。
- v1.0 §8.7：原生 Session 映射必须确定，禁止按「最新 Session」猜测 →
  :meth:`get_by_native_session`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from app.conversations.models import Conversation
from app.errors import DomainInvariantError

#: :meth:`ConversationRepository.list_recent` 不传 ``limit`` 时的默认条数。
#: 侧栏一屏装不下 50 条，取这个数是为了「默认调用绝不退化成全表」。
DEFAULT_RECENT_LIMIT = 50

#: ``limit`` 的上限。再大就不是列表而是导出了，那该是另一个端点。
MAX_RECENT_LIMIT = 500


@runtime_checkable
class ConversationRepository(Protocol):
    """v1.0 §11.1 ``conversations``。"""

    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def list_for_project(
        self, project_id: str, *, include_group_only: bool = False, archived: bool = False
    ) -> Sequence[Conversation]:
        """N §9.5：默认不返回 ``group_only``，避免 Group 批量拉起污染项目树。"""
        ...

    async def list_for_binding(self, agent_binding_id: str) -> Sequence[Conversation]:
        """N §9.2：同一 Binding 可以有多条并行 Conversation。"""
        ...

    async def list_recent(
        self,
        *,
        updated_after: datetime | None = None,
        project_id: str | None = None,
        limit: int = DEFAULT_RECENT_LIMIT,
        include_group_only: bool = False,
        archived: bool = False,
    ) -> Sequence[Conversation]:
        """**跨项目**的最近活动列表，按 ``updated_at`` 倒序（批次八第 1 件）。

        侧栏要的是「最近在动的会话」，与「某个项目下的会话」是两个问题：
        前者的天然形状是一次跨项目取前 N，用
        :meth:`list_for_project` 拼出来就是 1 + N 次查询，项目一多就成了
        30s 一次的 N+1 轮询。

        ``updated_after`` 是**增量刷新**的游标（严格大于），``project_id``
        把同一个端点收窄成单项目视图。``include_group_only`` 与
        :meth:`list_for_project` 同义：N §9.5 默认不把 Group 拉起的会话
        混进项目列表。
        """
        ...

    async def list_for_collaboration(
        self, collaboration_id: str
    ) -> Sequence[Conversation]:
        """由某个 Group 拉起的 Conversation（``created_by_collaboration_id``）。"""
        ...

    async def get_by_native_session(
        self, agent_binding_id: str, native_session_id: str
    ) -> Conversation | None:
        """v1.0 §8.7 / N §13.3：映射必须确定，断线重连不得创建重复 Conversation。"""
        ...

    async def save(self, conversation: Conversation) -> Conversation: ...

    async def delete(self, conversation_id: str) -> None:
        """v1.0 §16.6：删除 Conversation 不删除原生 Session。"""
        ...


class InMemoryConversationRepository:
    """参考实现。"""

    def __init__(self, conversations: Sequence[Conversation] = ()) -> None:
        self._by_id: dict[str, Conversation] = {c.id: c for c in conversations}

    async def get(self, conversation_id: str) -> Conversation | None:
        return self._by_id.get(conversation_id)

    async def list_for_project(
        self, project_id: str, *, include_group_only: bool = False, archived: bool = False
    ) -> Sequence[Conversation]:
        return tuple(
            c
            for c in self._by_id.values()
            if c.project_id == project_id
            and (c.archived_at is not None) == archived
            and (include_group_only or c.visibility == "project_visible")
        )

    async def list_for_binding(self, agent_binding_id: str) -> Sequence[Conversation]:
        return tuple(
            c for c in self._by_id.values() if c.agent_binding_id == agent_binding_id
        )

    async def list_recent(
        self,
        *,
        updated_after: datetime | None = None,
        project_id: str | None = None,
        limit: int = DEFAULT_RECENT_LIMIT,
        include_group_only: bool = False,
        archived: bool = False,
    ) -> Sequence[Conversation]:
        selected = [
            c
            for c in self._by_id.values()
            if (project_id is None or c.project_id == project_id)
            and (c.archived_at is not None) == archived
            and (include_group_only or c.visibility == "project_visible")
            and (updated_after is None or c.updated_at > updated_after)
        ]
        # 倒序按 updated_at，再按 id 断同刻的平局——和 sqlite 实现同一口径。
        selected.sort(key=lambda c: (c.updated_at, c.id), reverse=True)
        return tuple(selected[: max(0, limit)])

    async def list_for_collaboration(
        self, collaboration_id: str
    ) -> Sequence[Conversation]:
        return tuple(
            c
            for c in self._by_id.values()
            if c.created_by_collaboration_id == collaboration_id
        )

    async def get_by_native_session(
        self, agent_binding_id: str, native_session_id: str
    ) -> Conversation | None:
        for conversation in self._by_id.values():
            if (
                conversation.agent_binding_id == agent_binding_id
                and conversation.native_session_id == native_session_id
            ):
                return conversation
        return None

    async def save(self, conversation: Conversation) -> Conversation:
        if conversation.native_session_id is not None:
            existing = await self.get_by_native_session(
                conversation.agent_binding_id, conversation.native_session_id
            )
            if existing is not None and existing.id != conversation.id:
                raise DomainInvariantError(
                    "同一 Binding 下一个原生 Session 只能映射一条 Conversation"
                    f"（v1.0 §8.7）：{conversation.native_session_id!r}"
                )
        self._by_id[conversation.id] = conversation
        return conversation

    async def delete(self, conversation_id: str) -> None:
        self._by_id.pop(conversation_id, None)


__all__ = [
    "DEFAULT_RECENT_LIMIT",
    "MAX_RECENT_LIMIT",
    "ConversationRepository",
    "InMemoryConversationRepository",
]
