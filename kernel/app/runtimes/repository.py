"""Runtime Lease / Terminal Launch 的 Repository 接口与内存参考实现。

对应规范
--------
- v1.0 §11.1 ``runtime_leases``、§8.6（stale lease recovery）。
- R-10：Session Host 启动时 reconcile，按 ``backend_process_id`` 清理 stale lease。
- R-04 / 裁决表 #1 / **AD-11**：``acquire`` 在 ``advisory`` 策略下**不会**因为
  已有他方 lease 而失败——lease 是信息性的，不是锁。写入 / 释放 / 列出三件事而已，
  接口层刻意没有任何「获取失败」「等待锁」的形态。只有 ``exclusive``
  （为开源多用户版预留的升级档）才拒绝抢占。
- v1.0 §11.1 ``terminal_launches``：站外 CLI 启动簿记，按 Correlation ID 回查。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, Sequence, runtime_checkable

from app.errors import DomainInvariantError
from app.runtimes.models import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    RuntimeLease,
    TerminalLaunch,
    reconcile_leases,
)


@runtime_checkable
class RuntimeLeaseRepository(Protocol):
    """v1.0 §11.1 ``runtime_leases``。"""

    async def get(self, conversation_id: str) -> RuntimeLease | None: ...

    async def list_all(self) -> Sequence[RuntimeLease]: ...

    async def acquire(self, lease: RuntimeLease) -> RuntimeLease:
        """登记归属。``advisory`` 允许覆盖，``exclusive`` 拒绝抢占（R-04）。"""
        ...

    async def heartbeat(
        self, conversation_id: str, at: datetime | None = None
    ) -> RuntimeLease | None: ...

    async def release(self, conversation_id: str) -> None: ...

    async def reconcile(
        self,
        *,
        is_process_alive_by_id: dict[str, bool],
        now: datetime | None = None,
        heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> Sequence[RuntimeLease]:
        """R-10：清理 stale lease，返回被清理的那些。"""
        ...


class InMemoryRuntimeLeaseRepository:
    """参考实现。"""

    def __init__(self, leases: Sequence[RuntimeLease] = ()) -> None:
        self._by_conversation: dict[str, RuntimeLease] = {
            lease.conversation_id: lease for lease in leases
        }

    async def get(self, conversation_id: str) -> RuntimeLease | None:
        return self._by_conversation.get(conversation_id)

    async def list_all(self) -> Sequence[RuntimeLease]:
        return tuple(self._by_conversation.values())

    async def acquire(self, lease: RuntimeLease) -> RuntimeLease:
        existing = self._by_conversation.get(lease.conversation_id)
        if (
            existing is not None
            and existing.blocks_writes
            and existing.owner_id != lease.owner_id
        ):
            raise DomainInvariantError(
                "exclusive lease 已被他方持有，需显式强制接管（v1.0 §8.6）："
                f"{existing.owner_type}/{existing.owner_id}"
            )
        self._by_conversation[lease.conversation_id] = lease
        return lease

    async def heartbeat(
        self, conversation_id: str, at: datetime | None = None
    ) -> RuntimeLease | None:
        lease = self._by_conversation.get(conversation_id)
        if lease is None:
            return None
        refreshed = lease.beat(at)
        self._by_conversation[conversation_id] = refreshed
        return refreshed

    async def release(self, conversation_id: str) -> None:
        self._by_conversation.pop(conversation_id, None)

    async def reconcile(
        self,
        *,
        is_process_alive_by_id: dict[str, bool],
        now: datetime | None = None,
        heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> Sequence[RuntimeLease]:
        alive, stale = reconcile_leases(
            tuple(self._by_conversation.values()),
            is_process_alive=lambda pid: is_process_alive_by_id.get(pid, False),
            now=now,
            heartbeat_timeout=heartbeat_timeout,
        )
        self._by_conversation = {lease.conversation_id: lease for lease in alive}
        return stale


@runtime_checkable
class TerminalLaunchRepository(Protocol):
    """v1.0 §11.1 ``terminal_launches``。"""

    async def get(self, launch_id: str) -> TerminalLaunch | None: ...

    async def get_by_correlation_id(
        self, correlation_id: str
    ) -> TerminalLaunch | None:
        """v1.0 §8.7：外部进程靠 Correlation ID 与本次启动确定性对上号。"""
        ...

    async def list_for_conversation(
        self, conversation_id: str, *, include_finished: bool = True
    ) -> Sequence[TerminalLaunch]: ...

    async def save(self, launch: TerminalLaunch) -> TerminalLaunch:
        """Correlation ID 全局唯一；重复即冲突。"""
        ...

    async def delete(self, launch_id: str) -> None: ...


class InMemoryTerminalLaunchRepository:
    """参考实现。"""

    def __init__(self, launches: Sequence[TerminalLaunch] = ()) -> None:
        self._by_id: dict[str, TerminalLaunch] = {}
        for launch in launches:
            self._by_id[launch.id] = launch

    async def get(self, launch_id: str) -> TerminalLaunch | None:
        return self._by_id.get(launch_id)

    async def get_by_correlation_id(self, correlation_id: str) -> TerminalLaunch | None:
        for launch in self._by_id.values():
            if launch.correlation_id == correlation_id:
                return launch
        return None

    async def list_for_conversation(
        self, conversation_id: str, *, include_finished: bool = True
    ) -> Sequence[TerminalLaunch]:
        return tuple(
            launch
            for launch in self._by_id.values()
            if launch.conversation_id == conversation_id
            and (include_finished or launch.status in ("launched", "running"))
        )

    async def save(self, launch: TerminalLaunch) -> TerminalLaunch:
        clash = await self.get_by_correlation_id(launch.correlation_id)
        if clash is not None and clash.id != launch.id:
            raise DomainInvariantError(
                f"Correlation ID 已被占用（v1.0 §8.7）：{launch.correlation_id!r}"
            )
        self._by_id[launch.id] = launch
        return launch

    async def delete(self, launch_id: str) -> None:
        self._by_id.pop(launch_id, None)


__all__ = [
    "InMemoryRuntimeLeaseRepository",
    "InMemoryTerminalLaunchRepository",
    "RuntimeLeaseRepository",
    "TerminalLaunchRepository",
]
