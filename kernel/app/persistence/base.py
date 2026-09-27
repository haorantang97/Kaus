"""一整套 Repository 的装配结果。

为什么需要它
------------
v1.0 §11.1 列了十二张表；上层（Session Host / API 层）需要的是「一套齐活的
Repository」，而不是十二个各自 new 出来的对象。:class:`RepositorySet` 把它们打成
一个包，同时给契约测试一个可以参数化的统一入口：同一份测试代码，一次喂内存
实现、一次喂 SQLite 实现。

字段名与 v1.0 §11.1 的表一一对应，方便对照：

======================  ====================================
字段                     表
======================  ====================================
``projects``            ``projects``
``capabilities``        ``project_capabilities``
``backends``            ``backends``
``bindings``            ``agent_bindings``
``conversations``       ``conversations``
``leases``              ``runtime_leases``
``terminal_launches``   ``terminal_launches``
``group_materials``       ``group_materials``
``collaborations``      ``collaboration_sessions``
``members``             ``collaboration_members``
``group_messages``      ``collaboration_messages``
``events``              ``event_store``
======================  ====================================

``conversation_messages`` / ``conversation_events`` 不在其中：v1.0 §11.1 已把它们
降级为**可选缓存表**，AD-22 进一步裁定「在 live 探针给出结论之前不建」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from app.collaboration.settings import AppSettingsRepository, InMemoryAppSettingsRepository

from app.capabilities.projection_records import ProjectionResultRepository
from app.capabilities.repository import ProjectCapabilityRepository
from app.collaboration.repository import (
    CollaborationMemberRepository,
    CollaborationMessageRepository,
    CollaborationSessionRepository,
)
from app.conversations.repository import ConversationRepository
from app.events.repository import EventStoreRepository
from app.projects.repository import (
    AgentBindingRepository,
    BackendRepository,
    ProjectRepository,
)
from app.runtimes.repository import RuntimeLeaseRepository, TerminalLaunchRepository
from app.collaboration.materials import GroupMaterialsRepository


#: v1.0 §11.1 **推荐**的十二张表（不含 Phase 5 新增的 ``projection_results``）。
#: 契约测试用它证明「推荐表一张不缺」，同时看得出后来加了哪些。
RECOMMENDED_TABLES: frozenset[str] = frozenset(
    {
        "projects",
        "project_capabilities",
        "backends",
        "agent_bindings",
        "conversations",
        "runtime_leases",
        "terminal_launches",
        "group_materials",
        "collaboration_sessions",
        "collaboration_members",
        "collaboration_messages",
        "event_store",
    }
)


@dataclass(frozen=True)
class RepositorySet:
    """v1.0 §11.1 十二张推荐表 + Phase 5 新增表的 Repository 装配结果。"""

    projects: ProjectRepository
    capabilities: ProjectCapabilityRepository
    backends: BackendRepository
    bindings: AgentBindingRepository
    conversations: ConversationRepository
    leases: RuntimeLeaseRepository
    terminal_launches: TerminalLaunchRepository
    group_materials: GroupMaterialsRepository
    collaborations: CollaborationSessionRepository
    members: CollaborationMemberRepository
    group_messages: CollaborationMessageRepository
    events: EventStoreRepository
    #: Phase 5（AD-149）：v1.0 §11.1 推荐表之外新增的一张——每次物化留一行账。
    projections: ProjectionResultRepository
    settings: AppSettingsRepository = field(default_factory=InMemoryAppSettingsRepository)


#: :class:`RepositorySet` 的字段名 → 它对应的 v1.0 §11.1 表名。
#: 契约测试用它证明「十二张推荐表一张不缺」。
REPOSITORY_TABLES: dict[str, str] = {
    "projects": "projects",
    "capabilities": "project_capabilities",
    "backends": "backends",
    "bindings": "agent_bindings",
    "conversations": "conversations",
    "leases": "runtime_leases",
    "terminal_launches": "terminal_launches",
    "group_materials": "group_materials",
    "collaborations": "collaboration_sessions",
    "members": "collaboration_members",
    "group_messages": "collaboration_messages",
    "events": "event_store",
    # v1.0 §11.1 推荐的十二张之外，Phase 5 新增的一张（AD-149）。
    "projections": "projection_results",
    "settings": "app_settings",
}


__all__ = ["RECOMMENDED_TABLES", "REPOSITORY_TABLES", "RepositorySet"]
