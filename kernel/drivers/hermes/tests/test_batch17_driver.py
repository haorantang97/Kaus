"""批次十七（Driver 侧）：有效模型比较 / 工具回填取证 / 历史字段名放宽。

对应任务书第 3 件与第 4 件里落在 ``drivers/hermes/`` 的部分。与批次十六同一条
纪律：只用假 gateway（``fake_api_server``）与临时 ``HERMES_HOME``，一行都不碰
真实 ``~/.hermes``，也不读任何凭据文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.base import MessageInput, UnsupportedCapabilityError
from drivers.hermes import history as history_mod
from drivers.hermes.testing.fake_api_server import RUN3_MESSAGES, run3_script
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


def _write_engine_model(rig, model_id: str, *, profile: str = "default") -> None:
    """在沙盒 HERMES_HOME 里写一份只含 ``model`` 的 ``config.yaml``。

    真机上 Media 这类 profile 的模型正是这么来的：Binding 行是空的，值在引擎
    自己的配置里（HANDOFF §3）。**不写任何凭据键**。
    """
    home = rig._home_for(profile)  # noqa: SLF001 - 夹具内部路径
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f'model:\n  default: "{model_id}"\n', encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 第 3 件：快照 vs **有效**模型
# --------------------------------------------------------------------------- #


async def test_snapshot_equal_to_the_effective_model_is_accepted(rig) -> None:
    """真机 500 的复现：Binding 默认为空、模型来自引擎配置、会话快照同值。

    旧口径拿快照与 ``binding.default_model_id``（None）比，判成「不一致」→
    整条消息发不出去（`docs/quality/verify5.md` ②）。两个值说的其实是同一个
    模型，必须放行。
    """
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-sol")
    assert binding.default_model_id is None
    assert await driver.effective_model_id(binding) == "gpt-5.6-sol"

    conversation = rig.make_conversation(project, binding).evolve(
        model_id="gpt-5.6-sol"
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


async def test_snapshot_that_differs_raises_a_structured_unsupported(rig) -> None:
    """**AD-155 改判（批次三十一）**：快照与有效模型不一致时不再拦发送。

    这条测试原来断言的是「抛 ``conversation_model_unsupported``」。真机上那条
    规则的后果是：项目配置一物化，所有旧会话立刻发不出话。现在改为采纳——
    这里只守住「不抛」，采纳的完整形状（事件顺序、快照落库）在
    ``test_batch31_model_adopt.py``。
    """
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-sol")
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


# --------------------------------------------------------------------------- #
# 第 4 件：回填取证
# --------------------------------------------------------------------------- #


async def _run_and_collect(driver, runtime, text: str = "go"):
    await driver.send_message(runtime, MessageInput(text=text))
    envelopes = []
    async for envelope in driver.events(runtime):
        envelopes.append(envelope)
        if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
            break
    return envelopes


def _with_diagnostics(rig, driver, project):
    """一条打开了 ``tool_backfill_diagnostics`` 的 Binding。"""
    binding = rig.make_binding(project, driver)
    switched = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "tool_backfill_diagnostics": True,
            }
        }
    )
    driver.register_binding(switched)
    return switched


async def test_backfill_reports_how_many_it_filled(rig) -> None:
    """补齐成功时留一条 ``diagnostic.notice(info)``：本回合 M 条、历史 N 条、补了 K 条。"""
    driver = rig.make_driver()
    project = rig.make_project()
    await driver.probe()
    binding = _with_diagnostics(rig, driver, project)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.seed_run3_messages(runtime.native_session_id)
        server.set_script(run3_script)
        envelopes = await _run_and_collect(driver, runtime)
        notices = [
            e.event.message
            for e in envelopes
            if e.event.type == "diagnostic.notice" and "补齐" in e.event.message
        ]
        assert notices and "已从原生历史补齐 1 条" in notices[-1]
        # 取证絮语也必须排在终态之前，否则 reducer 当迟到事件丢掉。
        types = [e.event.type for e in envelopes]
        assert types.index("diagnostic.notice") < types.index("run.completed")
    finally:
        await driver.stop_runtime(runtime)


async def test_backfill_reports_a_count_mismatch_instead_of_guessing(rig) -> None:
    """历史里的调用数不够 → 一条都不补，但要说出「N vs M」。"""
    driver = rig.make_driver()
    project = rig.make_project()
    await driver.probe()
    binding = _with_diagnostics(rig, driver, project)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        # 工具调用行缺席（被压缩 / 分页没覆盖到）。
        server.seed_messages(
            runtime.native_session_id, [RUN3_MESSAGES[0], RUN3_MESSAGES[3]]
        )
        server.set_script(run3_script)
        envelopes = await _run_and_collect(driver, runtime)
        assert [e for e in envelopes if e.event.type == "tool.updated"] == []
        notices = [
            e.event.message for e in envelopes if e.event.type == "diagnostic.notice"
        ]
        assert any("0 条 < 本回合的 1 条" in message for message in notices)
    finally:
        await driver.stop_runtime(runtime)


async def test_backfill_diagnostics_are_off_by_default(rig) -> None:
    """默认关：正常用户的时间线上不该出现取证絮语（日志里仍然有）。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.seed_run3_messages(runtime.native_session_id)
        server.set_script(run3_script)
        envelopes = await _run_and_collect(driver, runtime)
        assert [e for e in envelopes if e.event.type == "tool.updated"]
        assert [e for e in envelopes if e.event.type == "diagnostic.notice"] == []
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# 第 4 件：历史字段名放宽（规格 §4.2 的响应体字段名是 [未验证]）
# --------------------------------------------------------------------------- #


def test_normalize_row_accepts_camel_case_and_nested_metadata() -> None:
    """``tool_calls`` 在顶层驼峰、或藏在 ``metadata`` 下，都要认。"""
    camel = history_mod.normalize_message_row(
        {"id": 1, "role": "assistant", "toolCalls": [{"id": "call_1"}]}
    )
    assert camel["tool_calls"] == [{"id": "call_1"}]

    nested = history_mod.normalize_message_row(
        {
            "id": 2,
            "role": "tool",
            "content": "{}",
            "metadata": {"tool_call_id": "call_1", "tool_name": "terminal"},
        }
    )
    assert nested["tool_call_id"] == "call_1"
    assert nested["tool_name"] == "terminal"
    # 顶层已经有值时不被覆盖：搬运而已，不改写事实。
    kept = history_mod.normalize_message_row(
        {"tool_name": "terminal", "metadata": {"toolName": "other"}}
    )
    assert kept["tool_name"] == "terminal"


def test_history_from_http_reads_tool_calls_out_of_metadata() -> None:
    """整条响应走一遍：字段名换了位置，工具调用与结果照样配得上。"""
    payload = {
        "object": "list",
        "session_id": "api_x",
        "data": [
            {
                "id": "10",
                "role": "assistant",
                "timestamp": 1.0,
                "metadata": {
                    "toolCalls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"command": "pwd"}',
                            },
                        }
                    ]
                },
            },
            {
                "id": "11",
                "role": "tool",
                "content": '{"output": "/tmp"}',
                "timestamp": 2.0,
                "metadata": {"toolCallId": "call_9", "toolName": "terminal"},
            },
        ],
    }
    history = history_mod.history_from_http("api_x", payload)
    assert history is not None
    calls, results = history_mod.tool_calls_from_history(history.entries)
    assert [call.call_id for call in calls] == ["call_9"]
    assert calls[0].name == "terminal"
    assert calls[0].arguments == {"command": "pwd"}
    assert results["call_9"] == "/tmp"  # 批次十八第 3 件：取 output 字段
