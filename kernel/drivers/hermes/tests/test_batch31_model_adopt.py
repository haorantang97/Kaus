"""批次三十一（Hermes Driver 侧）：AD-155「模型快照过期不拦发送，改为采纳」。

真机来路（``docs/quality/verify-batch30-retest.md`` ①③）：用户把项目配置物化到
引擎，Hermes 的 ``model.default`` 从 ``gpt-5.6-sol`` 变成 ``gpt-5.6-terra``，于是
**每一条**快照还是前者的旧会话都发不出话——``send_message`` 抛
``conversation_model_unsupported``，组投递整片 ``1 failed``。

改判后守三件事：①照旧提交；②``kaus/model.adopted`` 排在 ``run.started``
**之前**；③快照一致时一条采纳事件都不发。

隔离：只用假 gateway（``fake_api_server``）与临时 ``HERMES_HOME``，一行都不碰
真实 ``~/.hermes``，也不读任何凭据文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.base import MessageInput
from drivers.hermes.testing.fake_api_server import run3_script
from drivers.hermes.testing.harness import FakeHermesHarness
from runtime.event_reducer import (
    MODEL_ADOPTED_NAME,
    MODEL_ADOPTED_NAMESPACE,
    MODEL_ADOPTED_REASON_NOT_SCOPED,
)


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


async def _ready(rig):
    driver = rig.make_driver()
    server = rig.servers["default"]
    server.capabilities = {**server.capabilities, "features": {**server.capabilities["features"], "session_model_lock": False}}
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    return driver, project, binding


def _write_engine_model(rig, model_id: str, *, profile: str = "default") -> None:
    """在沙盒 ``HERMES_HOME`` 里写一份只含 ``model`` 的 ``config.yaml``。

    真机上物化（AD-149）改的正是这一个键。**不写任何凭据键**。
    """
    home = rig._home_for(profile)  # noqa: SLF001 - 夹具内部路径
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f'model:\n  default: "{model_id}"\n', encoding="utf-8"
    )


async def _drain(driver, runtime, *, limit: int = 20):
    """把这一轮的信封收到终态为止。"""
    envelopes = []
    async for envelope in driver.events(runtime):
        envelopes.append(envelope)
        if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
            break
        if len(envelopes) >= limit:
            break
    return envelopes


def _adoptions(envelopes):
    return [
        envelope
        for envelope in envelopes
        if envelope.event.type == "extension.event"
        and envelope.event.namespace == MODEL_ADOPTED_NAMESPACE
        and envelope.event.name == MODEL_ADOPTED_NAME
    ]


async def test_a_stale_snapshot_no_longer_blocks_the_send(rig) -> None:
    """物化之后的旧会话：照旧提交 ``POST /v1/runs``，不再抛不支持。"""
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-terra")
    conversation = rig.make_conversation(project, binding).evolve(
        model_id="gpt-5.6-sol"
    )
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="复测"))
        assert any(
            method == "POST" and path.endswith("/runs")
            for method, path, _ in server.requests
        )
    finally:
        await driver.stop_runtime(runtime)


async def test_the_adoption_notice_comes_before_run_started(rig) -> None:
    """顺序即含义：先说「从这里起按 B 跑」，再开始这一轮。"""
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-terra")
    conversation = rig.make_conversation(project, binding).evolve(
        model_id="gpt-5.6-sol"
    )
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="复测"))
        envelopes = await _drain(driver, runtime)
        types = [envelope.event.type for envelope in envelopes]
        adopted = _adoptions(envelopes)
        assert len(adopted) == 1
        assert types.index("extension.event") < types.index("run.started")
        assert envelopes.index(adopted[0]) < types.index("run.started")
        data = adopted[0].event.data
        assert data["from"] == "gpt-5.6-sol"
        assert data["to"] == "gpt-5.6-terra"
        assert data["reason"] == MODEL_ADOPTED_REASON_NOT_SCOPED
        # 它不属于任何一轮：解释的正是「这一轮为什么换了模型」。
        assert adopted[0].run_id is None
    finally:
        await driver.stop_runtime(runtime)


async def test_a_matching_snapshot_emits_no_adoption(rig) -> None:
    """快照就是有效模型 → 一条采纳事件都不发（没有什么可采纳的）。"""
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-terra")
    conversation = rig.make_conversation(project, binding).evolve(
        model_id="gpt-5.6-terra"
    )
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="hi"))
        envelopes = await _drain(driver, runtime)
        assert _adoptions(envelopes) == []
    finally:
        await driver.stop_runtime(runtime)


async def test_no_snapshot_emits_no_adoption(rig) -> None:
    """会话本来就没有自己的快照：跟着引擎跑是常态，不该冒出一行通知。"""
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-terra")
    conversation = rig.make_conversation(project, binding)
    assert conversation.model_id is None
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(run3_script)
        await driver.send_message(runtime, MessageInput(text="hi"))
        envelopes = await _drain(driver, runtime)
        assert _adoptions(envelopes) == []
    finally:
        await driver.stop_runtime(runtime)


async def test_adoption_is_decided_before_the_post_but_emitted_after_it(rig) -> None:
    """网关拒了这一句时不该留下一条采纳通知——那一轮根本没开始。"""
    driver, project, binding = await _ready(rig)
    _write_engine_model(rig, "gpt-5.6-terra")
    conversation = rig.make_conversation(project, binding).evolve(
        model_id="gpt-5.6-sol"
    )
    runtime = await driver.start_runtime(conversation, "card")
    server = rig.servers["default"]
    try:
        server.fail_runs(500)
        with pytest.raises(Exception):
            await driver.send_message(runtime, MessageInput(text="复测"))
        assert driver._state(runtime).queue.empty()  # noqa: SLF001 - 队列即时间线
    finally:
        await driver.stop_runtime(runtime)
