"""LeaseManager 的单元测试（AD-11 / v1.0 §8.8.4）。

最重要的一条是**否定性**的：lease 不是锁。已有他人 lease 时
:meth:`~runtime.lease_manager.LeaseManager.acquire_card` 仍然成功，
接口层也不存在任何「获取失败」的返回形态。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.runtimes.models import RuntimeLease
from app.runtimes.repository import InMemoryRuntimeLeaseRepository
from runtime.lease_manager import LeaseManager

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CONVERSATION = "conversation:11111111-1111-1111-1111-111111111111"


def make_manager(
    *leases: RuntimeLease, now: datetime = NOW, heartbeat_timeout=timedelta(seconds=90)
) -> tuple[LeaseManager, InMemoryRuntimeLeaseRepository]:
    repository = InMemoryRuntimeLeaseRepository(leases)
    return (
        LeaseManager(repository, heartbeat_timeout=heartbeat_timeout, clock=lambda: now),
        repository,
    )


async def test_card_lease_records_owner_and_heartbeat() -> None:
    manager, _ = make_manager()
    lease = await manager.acquire_card(
        CONVERSATION, owner_id="host-1", backend_process_id="runtime-1"
    )

    assert lease.owner_type == "card"
    assert lease.policy == "advisory"
    described = await manager.describe(CONVERSATION)
    assert described is not None
    assert (described.owner_type, described.owner_id) == ("card", "host-1")
    assert described.heartbeat_at == NOW
    assert described.backend_process_id == "runtime-1"
    assert described.advisory_only is True
    assert described.is_stale is False


async def test_acquire_never_fails_even_when_someone_else_holds_it() -> None:
    """AD-11：lease 是信息性的，不参与准入判断——拿不到 lease 不是拒绝的理由。"""
    manager, _ = make_manager()
    await manager.acquire_external_cli(CONVERSATION, owner_id="launcher-1")

    lease = await manager.acquire_card(CONVERSATION, owner_id="host-1")

    assert lease.owner_type == "card"
    described = await manager.describe(CONVERSATION)
    assert described is not None and described.owner_id == "host-1"


async def test_release_only_touches_its_own_row() -> None:
    """Card runtime 停止不得抹掉启动器写的 ``external-cli`` lease（AD-11）。"""
    manager, _ = make_manager()
    await manager.acquire_external_cli(CONVERSATION, owner_id="launcher-1")

    assert await manager.release(CONVERSATION, expected_owner_id="host-1") is False
    assert (await manager.describe(CONVERSATION)) is not None

    assert await manager.release(CONVERSATION, expected_owner_id="launcher-1") is True
    assert (await manager.describe(CONVERSATION)) is None


async def test_missing_lease_is_a_legal_state() -> None:
    """带外 CLI 没有 lease（AD-11 第三行）：describe 返回 None，不是错误。"""
    manager, _ = make_manager()
    assert await manager.describe(CONVERSATION) is None
    assert await manager.heartbeat(CONVERSATION) is None
    assert await manager.release(CONVERSATION) is False


async def test_heartbeat_refreshes_and_clears_staleness() -> None:
    stale_lease = RuntimeLease(
        conversation_id=CONVERSATION,
        owner_type="card",
        owner_id="host-1",
        acquired_at=NOW - timedelta(minutes=10),
        heartbeat_at=NOW - timedelta(minutes=10),
    )
    manager, _ = make_manager(stale_lease)

    described = await manager.describe(CONVERSATION)
    assert described is not None and described.is_stale is True

    await manager.heartbeat(CONVERSATION)
    refreshed = await manager.describe(CONVERSATION)
    assert refreshed is not None and refreshed.is_stale is False


async def test_reconcile_clears_stale_and_dead_process_leases() -> None:
    """v1.0 §8.8.4 / §9.4：Session Host 启动时的 lease reconcile。"""
    alive = RuntimeLease(
        conversation_id="conversation:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        owner_type="card",
        owner_id="host-1",
        backend_process_id="proc-alive",
        acquired_at=NOW,
        heartbeat_at=NOW,
    )
    dead_process = alive.evolve(
        conversation_id="conversation:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        backend_process_id="proc-dead",
    )
    timed_out = alive.evolve(
        conversation_id="conversation:cccccccc-cccc-cccc-cccc-cccccccccccc",
        backend_process_id="proc-alive",
        heartbeat_at=NOW - timedelta(minutes=30),
    )
    manager, repository = make_manager(alive, dead_process, timed_out)

    stale = await manager.reconcile(alive_process_ids=("proc-alive",))

    assert {lease.conversation_id for lease in stale} == {
        dead_process.conversation_id,
        timed_out.conversation_id,
    }
    assert [lease.conversation_id for lease in await repository.list_all()] == [
        alive.conversation_id
    ]


async def test_describe_all_projects_every_row() -> None:
    manager, _ = make_manager()
    await manager.acquire_card(CONVERSATION, owner_id="host-1")
    await manager.acquire_external_cli(
        "conversation:22222222-2222-2222-2222-222222222222", owner_id="launcher-1"
    )

    owners = {row.conversation_id: row.owner_type for row in await manager.describe_all()}
    assert owners == {
        CONVERSATION: "card",
        "conversation:22222222-2222-2222-2222-222222222222": "external-cli",
    }
