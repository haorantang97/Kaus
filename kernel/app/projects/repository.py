"""Project / Backend / AgentBinding 的 Repository 接口与内存参考实现。

职责
----
定义持久化边界。领域层只依赖这些 Protocol，具体存储（v1.0 §11 建议的 SQLite
领域数据库）在后续阶段实现。这里附带的 ``InMemory*Repository`` 是**参考实现**，
供契约测试与 Phase 1 迁移器脚手架使用，不是生产存储。

对应规范
--------
- v1.0 §11.1：``projects`` / ``backends`` / ``agent_bindings`` 三张表。
- R-05：``project.slug`` 不可变 —— :meth:`ProjectRepository.save` 必须拒绝
  「同一 id 换了 slug」的写入。
- R-09（关键约束）：一个 Project 可挂**多个同 backend** 的 Binding。因此本接口：
  * 没有任何 ``get_binding(project_id, backend_id) -> AgentBinding`` 形态的
    单值查询；按 backend 的查询一律返回序列
    （:meth:`AgentBindingRepository.list_for_backend`）；
  * ``save`` 不得以 ``(project_id, backend_id)`` 作为唯一键，只以 ``id`` 唯一。
  「默认 Binding」的单值语义由 ``is_default`` 表达，见
  :meth:`AgentBindingRepository.get_default_for_project`。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from app.errors import DomainInvariantError, SlugImmutableError
from app.projects.models import AgentBinding, Backend, Project, assert_binding_set_valid


@runtime_checkable
class ProjectRepository(Protocol):
    """v1.0 §11.1 ``projects``。"""

    async def get(self, project_id: str) -> Project | None: ...

    async def get_by_slug(self, slug: str) -> Project | None: ...

    async def list_all(self) -> Sequence[Project]: ...

    async def list_children(self, parent_project_id: str | None) -> Sequence[Project]: ...

    async def ancestry(self, project_id: str) -> Sequence[Project]:
        """返回从根到该 Project（含自身）的链，供 Capability Resolver 使用。"""
        ...

    async def save(self, project: Project) -> Project:
        """新增或更新。R-05：不得改动已存在记录的 ``slug``。"""
        ...

    async def delete(self, project_id: str) -> None:
        """删除 Project。C-4 / v1.0 §16.6：不得连带删除任何原生数据。"""
        ...


@runtime_checkable
class BackendRepository(Protocol):
    """v1.0 §11.1 ``backends``。"""

    async def get(self, backend_id: str) -> Backend | None: ...

    async def list_all(self) -> Sequence[Backend]: ...

    async def save(self, backend: Backend) -> Backend: ...


@runtime_checkable
class AgentBindingRepository(Protocol):
    """v1.0 §11.1 ``agent_bindings``；R-09 多 Binding 是硬约束。"""

    async def get(self, binding_id: str) -> AgentBinding | None: ...

    async def list_for_project(self, project_id: str) -> Sequence[AgentBinding]: ...

    async def list_for_backend(
        self, project_id: str, backend_id: str
    ) -> Sequence[AgentBinding]:
        """R-09：同一 Project 下同一 Backend 可能有多条，因此返回序列。"""
        ...

    async def get_default_for_project(self, project_id: str) -> AgentBinding | None:
        """``is_default`` 的单值查询——这是唯一允许返回单个 Binding 的按项目查询。"""
        ...

    async def save(self, binding: AgentBinding) -> AgentBinding: ...

    async def delete(self, binding_id: str) -> None:
        """C-4：detach Binding 不删除任何原生数据。"""
        ...


# --------------------------------------------------------------------------- #
# 内存参考实现
# --------------------------------------------------------------------------- #


class InMemoryProjectRepository:
    """参考实现：进程内字典存储，语义与接口一致。"""

    def __init__(self, projects: Sequence[Project] = ()) -> None:
        self._by_id: dict[str, Project] = {p.id: p for p in projects}

    async def get(self, project_id: str) -> Project | None:
        return self._by_id.get(project_id)

    async def get_by_slug(self, slug: str) -> Project | None:
        for project in self._by_id.values():
            if project.slug == slug:
                return project
        return None

    async def list_all(self) -> Sequence[Project]:
        return tuple(self._by_id.values())

    async def list_children(self, parent_project_id: str | None) -> Sequence[Project]:
        return tuple(
            p for p in self._by_id.values() if p.parent_project_id == parent_project_id
        )

    async def ancestry(self, project_id: str) -> Sequence[Project]:
        chain: list[Project] = []
        seen: set[str] = set()
        cursor = self._by_id.get(project_id)
        while cursor is not None:
            if cursor.id in seen:
                raise DomainInvariantError(f"Project 树出现环：{cursor.id!r}")
            seen.add(cursor.id)
            chain.append(cursor)
            cursor = (
                self._by_id.get(cursor.parent_project_id)
                if cursor.parent_project_id
                else None
            )
        chain.reverse()
        return tuple(chain)

    async def save(self, project: Project) -> Project:
        existing = self._by_id.get(project.id)
        if existing is not None and existing.slug != project.slug:
            raise SlugImmutableError(
                f"R-05：project.slug 不可变（{existing.slug!r} → {project.slug!r}）"
            )
        clash = await self.get_by_slug(project.slug)
        if clash is not None and clash.id != project.id:
            raise DomainInvariantError(f"slug 已被占用：{project.slug!r}")
        self._by_id[project.id] = project
        return project

    async def delete(self, project_id: str) -> None:
        self._by_id.pop(project_id, None)


class InMemoryBackendRepository:
    """参考实现。"""

    def __init__(self, backends: Sequence[Backend] = ()) -> None:
        self._by_id: dict[str, Backend] = {b.id: b for b in backends}

    async def get(self, backend_id: str) -> Backend | None:
        return self._by_id.get(backend_id)

    async def list_all(self) -> Sequence[Backend]:
        return tuple(self._by_id.values())

    async def save(self, backend: Backend) -> Backend:
        self._by_id[backend.id] = backend
        return backend


class InMemoryAgentBindingRepository:
    """参考实现。唯一键是 ``id``，**不是** ``(project_id, backend_id)``（R-09）。"""

    def __init__(self, bindings: Sequence[AgentBinding] = ()) -> None:
        self._by_id: dict[str, AgentBinding] = {b.id: b for b in bindings}

    async def get(self, binding_id: str) -> AgentBinding | None:
        return self._by_id.get(binding_id)

    async def list_for_project(self, project_id: str) -> Sequence[AgentBinding]:
        return tuple(b for b in self._by_id.values() if b.project_id == project_id)

    async def list_for_backend(
        self, project_id: str, backend_id: str
    ) -> Sequence[AgentBinding]:
        return tuple(
            b
            for b in self._by_id.values()
            if b.project_id == project_id and b.backend_id == backend_id
        )

    async def get_default_for_project(self, project_id: str) -> AgentBinding | None:
        for binding in await self.list_for_project(project_id):
            if binding.is_default:
                return binding
        return None

    async def save(self, binding: AgentBinding) -> AgentBinding:
        candidate = dict(self._by_id)
        candidate[binding.id] = binding
        assert_binding_set_valid(
            [b for b in candidate.values() if b.project_id == binding.project_id]
        )
        self._by_id = candidate
        return binding

    async def delete(self, binding_id: str) -> None:
        self._by_id.pop(binding_id, None)


__all__ = [
    "AgentBindingRepository",
    "BackendRepository",
    "InMemoryAgentBindingRepository",
    "InMemoryBackendRepository",
    "InMemoryProjectRepository",
    "ProjectRepository",
]
