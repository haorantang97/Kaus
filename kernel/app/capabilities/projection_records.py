"""``projection_results``：每次物化留下的一行账（v1.0 §11.1 / Phase 5 工作项）。

为什么要留账
------------
物化是**改用户机器上的文件**。改完之后有两个问题必然会被问到：「上一次是什么时候
改的、改了哪些键」和「这次的改动和上次比多了什么」。答案不能只存在 HTTP 响应里
——那是一次性的，页面一刷新就没了。所以每次 ``confirm=1`` 的物化写一行。

这一行**只存摘要，不存值**
--------------------------
``summary`` 里是键路径、动作、以及 unsupported 的原因计数——**不存 before/after
的值**。理由与 ``.kaus-projected.json`` 只记键路径是同一条（§5.4）：值里可能有
凭据，领域库不是存它们的地方。要看值就现读现比（``GET /drift``）。

保留最近 20 条
--------------
:meth:`ProjectionResultRepository.save` 之后按 Binding 修剪。这不是一份审计日志
（那要不可篡改、要保留期策略，是另一件事），是一份「最近发生过什么」的便签。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Protocol, Sequence, runtime_checkable

from pydantic import Field

from app.base import DomainModel
from app.ids import BindingId, ProjectId, _new_uuid, _require_uuid  # type: ignore[attr-defined]

#: 每条 Binding 保留的物化记录条数。
RETENTION: Final[int] = 20


def projection_result_id(value: str | None = None) -> str:
    """``projection:<uuid>``。"""
    return (
        f"projection:{_new_uuid() if value is None else _require_uuid(value, field='projection uuid')}"
    )


class ProjectionRecord(DomainModel):
    """一次物化的账。``summary`` 的形状由接入层决定，领域层只要求它是 JSON。"""

    id: str
    binding_id: BindingId
    project_id: ProjectId
    applied_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    summary: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        binding_id: str,
        project_id: str,
        summary: dict[str, Any] | None = None,
        applied_at: datetime | None = None,
        record_uuid: str | None = None,
    ) -> ProjectionRecord:
        return cls(
            id=projection_result_id(record_uuid),
            binding_id=binding_id,
            project_id=project_id,
            applied_at=applied_at or datetime.now(tz=timezone.utc),
            summary=dict(summary or {}),
        )


@runtime_checkable
class ProjectionResultRepository(Protocol):
    """v1.0 §11.1 之外、Phase 5 新增的一张表（AD-149）。"""

    async def save(self, record: ProjectionRecord) -> ProjectionRecord:
        """写一行，并把该 Binding 的记录修剪到最近 :data:`RETENTION` 条。"""
        ...

    async def list_for_binding(
        self, binding_id: str, *, limit: int = RETENTION
    ) -> Sequence[ProjectionRecord]:
        """按时间**倒序**（最近的在前）。"""
        ...


class InMemoryProjectionResultRepository:
    """参考实现。"""

    def __init__(self) -> None:
        self._rows: list[ProjectionRecord] = []

    async def save(self, record: ProjectionRecord) -> ProjectionRecord:
        self._rows.append(record)
        kept = sorted(
            (r for r in self._rows if r.binding_id == record.binding_id),
            key=lambda r: (r.applied_at, r.id),
            reverse=True,
        )[:RETENTION]
        keep_ids = {r.id for r in kept}
        self._rows = [
            r for r in self._rows if r.binding_id != record.binding_id or r.id in keep_ids
        ]
        return record

    async def list_for_binding(
        self, binding_id: str, *, limit: int = RETENTION
    ) -> Sequence[ProjectionRecord]:
        rows = sorted(
            (r for r in self._rows if r.binding_id == binding_id),
            key=lambda r: (r.applied_at, r.id),
            reverse=True,
        )
        return tuple(rows[:limit])


__all__ = [
    "InMemoryProjectionResultRepository",
    "ProjectionRecord",
    "ProjectionResultRepository",
    "RETENTION",
    "projection_result_id",
]
