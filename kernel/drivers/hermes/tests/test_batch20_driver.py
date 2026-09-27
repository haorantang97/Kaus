"""批次二十第 1/3 件：回合收尾丢失（AD-146）与 SSE 流健康自检。

真机症状（`docs/quality/verify-after-restart.md`）：中途的工具事件到得了，
回合末尾的助手正文与终态**一条都到不了**，页面永远 Running，最后被 Session Host
自愈成 ``runtime_lost``。

根因两层，本文件把两层都锁住：

1. **Driver 从不主动收流。** 规格 §3.4 写明「终态后 Driver 关闭该 run 的 SSE」，
   §3.6 引的文档也说未被消费的事件缓冲要五分钟才过期（就是为了防「detached
   client」）——**关流是客户端的事**。而 ``_pump_run`` 只在服务端把 body 收掉时
   才跑到它的 ``finally``，于是真机上收尾永远不发生。此前测不出来，是因为假网关
   一放完 ``run.completed`` 就写终止 chunk，替 Driver 把流关了；现在夹具照真机
   挂着（``RunScript.linger``）。
2. **收尾可以被工具回填拖死。** 回填要读一次原生历史，那条路上任何异常都会从
   ``_finalize_run`` 逃出去，把末尾正文与终态一起吞掉——而它跑在一个独立 Task 的
   ``finally`` 上，异常连日志都只是「Task exception was never retrieved」。

纪律同前几批：只用假 gateway + 临时 ``HERMES_HOME``，不碰真实 ``~/.hermes``，
不读任何凭据。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from drivers.base import MessageInput
from drivers.hermes import history as history_mod
from drivers.hermes.testing.fake_api_server import (
    RUN3_OUTPUT,
    RUN4_MESSAGES,
    run3_script,
)
from drivers.hermes.testing.harness import FakeHermesHarness

TERMINALS = ("run.completed", "run.failed", "run.interrupted")


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


async def _drain(driver, runtime, *, limit: int = 80):
    envelopes = []
    async for envelope in driver.events(runtime):
        envelopes.append(envelope)
        if envelope.event.type in TERMINALS:
            break
        if len(envelopes) >= limit:
            break
    return envelopes


async def _one_turn(rig, driver, project, binding, *, timeout: float = 10.0):
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        return runtime, await asyncio.wait_for(_drain(driver, runtime), timeout=timeout)
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# 第 1 件 A：关流是 Driver 的事（AD-146 的根因）
# --------------------------------------------------------------------------- #


async def test_the_driver_closes_the_stream_itself_after_run_completed(rig) -> None:
    """网关**不**关流，回合照样必须在 ``run.completed`` 之后立刻收尾。

    夹具在放完 ``run.completed`` 之后还会挂 ``linger`` 秒（照真机 0.21 的样子）。
    如果 Driver 还是「等服务端关流」，这一轮的终态就要迟到整整 ``linger`` 秒；
    真机上那个上界是五分钟的缓冲过期，也就是**永远不到**。
    """
    driver, project, binding = await _ready(rig)
    server = rig.servers["default"]
    server.set_script(run3_script)
    started = time.monotonic()
    _, envelopes = await _one_turn(rig, driver, project, binding)
    elapsed = time.monotonic() - started

    types = [e.event.type for e in envelopes]
    assert types[-1] == "run.completed"
    assert "message.completed" in types
    # linger 是 2s：收尾必须**明显**早于它，否则就是又在等服务端关流。
    assert elapsed < 1.5, f"收尾等到了服务端关流（{elapsed:.2f}s）"


async def test_the_final_text_comes_from_the_run_object_even_without_deltas(rig) -> None:
    """回合末尾的正文只有一个权威来源：``GET /v1/runs/{id}.output``（规格 §3.4）。

    真机那条 Media 会话一条 ``message.delta`` 都没有（provider 不流式），于是
    ``message.completed`` 是**唯一**的助手正文——收尾一丢，页面上就一个字都没有。
    """
    driver, project, binding = await _ready(rig)
    server = rig.servers["default"]
    script = run3_script()
    # 只留工具事件与 run.completed：真机那一轮就是这个形状。
    script.events = tuple(
        e for e in script.events if e.get("event") != "message.delta"
    )
    server.set_script(lambda: script)
    _, envelopes = await _one_turn(rig, driver, project, binding)

    types = [e.event.type for e in envelopes]
    assert "message.delta" not in types
    completed = [e for e in envelopes if e.event.type == "message.completed"]
    assert completed, "没有 delta 时末尾正文必须由收尾补出来"
    assert completed[-1].event.text == RUN3_OUTPUT
    assert types[-1] == "run.completed"


# --------------------------------------------------------------------------- #
# 第 1 件 B：收尾不可被回填拖死（AD-146 的三条端到端）
# --------------------------------------------------------------------------- #


async def test_a_500_from_the_history_endpoint_still_ends_the_turn(rig) -> None:
    """① 历史端点 500：补不了工具账，但正文与终态一条都不许少。"""
    driver, project, binding = await _ready(rig)
    server = rig.servers["default"]
    server.set_script(run3_script)
    server.fail_messages(500)
    _, envelopes = await _one_turn(rig, driver, project, binding)

    types = [e.event.type for e in envelopes]
    assert "message.completed" in types
    assert types[-1] == "run.completed"


async def test_a_fifo_count_mismatch_still_ends_the_turn(rig) -> None:
    """② 历史是真机形状但配不上：一条都不补（不张冠李戴），终态照发。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        # 真机形状的历史，但把带 tool_calls 的那一行拿掉 → 历史里 0 条调用，
        # 本回合 1 条 → 数量对不上。
        server.seed_messages(
            runtime.native_session_id,
            [row for row in RUN4_MESSAGES if not row.get("tool_calls")],
        )
        server.set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="go"))
        envelopes = await asyncio.wait_for(_drain(driver, runtime), timeout=10.0)
    finally:
        await driver.stop_runtime(runtime)

    types = [e.event.type for e in envelopes]
    assert "tool.updated" not in types, "配不上就一条都不该补"
    assert "message.completed" in types
    assert types[-1] == "run.completed"


async def test_an_exploding_history_parser_still_ends_the_turn(rig, monkeypatch) -> None:
    """③ 解析原生历史时抛异常（未知形状）：只降级成一条 info，终态照发。

    直接把解析函数换成会抛的那种——真机上「响应形状不认识」最终就落在这里，而
    这一条正是 AD-146 之前把 ``message.*`` 与 ``run.completed`` 一起吞掉的路径。
    """
    driver, project, binding = await _ready(rig)
    server = rig.servers["default"]
    server.set_script(run3_script)

    def _boom(_entries):
        raise ValueError("未知的历史形状")

    monkeypatch.setattr(history_mod, "tool_calls_from_history", _boom)
    _, envelopes = await _one_turn(rig, driver, project, binding)

    types = [e.event.type for e in envelopes]
    assert "message.completed" in types
    assert types[-1] == "run.completed"
    notices = [
        e for e in envelopes if e.event.type == "diagnostic.notice" and e.event.level == "info"
    ]
    assert notices, "回填出错要留一条 info，不能静悄悄"


# --------------------------------------------------------------------------- #
# 第 3 件：SSE 流健康自检（只有类型与计数）
# --------------------------------------------------------------------------- #


async def test_the_last_stream_record_has_types_and_counts_only(rig) -> None:
    """取证记录里只有事件名与计数——没有 delta 文本、没有 preview、没有输出。"""
    driver, project, binding = await _ready(rig)
    server = rig.servers["default"]
    server.set_script(run3_script)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        await asyncio.wait_for(_drain(driver, runtime), timeout=10.0)
    finally:
        await driver.stop_runtime(runtime)

    record = driver.debug_last_stream(conversation.id)
    assert record is not None
    assert record["counts"]["tool.started"] == 1
    assert record["counts"]["message.delta"] == 12
    assert record["counts"]["run.completed"] == 1
    assert record["sawRunCompleted"] is True
    assert record["total"] == len(record["eventTypes"])
    blob = repr(record)
    # 只有事件名：正文片段、工具名、命令 preview 一个都不许在里面。
    for content in (RUN3_OUTPUT, "echo ", "terminal", "preview"):
        assert content not in blob
    assert set(record["eventTypes"]) <= {
        "tool.started",
        "tool.completed",
        "message.delta",
        "reasoning.available",
        "run.completed",
    }
    assert driver.debug_last_stream("conversation:does-not-exist") is None
