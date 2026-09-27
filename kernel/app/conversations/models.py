"""Conversation 领域模型。

职责
----
定义「用户与一个 Agent Binding 的一条长期逻辑对话」。Conversation 是 Group 的
唯一成员抽象（N §9.1），也是 Card / External CLI 两个 Surface 的共同宿主
（v1.0 §4.5、D-07）。

对应规范
--------
- v1.0 §4.4：id / projectId / agentBindingId / title / nativeSessionId /
  nativeSessionHeadId / preferredSurface / modelId / providerId / reasoningMode /
  state / createdAt / updatedAt。
- v1.0 §8.8：通用 Conversation 只看到 ``native_session_id`` /
  ``native_session_head_id``（+ 可选 segments），原生的分段/续接概念不得泄露到公共层。
- N §9.5【替换】：Group 内新建的 Conversation 需要三组字段——

      origin:     standard | group_spawned
      visibility: project_visible | group_only
      retention:  persistent | decide_on_group_close | ephemeral

  建议默认（Group 内新建）：group_spawned / group_only / decide_on_group_close。
- N §9.7：``created_by_collaboration_id`` 记录是哪个 Group 拉起的。
- N §9.2：同一个 AgentBinding 可以在一个 Group 中同时启动多个 Conversation，
  每条都有独立 ``conversation_id`` / ``native_session_id`` / 事件流 / Runtime Lease。
- D-06：Conversation 创建后绑定一个 Agent Binding，不在中途静默替换。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.base import DomainModel
from app.errors import DomainInvariantError
from app.ids import (
    BindingId,
    CollaborationId,
    ConversationId,
    ProjectId,
    conversation_id,
)

Surface = Literal["card", "external-cli"]
"""v1.0 §4.5：Surface 只表示交互界面，不决定 Backend、也不决定 Model。"""

ConversationState = Literal[
    "idle", "running-card", "running-external", "paused", "ended", "error"
]
"""v1.0 §4.4。"""

ConversationOrigin = Literal["standard", "group_spawned"]
"""N §9.5。"""

ConversationVisibility = Literal["project_visible", "group_only"]
"""N §9.5。"""

ConversationRetention = Literal["persistent", "decide_on_group_close", "ephemeral"]
"""N §9.5。"""

TERMINAL_STATES: frozenset[str] = frozenset({"ended"})


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Conversation(DomainModel):
    """一条 Conversation。

    ``agent_binding_id`` 与 ``project_id`` 在创建后不可变（D-06：不在中途静默
    替换 Agent/Harness；换 Backend = 新建 Conversation）。
    """

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"id", "project_id", "agent_binding_id"}
    )

    id: ConversationId
    project_id: ProjectId
    agent_binding_id: BindingId
    title: str = Field(min_length=1)

    # --- 原生身份（v1.0 §8.8：只暴露这两个 + 可选 segments） ---------------- #
    native_session_id: str | None = None
    native_session_head_id: str | None = None
    native_session_segments: tuple[str, ...] = ()

    # --- Surface 与模型快照（v1.0 §4.4 / §7） ------------------------------ #
    preferred_surface: Surface = "card"
    model_id: str | None = None
    provider_id: str | None = None
    reasoning_mode: str | None = None
    execution_mode: str | None = Field(default=None, max_length=256)
    approval_mode: Literal["ask", "auto", "bypass", "read_only", "plan"] | None = None
    state: ConversationState = "idle"

    # --- N §9.5 Group 可见性与保留策略 ------------------------------------- #
    origin: ConversationOrigin = "standard"
    visibility: ConversationVisibility = "project_visible"
    retention: ConversationRetention = "persistent"
    created_by_collaboration_id: CollaborationId | None = None

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    archived_at: datetime | None = None

    @model_validator(mode="after")
    def _check_group_fields(self) -> Self:
        if self.origin == "standard" and self.created_by_collaboration_id is not None:
            raise ValueError(
                "origin=standard 的 Conversation 不应带 created_by_collaboration_id（N §9.5）"
            )
        if self.visibility == "group_only" and self.origin != "group_spawned":
            # N §9.5 只为 Group 内新建的 Conversation 定义 group_only；
            # 普通区新建后拖入 Group 的成员保持 project_visible（路径 A）。
            raise ValueError(
                "visibility=group_only 只适用于 origin=group_spawned 的 Conversation（N §9.5）"
            )
        if self.retention == "decide_on_group_close" and self.origin != "group_spawned":
            raise ValueError(
                "retention=decide_on_group_close 只适用于 Group 内新建的 Conversation（N §9.5）"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        agent_binding_id: str,
        title: str,
        preferred_surface: Surface = "card",
        model_id: str | None = None,
        provider_id: str | None = None,
        reasoning_mode: str | None = None,
        native_session_id: str | None = None,
        conversation_uuid: str | None = None,
        created_at: datetime | None = None,
    ) -> Conversation:
        """路径 A：普通区新建（N §9.3）。默认 standard / project_visible / persistent。"""
        timestamp = created_at or _now()
        return cls(
            id=conversation_id(conversation_uuid),
            project_id=project_id,
            agent_binding_id=agent_binding_id,
            title=title,
            native_session_id=native_session_id,
            preferred_surface=preferred_surface,
            model_id=model_id,
            provider_id=provider_id,
            reasoning_mode=reasoning_mode,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def create_group_spawned(
        cls,
        *,
        project_id: str,
        agent_binding_id: str,
        title: str,
        collaboration_id: str,
        preferred_surface: Surface = "card",
        model_id: str | None = None,
        provider_id: str | None = None,
        reasoning_mode: str | None = None,
        visibility: ConversationVisibility = "group_only",
        retention: ConversationRetention = "decide_on_group_close",
        conversation_uuid: str | None = None,
        created_at: datetime | None = None,
    ) -> Conversation:
        """路径 B：Group 内「启动新 Agent」（N §9.2 / §9.3 / §9.5）。

        底层与路径 A 完全同一套模型——只是三个策略字段取 N §9.5 的建议默认值。
        """
        timestamp = created_at or _now()
        return cls(
            id=conversation_id(conversation_uuid),
            project_id=project_id,
            agent_binding_id=agent_binding_id,
            title=title,
            preferred_surface=preferred_surface,
            model_id=model_id,
            provider_id=provider_id,
            reasoning_mode=reasoning_mode,
            origin="group_spawned",
            visibility=visibility,
            retention=retention,
            created_by_collaboration_id=collaboration_id,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def snapshot_model(
        self,
        *,
        model_id: str | None = None,
        provider_id: str | None = None,
        reasoning_mode: str | None = None,
        at: datetime | None = None,
    ) -> Conversation:
        """会话级模型/推理强度快照（v1.0 §7.3 + AD-12 / AD-114）。

        **快照即快照，不回溯**：这里改的只是这一条 Conversation 从此刻起用什么，
        既不动 Binding 的默认值，也不改这条会话已经发生过的事——「这一轮当时用的
        是哪个模型」由原生历史回答（D-16），不由这个字段追认。

        只覆盖显式给了的项：``None`` 表示「这次不改」。要把某项清回「跟随 Binding
        默认」，用 ``evolve`` 显式写 ``None``——两种意图不该挤在同一个入参里。
        """
        changes: dict[str, Any] = {"updated_at": at or _now()}
        if model_id is not None:
            changes["model_id"] = model_id
        if provider_id is not None:
            changes["provider_id"] = provider_id
        if reasoning_mode is not None:
            changes["reasoning_mode"] = reasoning_mode
        return self.evolve(**changes)

    def promote_to_project(self, *, at: datetime | None = None) -> Conversation:
        """N §9.5「保留到项目」：group_only → project_visible + persistent。"""
        return self.evolve(
            visibility="project_visible",
            retention="persistent",
            updated_at=at or _now(),
        )

    def bind_native_session(
        self,
        native_session_id: str,
        *,
        head_id: str | None = None,
        segments: tuple[str, ...] = (),
        at: datetime | None = None,
    ) -> Conversation:
        """绑定原生 Session（v1.0 §8.7：不得按「最新 Session」猜测并静默绑定）。"""
        if self.native_session_id is not None and self.native_session_id != native_session_id:
            raise DomainInvariantError(
                "Conversation 已绑定原生 Session，重绑必须显式解绑（v1.0 §8.7）"
            )
        return self.evolve(
            native_session_id=native_session_id,
            native_session_head_id=head_id or self.native_session_head_id,
            native_session_segments=segments or self.native_session_segments,
            updated_at=at or _now(),
        )

    @property
    def is_group_only(self) -> bool:
        return self.visibility == "group_only"

    @property
    def has_native_session(self) -> bool:
        """N §9 允许懒创建：Conversation 先成为规范对象，原生 Session 可后到。"""
        return self.native_session_id is not None


__all__ = [
    "Conversation",
    "ConversationOrigin",
    "ConversationRetention",
    "ConversationState",
    "ConversationVisibility",
    "Surface",
    "TERMINAL_STATES",
]
