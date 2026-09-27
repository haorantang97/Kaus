"""批次十六（Driver 侧）：失败修法 / 中断真实行为 / 工具输出回填 / 会话级模型。

对应任务书四件里落在 ``drivers/hermes/`` 的部分。每个用例都只用假 gateway
（``fake_api_server``）与临时 ``HERMES_HOME``，一行都不碰真实 ``~/.hermes``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.base import DriverError, MessageInput, UnsupportedCapabilityError
from drivers.hermes import failure_hints
from drivers.hermes.driver import HermesDriver
from drivers.hermes.supervisor import GatewayConfig, HermesGatewaySupervisor
from drivers.hermes.testing.fake_api_server import (
    CAPABILITIES,
    RUN3_MESSAGES,
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


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 第 1 件：两种 hint
# --------------------------------------------------------------------------- #


async def test_gateway_down_probe_message_tells_how_to_start_it(tmp_path: Path) -> None:
    """网关没在跑 → probe 的 message 是人话 + 「怎么起」（走查 F5）。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("API_SERVER_KEY=k\n", encoding="utf-8")
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home,
            port=_free_port(),  # 没人在这个端口上听
            key_ref=f"hermes-env:{home}/.env#API_SERVER_KEY",
            mode="adopted",
        )
    )
    status = await supervisor.ensure_ready()
    assert status.state == "unavailable"
    assert status.failure is not None
    assert status.failure.code == failure_hints.GATEWAY_UNREACHABLE
    described = status.describe() or ""
    assert "hermes gateway run" in described
    assert "API_SERVER_PORT" in described


async def test_wrong_key_probe_message_points_at_the_key_ref(
    tmp_path: Path, rig
) -> None:
    """key 不匹配 → 修法是「核对 key_ref 指向的 profile」，不是「再试一次」。"""
    server = rig.server_for("default")
    home = tmp_path / "wrong-home"
    home.mkdir()
    (home / ".env").write_text("API_SERVER_KEY=not-the-key\n", encoding="utf-8")
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home,
            port=server.port,
            key_ref=f"hermes-env:{home}/.env#API_SERVER_KEY",
            mode="adopted",
        )
    )
    status = await supervisor.ensure_ready()
    assert status.state == "degraded"
    assert status.failure is not None
    assert status.failure.code == failure_hints.GATEWAY_KEY_MISMATCH
    assert "key_ref" in (status.describe() or "")


async def test_start_runtime_carries_the_hint_to_the_caller(
    tmp_path: Path, rig
) -> None:
    """``start_runtime`` 起不来时，修法要跟着异常一起走到接入层。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    rig.servers["default"].stop()  # 网关中途没了
    with pytest.raises(DriverError) as excinfo:
        await driver.start_runtime(conversation, "card")
    failure = excinfo.value.failure
    assert failure is not None and failure.code == failure_hints.GATEWAY_UNREACHABLE
    assert "hermes gateway run" in (failure.hint or "")


# --------------------------------------------------------------------------- #
# 第 2 件：中断的真实行为（规格 §2.0：POST /v1/runs/{id}/stop）
# --------------------------------------------------------------------------- #


async def test_interrupt_posts_run_stop_and_converges(rig) -> None:
    """真机上停止接口是存在的（实测 200 + run 对象）→ **不**抛不支持。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.set_script(hold_script)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        await driver.interrupt(runtime)
        stops = [path for method, path, _ in server.requests if method == "POST" and path.endswith("/stop")]
        assert stops, "interrupt 必须真的打过 /stop"
    finally:
        await driver.stop_runtime(runtime)


async def test_interrupt_without_run_stop_capability_is_unsupported(rig) -> None:
    """网关没声明 ``run_stop`` → 抛 UnsupportedCapabilityError，不静默成功。"""
    features = dict(CAPABILITIES["features"])
    features["run_stop"] = False
    capabilities = {**CAPABILITIES, "features": features}
    rig.server_for("default").capabilities = capabilities
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(hold_script)
        await driver.send_message(runtime, MessageInput(text="sleep"))
        with pytest.raises(UnsupportedCapabilityError):
            await driver.interrupt(runtime)
    finally:
        await driver.stop_runtime(runtime)


async def test_note_interrupt_unconfirmed_downgrades_the_capability(rig) -> None:
    """真机没确认中断 → ``card.interrupt`` 降为 unverified + verification=live。"""
    driver, _project, _binding = await _ready(rig)
    before = await driver.get_capabilities()
    assert before.card.interrupt.value == "immediate"
    driver.note_interrupt_unconfirmed()
    after = await driver.get_capabilities()
    assert after.card.interrupt.value == "unverified"
    assert after.card.interrupt.verification == "live"
    # 再探测一次也不会把证据擦掉——协商只会再说一遍那句被推翻的话。
    await driver.probe()
    assert (await driver.get_capabilities()).card.interrupt.value == "unverified"


# --------------------------------------------------------------------------- #
# 第 3 件：工具输出回填
# --------------------------------------------------------------------------- #


async def _run_and_collect(driver, runtime, text: str = "go"):
    await driver.send_message(runtime, MessageInput(text=text))
    envelopes = []
    async for envelope in driver.events(runtime):
        envelopes.append(envelope)
        if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
            break
    return envelopes


async def test_tool_output_is_backfilled_from_native_history(rig) -> None:
    """回合结束后补一条 ``tool.updated``：完整入参 + 真实结果 + cumulative。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.seed_run3_messages(runtime.native_session_id)
        server.set_script(run3_script)
        envelopes = await _run_and_collect(driver, runtime)
        updates = [e for e in envelopes if e.event.type == "tool.updated"]
        assert len(updates) == 1
        event = updates[0].event
        # FIFO 配对：本 run 只有一次 tool.started，对上历史里最后一条工具调用。
        started = [e for e in envelopes if e.event.type == "tool.started"]
        assert event.call_id == started[0].event.call_id
        assert event.cumulative is True
        # 批次十八第 3 件：结果行的 content 是 `{"output": …}`，卡片要的是里面
        # 那段输出，不是包着它的那层信封。
        assert event.output == "HERMES-PROBE-TOOL-MARKER"
        assert event.progress["input"] == {"command": "echo HERMES-PROBE-TOOL-MARKER"}
        assert event.progress["nativeCallId"] == "call_00_smUvuG9hSJ7fY6kcQmcR1565"
        assert event.progress["source"] == "native_history"
        # 顺序：补账必须排在终态之前，否则 reducer 会把它当迟到事件丢掉。
        types = [e.event.type for e in envelopes]
        assert types.index("tool.updated") < types.index("run.completed")
    finally:
        await driver.stop_runtime(runtime)


async def test_no_backfill_when_history_has_no_tool_calls(rig) -> None:
    """读不到就不发，不猜（历史里没有工具调用行时一条 tool.updated 都没有）。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        # 只铺用户与助手两行——工具调用行缺席（被压缩、或分页没覆盖到）。
        server.seed_messages(
            runtime.native_session_id,
            [RUN3_MESSAGES[0], RUN3_MESSAGES[3]],
        )
        server.set_script(run3_script)
        envelopes = await _run_and_collect(driver, runtime)
        assert [e for e in envelopes if e.event.type == "tool.updated"] == []
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# 第 4 件：会话级模型
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locked, expected", [(False, "unsupported"), (True, "supported")])
async def test_conversation_model_support_follows_gateway_capabilities(rig, locked, expected) -> None:
    server = rig.server_for("default")
    server.capabilities = {**server.capabilities, "features": {**server.capabilities["features"], "session_model_lock": locked}}
    driver, _project, _binding = await _ready(rig)
    capabilities = await driver.get_capabilities()
    assert capabilities.models.conversation_scoped.value == expected
    # 模型下拉本身照旧可用（改的是 Binding 默认）——两根轴不是一回事。
    assert capabilities.models.mode == "constrained"


async def test_send_message_adopts_a_conversation_model_it_cannot_honour(rig) -> None:
    """会话快照与有效模型不一致 → **AD-155（批次三十一）改判为采纳，不再拦。**

    这条测试原来断言的是「抛不支持」。改判的理由在
    :meth:`HermesDriver._model_adoption` 的 docstring 里：快照过期不是用户的错，
    拦下去会让物化过配置的项目里**所有**旧会话一句话都发不出。
    ``PATCH /conversations/{id}`` 那条显式换模型的路仍旧 501（见
    ``test_conversation_model_support_follows_gateway_capabilities`` 与接入层
    测试）。
    """
    server = rig.server_for("default")
    server.capabilities = {**server.capabilities, "features": {**server.capabilities["features"], "session_model_lock": False}}
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding).evolve(
        model_id="some-other-model"
    )
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="hi"))
        assert any(
            method == "POST" and path.endswith("/runs")
            for method, path, _ in server.requests
        )
    finally:
        await driver.stop_runtime(runtime)
