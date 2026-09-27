"""批次五十三：run id 跨进程唯一（MV-01 / AD-174）在**组这一侧**的那一半。

这里只测一件事：``_run_started_at`` 这条扫描（已搬成纯函数
:func:`app.collaboration.member_turns.run_started_at`）在**撞名的历史数据**上
取到的是哪一条。

为什么这条要单独有一份测试
--------------------------
run id 修好之后，新账本里不会再有同名的 ``run.started``。但老账本里已经躺着
——后端每重启一次、续接同一条会话一次，就多一对同名的 ``<session>:r1``。这些
数据**不迁移**（AD-174），所以那道「从尾往前扫」的兜底要一直留着，而一道没有
测试守着的兜底迟早会被人顺手改回去（它看上去毫无意义：id 都唯一了，扫描方向
当然无所谓）。这份文件就是那句「不，还有历史数据」。

翻译器那一侧的唯一性断言在 ``drivers/acp/tests/test_translator.py``，
「重启后续接」的端到端形状在 ``drivers/acp/tests/test_driver.py``，
通用那把尺子在 ``drivers/contract_tests/suite.py``。

隔离：全是纯函数，不起进程、不碰数据库、不读任何凭据。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.collaboration.member_turns import membership_covers, run_started_at
from app.events.models import StoredEvent
from runtime.event_envelope import (
    EventSource,
    MessageDelta,
    RunStarted,
    make_envelope,
)

SESSION = "47001d1c-5a8e-47cd-b05a-fd1cbee30758"
#: 修之前的形状：run id 里只有会话 id 和进程内计数器。
COLLIDING_RUN_ID = f"{SESSION}:r1"
SOURCE = EventSource(driver_kind="acp", driver_version="0.1.0")
T0 = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)


def _row(sequence: int, event, *, created_at: datetime) -> StoredEvent:
    envelope = make_envelope(
        event=event,
        project_id="project:demo",
        conversation_id="conversation:11111111-1111-1111-1111-111111111111",
        agent_binding_id="binding:demo:acp",
        backend_id="backend:acp",
        sequence=sequence,
        source=SOURCE,
        occurred_at=T0,
        event_id=f"evt-{sequence}",
        run_id=getattr(event, "run_id", None) or COLLIDING_RUN_ID,
    )
    return StoredEvent.from_envelope(envelope, now=created_at)


def _legacy_ledger() -> tuple[StoredEvent, ...]:
    """一段**老账本**：两条 ``run.started`` 同名，中间隔着重启。

    行 0/1 是重启**前**那一轮；行 2 是重启**后**续接同一条会话的第一轮——翻译器
    的计数器从 0 重新数，于是它也叫 ``<session>:r1``。
    """
    return (
        _row(0, RunStarted(run_id=COLLIDING_RUN_ID), created_at=T0),
        _row(
            1,
            MessageDelta(message_id=f"{COLLIDING_RUN_ID}:m1", text="重启前说的话"),
            created_at=T0 + timedelta(seconds=1),
        ),
        _row(
            2,
            RunStarted(run_id=COLLIDING_RUN_ID),
            created_at=T0 + timedelta(minutes=30),
        ),
    )


def test_colliding_history_resolves_to_the_later_run_started() -> None:
    """撞名的老数据上，取到的必须是**后一条**的 ``created_at``。

    从头扫会取到 ``T0``（重启前那一轮的落库时间）。那个时间戳会被拿去判「这一轮
    开跑时他还在不在组里」，判在半小时前 —— 成员是这半小时里加进来的话，他这一
    轮的回答就被静默丢掉了。
    """
    rows = _legacy_ledger()
    assert run_started_at(rows, COLLIDING_RUN_ID) == T0 + timedelta(minutes=30)


def test_a_unique_run_id_is_found_wherever_it_sits() -> None:
    """id 唯一时方向本来就无所谓——这条守的是「兜底没有把正常情况弄坏」。"""
    rows = _legacy_ledger()
    unique = f"{SESSION}:9f3c1a20:r1"
    extra = _row(
        3, RunStarted(run_id=unique), created_at=T0 + timedelta(minutes=31)
    )
    assert run_started_at((*rows, extra), unique) == T0 + timedelta(minutes=31)


def test_a_run_with_no_started_row_is_none_not_a_crash() -> None:
    """找不到就是 ``None``——:func:`membership_covers` 对 ``None`` 判真。

    宁可多记一行可追溯的发言，也不要因为缺一个时间戳而静默丢掉成员的回答。
    """
    assert run_started_at(_legacy_ledger(), f"{SESSION}:deadbeef:r7") is None
    assert run_started_at((), COLLIDING_RUN_ID) is None
    assert membership_covers(joined_at=T0, left_at=None, run_started_at=None)


def test_the_later_row_is_the_one_that_keeps_a_late_joiner_in_the_room() -> None:
    """把两件事接起来看：取错时间戳 = 把成员的回答判成「他那时还没进组」。

    成员在重启后、第二轮开跑前加入。取后一条 → 判真（他在），取前一条 → 判假
    （他不在），那一行发言就没了。
    """
    rows = _legacy_ledger()
    joined_at = T0 + timedelta(minutes=10)
    started = run_started_at(rows, COLLIDING_RUN_ID)
    assert membership_covers(joined_at=joined_at, left_at=None, run_started_at=started)
    assert not membership_covers(joined_at=joined_at, left_at=None, run_started_at=T0)
