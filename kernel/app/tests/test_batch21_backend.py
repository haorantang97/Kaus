"""批次二十一第 3 件：``stopping-unconfirmed`` 挂太久也要能自己走出来（AD-147）。

AD-136 那条自愈的第一条判据是「本进程手上**没有** runtime」，而真机 25e6 卡住
的时候 runtime 还在（Driver 侧那一轮已经被收成死局，Session Host 并不知道），
所以它永远够不着这种卡死。这里补的是第二种失配：

- ``runState == "stopping-unconfirmed"``；
- 距离点停止已经超过 60s；
- Driver 明确说这一轮已经没有活动了（``run_is_active`` 回 ``False``）。

三条**都**满足才合成 ``run.interrupted{reason:"stop_unconfirmed"}``；Driver 说
「还在跑」或「不知道」时一律不动——编一句「中断了」比不收敛更糟。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 的子类桩，不碰任何真实引擎。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from app.tests.test_batch17_backend import Harness  # noqa: E402
from drivers.base import MessageInput  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from runtime.session_host import (  # noqa: E402
    STOP_UNCONFIRMED_GRACE_SECONDS,
    STOP_UNCONFIRMED_REASON,
)


class _StuckStopDriver(MockDriver):
    """一台「停止请求收下了，但回合再也不给终态」的引擎。

    ``run_is_active`` 是可选钩子（`BackendDriver` 协议里没有它），Session Host
    用 ``getattr`` 取——这里按用例的需要给出三种答案。
    """

    def __init__(self, *, active_answer: bool | None = False) -> None:
        super().__init__()
        self.active_answer = active_answer
        self.interrupted = 0

    async def interrupt(self, runtime) -> None:  # noqa: D102 - 收下就完了，不发终态
        self.interrupted += 1

    async def run_is_active(self, runtime) -> bool | None:  # noqa: D102
        return self.active_answer


async def _stuck_stopping(built: Harness, *, waited: float) -> None:
    """造一条「点过停止、``waited`` 秒过去了还没收敛」的活跃会话。"""
    conversation = await built.new_conversation()
    await built.host.start_runtime(conversation)
    await built.host.send_message(conversation.id, MessageInput(text="sleep"))
    await built.host.interrupt(conversation.id)
    runtime = built.host._runtimes[conversation.id]  # noqa: SLF001 - 用例要拨时钟
    assert runtime.interrupt_requested_at is not None
    runtime.interrupt_requested_at -= timedelta(seconds=waited)
    return conversation


def _terminals(envelopes):
    return [
        e
        for e in envelopes
        if e.event.type in ("run.completed", "run.failed", "run.interrupted")
    ]


async def _run(tmp_path, *, active_answer, waited):
    built = Harness(tmp_path, driver=_StuckStopDriver(active_answer=active_answer))
    await built.seed()
    try:
        conversation = await _stuck_stopping(built, waited=waited)
        assert await built.host.run_state_of(conversation) == "stopping-unconfirmed"
        healed = await built.host.reconcile_conversation(conversation)
        events = await built.host.event_store.replay(conversation.id)
        state = await built.host.run_state_of(conversation)
        return built, conversation, healed, events, state
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


async def test_a_stop_stuck_for_over_a_minute_heals(tmp_path) -> None:
    """60s + Driver 说「这一轮没活动了」→ 合成 ``run.interrupted``，页头回 idle。"""
    _, _, healed, events, state = await _run(
        tmp_path, active_answer=False, waited=STOP_UNCONFIRMED_GRACE_SECONDS + 1
    )
    assert healed is True
    terminals = _terminals(events)
    assert len(terminals) == 1
    assert terminals[0].event.type == "run.interrupted"
    assert terminals[0].event.reason == STOP_UNCONFIRMED_REASON
    # 给人看的那一句也要留下（只说状态，不编原因）。
    assert any(e.event.type == "diagnostic.notice" for e in events)
    assert state == "idle"


async def test_it_waits_out_the_grace_period(tmp_path) -> None:
    """刚点下去的停止不算卡死：Driver 那条 30s 轮询还在跑，两边一起动手会出两条终态。"""
    _, _, healed, events, state = await _run(
        tmp_path, active_answer=False, waited=STOP_UNCONFIRMED_GRACE_SECONDS - 5
    )
    assert healed is False
    assert _terminals(events) == []
    assert state == "stopping-unconfirmed"


@pytest.mark.parametrize("answer", [True, None])
async def test_it_does_not_guess_when_the_engine_might_still_be_running(
    tmp_path, answer
) -> None:
    """Driver 回「还在跑」或「不知道」→ 不动。编一句「中断了」比不收敛更糟。"""
    _, _, healed, events, state = await _run(
        tmp_path, active_answer=answer, waited=STOP_UNCONFIRMED_GRACE_SECONDS + 30
    )
    assert healed is False
    assert _terminals(events) == []
    assert state == "stopping-unconfirmed"


def test_a_driver_without_the_hook_is_left_alone(tmp_path) -> None:
    """没有 ``run_is_active`` 这条面的 Driver 一律按「不知道」处理。"""

    class _NoHook(_StuckStopDriver):
        run_is_active = None  # 属性存在但不是可调用的钩子 → getattr 拿到 None

    async def _go() -> None:
        built = Harness(tmp_path, driver=_NoHook())
        await built.seed()
        try:
            conversation = await _stuck_stopping(
                built, waited=STOP_UNCONFIRMED_GRACE_SECONDS + 30
            )
            assert await built.host.reconcile_conversation(conversation) is False
            assert _terminals(await built.host.event_store.replay(conversation.id)) == []
        finally:
            await built.host.aclose()
            built.unit_of_work.close()

    asyncio.run(_go())
