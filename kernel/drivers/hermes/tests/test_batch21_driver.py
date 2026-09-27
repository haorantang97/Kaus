"""停止确认：超时保留运行身份，后续原生终态仍可收敛。

只使用假 HTTP/SSE gateway 和临时 HERMES_HOME，不调用真实引擎。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from drivers.base import MessageInput, TurnAlreadyRunningError, UnsupportedCapabilityError
from drivers.hermes.testing.fake_api_server import (
    stop_never_confirms_script,
    stop_stalls_script,
    stop_then_completes_script,
)
from drivers.hermes.testing.harness import FakeHermesHarness
from drivers.hermes.translator import STOP_UNCONFIRMED_REASON

TERMINALS = ("run.completed", "run.failed", "run.interrupted")


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


async def _ready(rig, *, timeout: float = 0.6, interval: float = 0.05):
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    # 缩短提示期限，生产默认 30 秒；到期不是终态。
    driver.stop_confirm_timeout = timeout
    driver.stop_poll_interval = interval
    return driver, project, binding


async def _stopped_turn(rig, driver, project, binding, script, *, timeout: float = 15.0):
    """发一轮 → 见到 ``run.started`` → 停止 → 收齐到终态为止的事件。"""
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    rig.servers["default"].set_script(script)
    collected = []
    try:
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        first = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        assert first.event.type == "run.started"
        await asyncio.wait_for(driver.interrupt(runtime), timeout=timeout)
        while True:
            envelope = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
            collected.append(envelope)
            if envelope.event.type in TERMINALS:
                break
        return runtime, conversation, collected
    finally:
        await driver.stop_runtime(runtime)


@asynccontextmanager
async def _unconfirmed_turn(rig, driver, project, binding, script=stop_never_confirms_script):
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    rig.servers["default"].set_script(script)
    collected = []
    try:
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        assert (await asyncio.wait_for(stream.__anext__(), timeout=5)).event.type == "run.started"
        await driver.interrupt(runtime)
        while True:
            envelope = await asyncio.wait_for(stream.__anext__(), timeout=5)
            collected.append(envelope)
            assert envelope.event.type not in TERMINALS
            if envelope.event.type == "extension.event" and envelope.event.name == "run.stop_unconfirmed":
                break
        yield runtime, conversation, stream, collected
    finally:
        await driver.stop_runtime(runtime)


async def _collect_terminal(stream, collected):
    while True:
        envelope = await asyncio.wait_for(stream.__anext__(), timeout=5)
        collected.append(envelope)
        if envelope.event.type in TERMINALS:
            return envelope


# --------------------------------------------------------------------------- #
# 未确认的停止保持可观察，只有原生终态才结束
# --------------------------------------------------------------------------- #


async def test_a_stop_the_engine_never_confirms_remains_active(rig) -> None:
    driver, project, binding = await _ready(rig)
    async with _unconfirmed_turn(rig, driver, project, binding) as (runtime, _, _, collected):
        state = driver._runtimes[runtime.runtime_id]
        assert not state.run.finalized
        assert state.run.stop_task is not None and not state.run.stop_task.done()
        assert runtime.metadata["activeRunId"] == state.run.run_id
        assert await driver.run_is_active(runtime) is True
        assert driver.active_run_count() == 1
        with pytest.raises(TurnAlreadyRunningError):
            await driver.send_message(runtime, MessageInput(text="next"))
        assert collected[-1].event.data == {
            "stopStatus": "running", "reason": STOP_UNCONFIRMED_REASON,
        }
        assert any(e.event.type == "diagnostic.notice" and "running" in e.event.message for e in collected)


async def test_a_stop_stuck_in_the_transitional_status_remains_active(rig) -> None:
    driver, project, binding = await _ready(rig)
    async with _unconfirmed_turn(
        rig, driver, project, binding, lambda: stop_never_confirms_script("stopping")
    ) as (runtime, conversation, _, _):
        assert await driver.run_is_active(runtime) is True
        assert driver.debug_last_stream(conversation.id)["stop"]["polled"][-1] == "stopping"


async def test_the_stop_call_itself_returns_at_once(rig) -> None:
    """`POST /interrupt` 不该等满那 30 秒。

    等一个终态最多 30s 是对的，但那一段跑在后台任务里：用户点一次停止要等
    半分钟才拿到 HTTP 响应是另一种坏（页面此刻要的只是「请求已发出」）。终态
    照常走事件流到达。
    """
    driver, project, binding = await _ready(rig, timeout=3.0, interval=0.1)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    rig.servers["default"].set_script(stop_never_confirms_script)
    try:
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        started = time.monotonic()
        await asyncio.wait_for(driver.interrupt(runtime), timeout=1.0)
        assert time.monotonic() - started < 0.5
        # 期限之后只到达提示，不能提前宣布停止成功。
        while True:
            envelope = await asyncio.wait_for(stream.__anext__(), timeout=10.0)
            assert envelope.event.type not in TERMINALS
            if envelope.event.type == "extension.event" and envelope.event.name == "run.stop_unconfirmed":
                break
        assert await driver.run_is_active(runtime) is True
    finally:
        await driver.stop_runtime(runtime)


async def test_the_interrupt_capability_degrades_to_unverified_after_that(rig) -> None:
    """AD-130：一次没确认就足以推翻「immediate」这句声明（只降不升）。"""
    driver, project, binding = await _ready(rig)
    async with _unconfirmed_turn(rig, driver, project, binding):
        capabilities = await driver.get_capabilities()
        assert capabilities.card.interrupt.value == "unverified"


async def test_the_driver_polls_until_the_engine_settles(rig) -> None:
    """引擎只是慢：先 ``stopping``，几百毫秒后落 ``cancelled``（规格 §5 的 1s 轮询）。

    这一支必须是**正常的**中断收尾（``reason="stopped"``），不能被兜底抢走。
    """
    driver, project, binding = await _ready(rig, timeout=5.0, interval=0.05)
    runtime, conversation, collected = await _stopped_turn(
        rig, driver, project, binding, lambda: stop_stalls_script(0.3)
    )

    terminal = collected[-1]
    assert terminal.event.type == "run.interrupted"
    assert terminal.event.reason == "stopped"
    record = driver.debug_last_stream(conversation.id)["stop"]
    assert record["outcome"] == "polled_terminal"
    # 轮询确实读到过过渡态，最后一次才是终态。
    assert "stopping" in record["polled"]
    assert record["polled"][-1] == "cancelled"


async def test_a_terminal_arriving_on_the_sse_wins(rig) -> None:
    """`/stop` 回过渡态，但 SSE 随后把这一轮跑完了 → 正常收尾，不合成中断。"""
    driver, project, binding = await _ready(rig, timeout=5.0, interval=0.05)
    _, conversation, collected = await _stopped_turn(
        rig, driver, project, binding, lambda: stop_then_completes_script(0.1)
    )

    assert collected[-1].event.type == "run.completed"
    assert [e.event.type for e in collected].count("run.interrupted") == 0
    record = driver.debug_last_stream(conversation.id)["stop"]
    assert record["outcome"] in ("sse_terminal", "polled_terminal")


async def test_stop_endpoint_404_is_still_unsupported_not_a_silent_stop(rig) -> None:
    """停止接口不可用（404/405/501）仍旧抛 ``UnsupportedCapabilityError`` → 接入层 501。

    AD-147 只改「2xx 之后怎么等」，没有把这条路变软：没有停止接口就是没有，
    不能假装停了，收尾权也要还给 SSE 泵。
    """
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    server.set_script(stop_never_confirms_script)
    try:
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        first = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        assert first.event.type == "run.started"
        # 让 `/stop` 打到一个不存在的 run 上（真机上的 404 既可能是「没有这个
        # 端点」也可能是「不认识这个 run」，两者都不该被当成「已中断」）。
        server.runs.clear()
        with pytest.raises(UnsupportedCapabilityError):
            await asyncio.wait_for(driver.interrupt(runtime), timeout=5.0)
        state = driver._runtimes[runtime.runtime_id]  # noqa: SLF001 - 收尾权归属是内部不变量
        assert state.run.stop_owns_finalize is False
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# 第 2 件：取证（`DASH_DEBUG=1` 的 last-stream 多一段 stop）
# --------------------------------------------------------------------------- #


async def test_the_stop_forensics_are_status_words_only(rig) -> None:
    """``stop: {responseStatus, bodyStatus, polled, outcome}``——只有状态词。

    下一次真机就靠它确认 Hermes 的 `/stop` 到底回什么。里面不能出现任何内容：
    助手正文（run 对象的 ``output``）、session id、用量一个都不许带。
    """
    driver, project, binding = await _ready(rig)
    async with _unconfirmed_turn(rig, driver, project, binding) as (runtime, conversation, _, _):
        record = driver.debug_last_stream(conversation.id)["stop"]
        assert set(record) == {"responseStatus", "bodyStatus", "polled", "outcome"}
        assert record["responseStatus"] == 200
        assert record["bodyStatus"] == "running"
        assert record["outcome"] == "stop_unconfirmed"
        assert record["polled"] and set(record["polled"]) == {"running"}
        blob = repr(record)
        assert runtime.native_session_id
        assert runtime.native_session_id not in blob
        assert "output" not in blob


async def test_a_run_that_was_never_stopped_has_no_stop_section(rig) -> None:
    """没点过停止就没有这一段（不编：N §13.1）。"""
    from drivers.hermes.testing.fake_api_server import run3_script

    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    rig.servers["default"].set_script(run3_script)
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        async for envelope in driver.events(runtime):
            if envelope.event.type in TERMINALS:
                break
    finally:
        await driver.stop_runtime(runtime)
    assert driver.debug_last_stream(conversation.id)["stop"] is None


# --------------------------------------------------------------------------- #
# 第 3 件的 Driver 一侧：`run_is_active` 是自愈的判据
# --------------------------------------------------------------------------- #


async def test_run_is_active_answers_from_the_status_endpoint(rig) -> None:
    """Session Host 的 60s 自愈只在**明确的 False** 上动手，所以这条面不许猜。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    server.set_script(stop_never_confirms_script)
    try:
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        # 还在跑（`started`）。
        assert await driver.run_is_active(runtime) is True
        # 网关不认识这个 run 了 → 明确的「不在跑」。
        server.runs.clear()
        assert await driver.run_is_active(runtime) is False
    finally:
        await driver.stop_runtime(runtime)


async def test_late_sse_completion_after_timeout_still_delivers_final_text(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    async with _unconfirmed_turn(
        rig, driver, project, binding, lambda: stop_then_completes_script(0.25)
    ) as (runtime, _, stream, collected):
        terminal = await _collect_terminal(stream, collected)
        assert terminal.event.type == "run.completed"
        assert any(e.event.type == "message.completed" and e.event.text == "ok" for e in collected)
        assert sum(e.event.type in TERMINALS for e in collected) == 1
        assert await driver.run_is_active(runtime) is False
        assert runtime.metadata["activeRunId"] is None
        assert driver.active_run_count() == 0


async def test_late_status_completion_is_still_polled_after_timeout(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    async with _unconfirmed_turn(
        rig, driver, project, binding, lambda: stop_stalls_script(0.4)
    ) as (runtime, _, stream, collected):
        terminal = await _collect_terminal(stream, collected)
        assert terminal.event.type == "run.interrupted"
        assert terminal.event.reason == "stopped"
        assert sum(e.event.type == "extension.event" and e.event.name == "run.stop_unconfirmed" for e in collected) == 1
        assert await driver.run_is_active(runtime) is False


async def test_repeated_stop_reuses_the_same_monitor(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    async with _unconfirmed_turn(rig, driver, project, binding) as (runtime, _, _, _):
        run = driver._runtimes[runtime.runtime_id].run
        monitor = run.stop_task
        await asyncio.gather(*(driver.interrupt(runtime) for _ in range(3)))
        assert run.stop_task is monitor
        assert monitor is not None and not monitor.done()
        stops = [path for method, path, _ in rig.servers["default"].requests if method == "POST" and path.endswith("/stop")]
        assert len(stops) == 1
        assert await driver.run_is_active(runtime) is True


async def test_closed_remote_stream_does_not_release_an_unconfirmed_run(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    async with _unconfirmed_turn(
        rig, driver, project, binding, lambda: stop_stalls_script(0.5)
    ) as (runtime, _, stream, collected):
        run = driver._runtimes[runtime.runtime_id].run
        # 这个网关会先关闭 SSE，再经过更长时间把原生状态改为 cancelled。
        await asyncio.wait_for(asyncio.shield(run.task), timeout=1)
        assert not run.finalized
        assert driver.active_run_count() == 1
        with pytest.raises(TurnAlreadyRunningError):
            await driver.send_message(runtime, MessageInput(text="must wait"))
        assert (await _collect_terminal(stream, collected)).event.type == "run.interrupted"
        assert await driver.run_is_active(runtime) is False


async def test_disappeared_remote_run_reports_failure_instead_of_cancelled(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    async with _unconfirmed_turn(rig, driver, project, binding) as (runtime, _, stream, collected):
        rig.servers["default"].runs.clear()
        terminal = await _collect_terminal(stream, collected)
        assert terminal.event.type == "run.failed"
        assert terminal.event.error.code == "hermes.run_unavailable"
        assert terminal.event.error.retriable is False
        assert await driver.run_is_active(runtime) is False


async def test_parallel_stop_clicks_before_the_first_response_send_once(rig) -> None:
    driver, project, binding = await _ready(rig, timeout=0.08, interval=0.02)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    rig.servers["default"].set_script(stop_never_confirms_script)
    try:
        await driver.send_message(runtime, MessageInput(text="sleep"))
        await asyncio.gather(*(driver.interrupt(runtime) for _ in range(3)))
        stops = [path for method, path, _ in rig.servers["default"].requests if method == "POST" and path.endswith("/stop")]
        assert len(stops) == 1
    finally:
        await driver.stop_runtime(runtime)
