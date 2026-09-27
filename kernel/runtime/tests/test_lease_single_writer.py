"""AD-122：Lease 从软提示升格为单写者。

验四件事（任务书「验证」第一组）：
冲突不覆盖 / 强制接管带前 owner / 过期即 stale 可直接回收 / 同一 owner 续期。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.persistence.memory import in_memory_repository_set
from runtime.lease_manager import DEFAULT_LEASE_TTL_SECONDS, LeaseManager

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
CONVERSATION = "conversation:11111111-1111-4111-8111-111111111111"


def _manager(ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> LeaseManager:
    return LeaseManager(
        in_memory_repository_set().leases,
        heartbeat_timeout=timedelta(seconds=ttl_seconds),
    )


async def test_live_external_lease_blocks_card() -> None:
    """外部终端持有且未过期 → Card 拿不到，且**不覆盖**原记录。"""
    manager = _manager()
    await manager.try_acquire_external_cli(
        CONVERSATION, owner_id="launch:abc", metadata={"launchId": "launch:abc"}, at=T0
    )
    outcome = await manager.try_acquire_card(
        CONVERSATION, owner_id="session-host", at=T0 + timedelta(seconds=5)
    )
    assert outcome.granted is False
    assert outcome.conflict is not None
    assert outcome.conflict.owner_type == "external-cli"
    assert outcome.conflict.metadata["launchId"] == "launch:abc"
    # 没覆盖：库里还是外部那条。
    current = await manager.describe(CONVERSATION)
    assert current is not None and current.owner_id == "launch:abc"


async def test_force_takes_over_and_reports_previous_owner() -> None:
    """``force=True`` 才接管，并把前 owner 交回调用方（用于 lease.taken_over）。"""
    manager = _manager()
    await manager.try_acquire_card(CONVERSATION, owner_id="session-host", at=T0)
    outcome = await manager.try_acquire_external_cli(
        CONVERSATION,
        owner_id="launch:xyz",
        at=T0 + timedelta(seconds=10),
        force=True,
    )
    assert outcome.granted is True
    assert outcome.took_over is True
    assert outcome.previous is not None
    assert outcome.previous.owner_type == "card"
    # 强制接管不是 stale 回收：两者在事件里要分得开。
    assert outcome.stale_recovered is False
    current = await manager.describe(CONVERSATION)
    assert current is not None and current.owner_type == "external-cli"


async def test_expired_lease_is_recovered_without_force() -> None:
    """``heartbeat_at + ttl`` 过了就是 stale：直接接管，并标记出来打 diagnostic。"""
    manager = _manager(ttl_seconds=90)
    await manager.try_acquire_external_cli(CONVERSATION, owner_id="launch:dead", at=T0)
    outcome = await manager.try_acquire_card(
        CONVERSATION,
        owner_id="session-host",
        at=T0 + timedelta(seconds=91),
    )
    assert outcome.granted is True
    assert outcome.stale_recovered is True
    assert outcome.previous is not None and outcome.previous.owner_id == "launch:dead"
    assert outcome.reason and "过期" in outcome.reason


async def test_same_owner_reacquire_refreshes_instead_of_conflicting() -> None:
    """同一 owner 再拿一次 = 续期，不是抢占（Card 补 backend_process_id 走这条）。"""
    manager = _manager()
    first = await manager.try_acquire_card(CONVERSATION, owner_id="session-host", at=T0)
    later = T0 + timedelta(seconds=30)
    second = await manager.try_acquire_card(
        CONVERSATION,
        owner_id="session-host",
        backend_process_id="mock-runtime-1",
        at=later,
    )
    assert first.granted and second.granted
    assert second.previous is None and second.stale_recovered is False
    described = await manager.describe(CONVERSATION, now=later)
    assert described is not None
    assert described.backend_process_id == "mock-runtime-1"
    assert described.heartbeat_at == later
    # 有效期跟着心跳走。
    assert described.expires_at == later + manager.lease_ttl
    assert described.is_stale is False
