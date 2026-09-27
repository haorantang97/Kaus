"""批次十八（Driver 侧）：停止确认（AD-130 补）与工具回填按真机形状。

对应任务书第 2 件与第 3 件。纪律同批次十六/十七：只用假 gateway
（``fake_api_server``）与临时 ``HERMES_HOME``，一行都不碰真实 ``~/.hermes``，
也不读任何凭据文件。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from drivers.base import MessageInput, NativeHistoryEntry
from drivers.hermes import history as history_mod
from drivers.hermes.driver import TERMINAL_STOP_STATUSES
from drivers.hermes.testing.fake_api_server import (
    RUN4_MESSAGES,
    hold_script,
    run3_script,
)
from drivers.hermes.testing.harness import FakeHermesHarness


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


async def _ready(rig):
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    return driver, project, binding


async def _drain(driver, runtime, *, limit: int = 60):
    envelopes = []
    async for envelope in driver.events(runtime):
        envelopes.append(envelope)
        if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
            break
        if len(envelopes) >= limit:
            break
    return envelopes


# --------------------------------------------------------------------------- #
# 第 2 件：停止确认（AD-130 补 / 规格 §2.0）
# --------------------------------------------------------------------------- #


async def test_stop_with_a_terminal_body_synthesizes_the_terminal_at_once(rig) -> None:
    """规格 §2.0 实测：``POST /stop`` 回 200 + 完整 run 对象。

    真机 ② 的症状是「点了停止，5 秒内没有终态事件，页面提示引擎没有确认」——
    而那份响应体里早就写着这一轮已经 ``cancelled``。既然如此就当场收尾。
    """
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.set_script(hold_script)
        stream = driver.events(runtime)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        first = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        assert first.event.type == "run.started"
        run_id = first.run_id

        await asyncio.wait_for(driver.interrupt(runtime), timeout=8.0)
        collected = []
        while True:
            envelope = await asyncio.wait_for(stream.__anext__(), timeout=8.0)
            collected.append(envelope)
            if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
                break
        assert collected[-1].event.type == "run.interrupted"
        assert collected[-1].event.reason == "stopped"
        # 收尾用的是 /stop 的响应体，不必再多跑一次 GET /v1/runs/{id}。
        assert (
            sum(
                1
                for method, path, _ in server.requests
                if method == "GET" and path.endswith(f"/runs/{run_id}")
            )
            == 0
        )
    finally:
        await driver.stop_runtime(runtime)


def test_only_real_terminal_statuses_count_as_confirmation() -> None:
    """``stopping`` 是过渡态（规格 §3.4），不算确认；``stopped`` 归一成 cancelled。"""
    from drivers.hermes.driver import _terminal_run_object

    class _Response:
        def __init__(self, body, ok=True):
            self._body = body
            self.ok = ok

        def json(self):
            return self._body

    assert "stopping" not in TERMINAL_STOP_STATUSES
    assert _terminal_run_object(_Response({"status": "stopping"})) is None
    assert _terminal_run_object(_Response({"status": "cancelled"}))["status"] == "cancelled"
    assert _terminal_run_object(_Response({"status": "stopped"}))["status"] == "cancelled"
    # 请求没发出去 / 不是 200 / body 里没有 status —— 一律维持原来的等待逻辑。
    assert _terminal_run_object(None) is None
    assert _terminal_run_object(_Response({"status": "cancelled"}, ok=False)) is None
    assert _terminal_run_object(_Response({"object": "hermes.run"})) is None


# --------------------------------------------------------------------------- #
# 第 3 件：工具回填按真机形状（`docs/quality/verify-next.md` 末尾的取证 JSON）
# --------------------------------------------------------------------------- #


def _entries(rows):
    return [history_mod.to_history_entry(history_mod.normalize_message_row(r)) for r in rows]


def test_tool_calls_pair_by_the_real_call_id() -> None:
    """真机形状：``tool_calls`` 是数组、``id`` 是 number、结果行按 ``tool_call_id`` 配对。"""
    calls, results = history_mod.tool_calls_from_history(_entries(RUN4_MESSAGES))
    assert [call.call_id for call in calls] == ["call_5mJFQcWjkPeHY0wNYmOjkjCW"]
    assert calls[0].name == "terminal"
    # 完整入参来自 ``function.arguments``（JSON 字符串 → 结构）。
    assert calls[0].arguments == {"command": "pwd && ls | head -5"}
    assert set(results) == {"call_5mJFQcWjkPeHY0wNYmOjkjCW"}


def test_a_json_content_yields_the_output_field_not_the_envelope() -> None:
    """结果行的 ``content`` 是 ``{"output": "…"}``：卡片要的是里面那段，不是外壳。"""
    _, results = history_mod.tool_calls_from_history(_entries(RUN4_MESSAGES))
    output = results["call_5mJFQcWjkPeHY0wNYmOjkjCW"]
    assert isinstance(output, str)
    assert output.startswith("/Users/example")
    assert "AGENTS.md" in output


def test_unparsable_content_falls_back_to_the_raw_string() -> None:
    """解析不了就原样给字符串——比给一张空工具卡诚实（N §13.1）。"""
    entry = NativeHistoryEntry(
        entry_id="1",
        role="tool",
        kind="tool_result",
        text="not json at all {",
        metadata={"toolCallId": "call_x"},
    )
    _, results = history_mod.tool_calls_from_history([entry])
    assert results["call_x"] == "not json at all {"
    # 合法 JSON 但没有 output 键 → 原样给结构，不猜哪个字段是输出。
    other = entry.model_copy(update={"text": '{"stdout": "hi"}'})
    _, structured = history_mod.tool_calls_from_history([other])
    assert structured["call_x"] == {"stdout": "hi"}


async def test_the_backfill_uses_the_real_machine_shape_end_to_end(rig) -> None:
    """整条链路：真机形状的历史 → ``tool.updated`` 上带得出入参与输出。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.seed_run4_messages(runtime.native_session_id)
        server.set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="go"))
        envelopes = await asyncio.wait_for(_drain(driver, runtime), timeout=10.0)
        updates = [e for e in envelopes if e.event.type == "tool.updated"]
        assert updates, "工具回填没发出来"
        assert updates[-1].event.output.startswith("/Users/example")
        types = [e.event.type for e in envelopes]
        # 批次十六定的顺序：回填必须排在终态之前。
        assert types.index("tool.updated") < types.index("run.completed")
    finally:
        await driver.stop_runtime(runtime)
