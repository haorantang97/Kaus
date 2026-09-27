"""内存实现的装配点。

参考实现，不是生产存储：进程退出即消失。用于契约测试的对照组、Phase 1 迁移器
脚手架，以及任何「我只想要一套空仓库」的场合。
"""

from __future__ import annotations

from app.capabilities.projection_records import InMemoryProjectionResultRepository
from app.capabilities.repository import InMemoryProjectCapabilityRepository
from app.collaboration.repository import (
    InMemoryCollaborationMemberRepository,
    InMemoryCollaborationMessageRepository,
    InMemoryCollaborationSessionRepository,
)
from app.conversations.repository import InMemoryConversationRepository
from app.events.repository import InMemoryEventStoreRepository
from app.persistence.base import RepositorySet
from app.projects.repository import (
    InMemoryAgentBindingRepository,
    InMemoryBackendRepository,
    InMemoryProjectRepository,
)
from app.runtimes.repository import (
    InMemoryRuntimeLeaseRepository,
    InMemoryTerminalLaunchRepository,
)
from app.collaboration.materials import InMemoryGroupMaterialsRepository


def in_memory_repository_set() -> RepositorySet:
    """新建一套彼此独立的空内存仓库。"""
    return RepositorySet(
        projects=InMemoryProjectRepository(),
        capabilities=InMemoryProjectCapabilityRepository(),
        backends=InMemoryBackendRepository(),
        bindings=InMemoryAgentBindingRepository(),
        conversations=InMemoryConversationRepository(),
        leases=InMemoryRuntimeLeaseRepository(),
        terminal_launches=InMemoryTerminalLaunchRepository(),
        group_materials=InMemoryGroupMaterialsRepository(),
        collaborations=InMemoryCollaborationSessionRepository(),
        members=InMemoryCollaborationMemberRepository(),
        group_messages=InMemoryCollaborationMessageRepository(),
        events=InMemoryEventStoreRepository(),
        projections=InMemoryProjectionResultRepository(),
    )


__all__ = ["in_memory_repository_set"]
