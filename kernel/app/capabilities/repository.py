"""Project Capability 的 Repository 接口与内存参考实现。

职责
----
提供 Resolver 所需的赋值读取边界：按 Project 取赋值、按祖先链批量取赋值。

对应规范
--------
- v1.0 §11.1 ``project_capabilities``。
- R-01：backend-scoped 能力类型（``<backend>:<name>``）与通用能力共用**同一张表、
  同一个 Registry**，因此这里没有任何按 backend 分表/分接口的设计；
  按 backend 过滤只发生在 Resolver 输出侧。
- v1.0 §5.2：同一 (project, capability_type, capability_id) 只应有一条赋值。
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from app.capabilities.models import ProjectCapability
from app.errors import DomainInvariantError


@runtime_checkable
class ProjectCapabilityRepository(Protocol):
    """v1.0 §11.1 ``project_capabilities``。"""

    async def get(self, assignment_id: str) -> ProjectCapability | None: ...

    async def list_for_project(self, project_id: str) -> Sequence[ProjectCapability]: ...

    async def list_for_projects(
        self, project_ids: Sequence[str]
    ) -> Mapping[str, Sequence[ProjectCapability]]:
        """按祖先链批量取，直接喂给 Capability Resolver（R-01 树计算层）。"""
        ...

    async def save(self, assignment: ProjectCapability) -> ProjectCapability:
        """同一 (project, type, capability_id) 唯一；重复写入即覆盖。"""
        ...

    async def delete(self, assignment_id: str) -> None: ...


class InMemoryProjectCapabilityRepository:
    """参考实现。"""

    def __init__(self, assignments: Sequence[ProjectCapability] = ()) -> None:
        self._by_id: dict[str, ProjectCapability] = {}
        for assignment in assignments:
            self._put(assignment)

    def _natural_key(self, assignment: ProjectCapability) -> tuple[str, str, str]:
        return (assignment.project_id, assignment.capability_type, assignment.capability_id)

    def _put(self, assignment: ProjectCapability) -> None:
        natural = self._natural_key(assignment)
        for existing_id, existing in list(self._by_id.items()):
            if self._natural_key(existing) == natural and existing_id != assignment.id:
                del self._by_id[existing_id]
        self._by_id[assignment.id] = assignment

    async def get(self, assignment_id: str) -> ProjectCapability | None:
        return self._by_id.get(assignment_id)

    async def list_for_project(self, project_id: str) -> Sequence[ProjectCapability]:
        return tuple(a for a in self._by_id.values() if a.project_id == project_id)

    async def list_for_projects(
        self, project_ids: Sequence[str]
    ) -> Mapping[str, Sequence[ProjectCapability]]:
        return {pid: await self.list_for_project(pid) for pid in project_ids}

    async def save(self, assignment: ProjectCapability) -> ProjectCapability:
        if assignment.assignment_mode == "block" and assignment.config:
            raise DomainInvariantError("block 赋值不得携带 config")
        self._put(assignment)
        return assignment

    async def delete(self, assignment_id: str) -> None:
        self._by_id.pop(assignment_id, None)


__all__ = [
    "InMemoryProjectCapabilityRepository",
    "ProjectCapabilityRepository",
]
