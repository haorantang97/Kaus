"""Runtime Lease 管理器（AD-11 / v1.0 §8.8.4；AD-122 升格为单写者）。

两套入口，两种语义
------------------
本模块现在同时提供两代口径，**不要混用**：

============================  =====================================================
``acquire_card`` /            AD-11 的旧口径：信息性 lease，**永远成功**，覆盖记录
``acquire_external_cli``      而不判断。只驱动软提示与 Header 显示。
``try_acquire_card`` /        AD-122 的新口径：**单写者**。已有未过期且 owner 不同的
``try_acquire_external_cli``  lease 时返回冲突（不覆盖），除非 ``force=True``。
============================  =====================================================

AD-122 为什么要升格：Card 与外部终端写的是**同一条 Native Session**（v1.0 §8.6
硬性约束「同一 Native Session 不允许 Card 与 CLI 并行写入」）。软提示挡不住双写，
只能挡住「用户不知情」。所以 Card ⇄ External CLI 这条交接路径改走
``try_acquire_*``：拿不到就 409，由用户显式选择强制接管。

过期与 stale recovery
---------------------
lease 的有效期 = ``heartbeat_at + ttl``（ttl 即 :attr:`LeaseManager.lease_ttl`，
默认 90s，接入层可由 ``runtime.lease_ttl_seconds`` 配置）。过期的 lease 视为
**stale**，可被直接接管，不需要 ``force``——持有者已经不在了，让用户去点一次
「强制接管」只是让他替一个死进程做决定。接管时 :class:`LeaseOutcome` 会带上
``stale_recovered=True`` 与一句 ``reason``，调用方据此打一条 diagnostic。

AD-11 的写入者分工
------------------
| 触发 | 写入者 | ``owner_type`` | 释放时机 |
|---|---|---|---|
| Card runtime 启动 | Session Host（:meth:`LeaseManager.acquire_card`） | ``card`` | runtime 停止 |
| Dashboard 拉起外部 CLI | Terminal Launcher（:meth:`LeaseManager.acquire_external_cli`，**本期只是预留接口**） | ``external-cli`` | 外部进程退出被检测到 |
| 用户自行在终端跑 CLI | 无人写 | — | 无 lease；只能靠 §8.8.2 的带外检测发现 |

「无 lease」是**合法状态**，不是异常：:meth:`LeaseManager.describe` 返回 ``None``
时 UI 应显示「无人持有」，而不是拒绝发送。任何实现都不得把「拿不到 lease」
变成拒绝发送的理由——那等于把 D-09 第一期偷偷做成强制锁。

对应规范
--------
- **AD-11**（锁定）：写入者、``owner_type``、释放时机、信息性质。
- v1.0 §8.8.4：字段 = owner、acquired_at、heartbeat、``backend_process_id``；
  必须具备 stale lease recovery；Session Host 启动时执行 lease reconcile。
- R-04 / D-09：第一期软提示，不锁不禁用。``exclusive`` 档留给开源多用户版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping, Sequence

from pydantic import Field

from app.base import DomainModel
from app.runtimes.models import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    OwnerType,
    RuntimeLease,
)
from app.runtimes.repository import RuntimeLeaseRepository


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


DEFAULT_LEASE_TTL_SECONDS: Final[float] = DEFAULT_HEARTBEAT_TIMEOUT.total_seconds()
"""AD-122 的 lease 有效期（秒）。接入层可由 ``runtime.lease_ttl_seconds`` 覆盖。"""


class LeaseDescription(DomainModel):
    """一条 Conversation 当前的 Runtime Owner 视图（v1.0 §8.8.4：UI 必须显示）。

    这是**只读投影**，专门给 Header 与软提示条用；它不带任何「能不能写」的判断，
    因为软提示模式下答案永远是「能」。
    """

    conversation_id: str
    owner_type: OwnerType
    owner_id: str
    backend_process_id: str | None = None
    acquired_at: datetime
    heartbeat_at: datetime
    #: AD-122：``heartbeat_at + ttl``。到点之后这条 lease 可被直接接管。
    expires_at: datetime | None = None
    is_stale: bool = False
    #: 恒为 True：AD-11 的 lease 不参与准入判断。留成字段是为了让消费端
    #: 显式读到这个口径，而不是靠注释约定。
    advisory_only: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class LeaseOutcome:
    """一次 ``try_acquire_*`` 的结果（AD-122）。

    ``granted=False`` 时 :attr:`conflict` 必然非空——那就是拦住这次获取的那条
    lease，接入层把它原样放进 409 的 ``detail``。``granted=True`` 时：

    - :attr:`previous` 非空 = 接管了别人的 lease（强制接管或 stale 回收）；
    - :attr:`stale_recovered` 为真 = 前一条已过期，属于 stale recovery，
      调用方应打一条 diagnostic 而不是当作正常获取。
    """

    granted: bool
    lease: RuntimeLease | None = None
    conflict: LeaseDescription | None = None
    previous: LeaseDescription | None = None
    stale_recovered: bool = False
    reason: str | None = None

    @property
    def took_over(self) -> bool:
        """是否从别人手里接管（含 stale 回收）。"""
        return self.granted and self.previous is not None


class LeaseManager:
    """``runtime_leases`` 的写入方（AD-11）。

    参数
    ----
    repository:
        :class:`~app.runtimes.repository.RuntimeLeaseRepository` 实现。
    heartbeat_timeout:
        stale 判定阈值（v1.0 §8.8.4 stale lease recovery）。
    """

    def __init__(
        self,
        repository: RuntimeLeaseRepository,
        *,
        heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
        clock=_utcnow,
    ) -> None:
        self._repository = repository
        self.heartbeat_timeout = heartbeat_timeout
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    async def acquire_card(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        backend_process_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> RuntimeLease:
        """Card runtime 启动时写 ``owner=card``（AD-11 第一行）。

        **永远成功**：即使当前已有别人的 lease（包括 ``external-cli``），也只是
        覆盖记录并让软提示显示新的 owner。不存在「因为拿不到 lease 而不启动」。
        """
        return await self._acquire(
            conversation_id,
            owner_type="card",
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            metadata=metadata,
            at=at,
        )

    async def acquire_external_cli(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        backend_process_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> RuntimeLease:
        """**预留接口**：Dashboard 拉起外部 CLI 时由启动器写 ``owner=external-cli``。

        AD-11 把这一行的写入者定为 Terminal Launcher 路径，不是 Session Host；
        本方法只是把「写在哪儿、写成什么形状」固定下来，供启动器调用。
        Session Host 自身不会调它。
        """
        return await self._acquire(
            conversation_id,
            owner_type="external-cli",
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            metadata=metadata,
            at=at,
        )

    # --- AD-122：单写者入口 -------------------------------------------- #

    @property
    def lease_ttl(self) -> timedelta:
        """lease 的有效期。与 stale 判定阈值是同一个值，不另设一套口径。"""
        return self.heartbeat_timeout

    def expires_at(self, lease: RuntimeLease) -> datetime:
        """这条 lease 什么时候过期（``heartbeat_at + ttl``）。

        显式的 ``expires_at`` 列若更早则以它为准——那是写入方主动约的更短有效期，
        不该被 ttl 拉长。
        """
        deadline = lease.heartbeat_at + self.lease_ttl
        if lease.expires_at is not None and lease.expires_at < deadline:
            return lease.expires_at
        return deadline

    def _is_expired(self, lease: RuntimeLease, now: datetime) -> bool:
        return now >= self.expires_at(lease)

    async def try_acquire_card(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        backend_process_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        at: datetime | None = None,
        force: bool = False,
    ) -> LeaseOutcome:
        """Card 侧获取写权（AD-122）。外部 lease 未过期时返回冲突，不覆盖。"""
        return await self._try_acquire(
            conversation_id,
            owner_type="card",
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            metadata=metadata,
            at=at,
            force=force,
        )

    async def try_acquire_external_cli(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        backend_process_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        at: datetime | None = None,
        force: bool = False,
    ) -> LeaseOutcome:
        """Terminal Launcher 侧获取写权（AD-122）。语义同 :meth:`try_acquire_card`。"""
        return await self._try_acquire(
            conversation_id,
            owner_type="external-cli",
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            metadata=metadata,
            at=at,
            force=force,
        )

    async def _try_acquire(
        self,
        conversation_id: str,
        *,
        owner_type: OwnerType,
        owner_id: str,
        backend_process_id: str | None,
        metadata: Mapping[str, Any] | None,
        at: datetime | None,
        force: bool,
    ) -> LeaseOutcome:
        now = at or self._clock()
        existing = await self._repository.get(conversation_id)
        previous: LeaseDescription | None = None
        stale_recovered = False
        reason: str | None = None
        if existing is not None and (
            existing.owner_type != owner_type or existing.owner_id != owner_id
        ):
            described = self._describe(existing, now=now)
            if self._is_expired(existing, now):
                # stale recovery：持有者已经不在了，不必让用户替死进程做决定。
                stale_recovered = True
                previous = described
                reason = (
                    f"接管了已过期的 lease（owner={existing.owner_type}/"
                    f"{existing.owner_id}，最后心跳 {existing.heartbeat_at.isoformat()}）"
                )
            elif force:
                previous = described
                reason = (
                    f"强制接管（前 owner={existing.owner_type}/{existing.owner_id}）"
                )
            else:
                return LeaseOutcome(
                    granted=False,
                    conflict=described,
                    reason=(
                        f"lease 由 {existing.owner_type}/{existing.owner_id} 持有，"
                        "未过期；需要显式强制接管"
                    ),
                )
        lease = await self._acquire(
            conversation_id,
            owner_type=owner_type,
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            metadata=metadata,
            at=now,
        )
        return LeaseOutcome(
            granted=True,
            lease=lease,
            previous=previous,
            stale_recovered=stale_recovered,
            reason=reason,
        )

    async def _acquire(
        self,
        conversation_id: str,
        *,
        owner_type: OwnerType,
        owner_id: str,
        backend_process_id: str | None,
        metadata: Mapping[str, Any] | None,
        at: datetime | None,
    ) -> RuntimeLease:
        now = at or self._clock()
        lease = RuntimeLease(
            conversation_id=conversation_id,
            owner_type=owner_type,
            owner_id=owner_id,
            backend_process_id=backend_process_id,
            # R-04 第一期：只有 advisory。exclusive 档不由本管理器写。
            policy="advisory",
            acquired_at=now,
            heartbeat_at=now,
            metadata=dict(metadata or {}),
        )
        return await self._repository.acquire(lease)

    async def heartbeat(
        self, conversation_id: str, *, at: datetime | None = None
    ) -> RuntimeLease | None:
        """刷新心跳；没有 lease 时返回 ``None``（不是错误）。"""
        return await self._repository.heartbeat(conversation_id, at or self._clock())

    async def release(
        self, conversation_id: str, *, expected_owner_id: str | None = None
    ) -> bool:
        """释放。返回是否真的删了一行。

        ``expected_owner_id`` 非空时只释放自己那行——避免 Card runtime 停止时
        顺手抹掉启动器写的 ``external-cli`` lease（AD-11：两者各自释放）。
        """
        current = await self._repository.get(conversation_id)
        if current is None:
            return False
        if expected_owner_id is not None and current.owner_id != expected_owner_id:
            return False
        await self._repository.release(conversation_id)
        return True

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    async def describe(
        self, conversation_id: str, *, now: datetime | None = None
    ) -> LeaseDescription | None:
        """当前 owner 与心跳；无人持有返回 ``None``（合法状态，见模块文档）。"""
        lease = await self._repository.get(conversation_id)
        if lease is None:
            return None
        return self._describe(lease, now=now or self._clock())

    async def describe_all(
        self, *, now: datetime | None = None
    ) -> tuple[LeaseDescription, ...]:
        at = now or self._clock()
        return tuple(
            self._describe(lease, now=at) for lease in await self._repository.list_all()
        )

    def _describe(self, lease: RuntimeLease, *, now: datetime) -> LeaseDescription:
        return LeaseDescription(
            conversation_id=lease.conversation_id,
            owner_type=lease.owner_type,
            owner_id=lease.owner_id,
            backend_process_id=lease.backend_process_id,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=self.expires_at(lease),
            is_stale=self._is_expired(lease, now),
            metadata=dict(lease.metadata),
        )

    # ------------------------------------------------------------------ #
    # 启动时的 reconcile（v1.0 §8.8.4 / §9.4）
    # ------------------------------------------------------------------ #

    async def reconcile(
        self,
        *,
        alive_process_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> tuple[RuntimeLease, ...]:
        """Session Host 启动时清理 stale lease，返回被清理的那些。

        判定顺序（见 :func:`~app.runtimes.models.reconcile_leases`）：
        显式过期 → 心跳超时 → ``backend_process_id`` 对应进程已不存在。
        """
        known = {pid: True for pid in alive_process_ids}
        stale = await self._repository.reconcile(
            is_process_alive_by_id=known,
            now=now or self._clock(),
            heartbeat_timeout=self.heartbeat_timeout,
        )
        return tuple(stale)


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "LeaseDescription",
    "LeaseManager",
    "LeaseOutcome",
]
