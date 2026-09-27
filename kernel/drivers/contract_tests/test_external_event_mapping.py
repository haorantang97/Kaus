"""外部事件 → 公共事件的映射示例（AD-32 落到 Envelope v1.1 上的证明）。

要证明什么
----------
live 探针（``docs/probes/2026-09-02-run3-live.md``）在一个真实的外部 HTTP + SSE
后端上抓到了五个事件名，AD-32 据此定下了 Phase 3B 的映射方向。3B 还没开工，但
Envelope v1.1 **马上要冻结**——所以必须先回答一个问题：

    这五个外部事件，能不能一条不丢地映进 v1.1 的公共 union？

不能的话，冻结就是把一个表达力不够的 schema 钉死。这个文件用
``drivers/mock/`` 里的 ``external-http-shaped`` 剧本回答它：
外部报文 → :class:`~drivers.mock.external_shapes.ExternalHttpStreamTranslator`
→ 公共事件 → MockDriver 播出 → Reducer 归并 → 断言用户可见内容逐字还原。

无损的四层含义（本文件逐条断言）
--------------------------------
1. **事件粒度**：每条外部事件产出恰好一条公共事件，不合并、不丢弃、不发明；
2. **类型归属**：产出的类型落在 AD-32 定的映射表里，且都在公共 union 内；
3. **内容**：正文、思考文本、工具名、错误标志、run id 逐字保全；
4. **兜底**：外部报文里没有公共归宿的原生字段是**已知且列举过的**
   （:data:`UNMAPPED_EXTERNAL_FIELDS`），不是被悄悄吃掉的。

命名
----
文件名与剧本名都不带任何 Backend 的名字（``external-http-shaped``），
这样公共层纯净性扫描（``kernel/tests/test_public_type_purity.py``）与人肉审查
都不会在 kernel 里读到某一家的私有名词——N §3。
"""

from __future__ import annotations

import pytest

from drivers.base import MessageInput
from drivers.contract_tests.harness import (
    ContractScenario,
    collect_until_run_terminal,
    events_of_type,
)
from drivers.mock.external_shapes import (
    EXTERNAL_EVENT_MAP,
    EXTERNAL_EVENT_NAMES,
    EXTERNAL_HTTP_SAMPLE_STREAM,
    UNMAPPED_EXTERNAL_FIELDS,
    ExternalHttpStreamTranslator,
    synthetic_message_id,
)
from drivers.mock.harness import MockDriverContractHarness
from runtime.event_envelope import AGENT_EVENT_TYPES
from runtime.event_reducer import MessageItem, TimelineState, ToolItem, reduce_events

#: 报文里**不需要**映射的结构性字段：``event`` 是事件名本身，``run_id`` 进信封头。
_STRUCTURAL_FIELDS = frozenset({"event", "run_id"})


def _translate_sample() -> tuple:
    return ExternalHttpStreamTranslator().translate_stream(EXTERNAL_HTTP_SAMPLE_STREAM)


# --------------------------------------------------------------------------- #
# 1. 映射表本身
# --------------------------------------------------------------------------- #


def test_mapping_table_covers_every_probed_external_event() -> None:
    """探针实测到的五个事件名，映射表一个不缺。"""
    assert set(EXTERNAL_EVENT_MAP) == set(EXTERNAL_EVENT_NAMES)
    assert len(EXTERNAL_EVENT_NAMES) == 5


def test_mapping_table_targets_are_all_public_events() -> None:
    """映射的落点必须全在公共 union 内——不允许「翻到一个还没有的事件上」。"""
    for external_name, targets in EXTERNAL_EVENT_MAP.items():
        assert targets, external_name
        for target in targets:
            assert target in AGENT_EVENT_TYPES, (
                f"{external_name!r} 映到了不存在的公共事件 {target!r}"
            )


def test_mapping_depends_on_the_two_ad27_additions() -> None:
    """这条映射恰好用上了 AD-27 补的两项之一，说明补全不是可有可无的。

    ``reasoning.available`` 带增量文本时落到 ``reasoning.delta``（AD-27 新增）。
    没有它，思考原文只能塞进 ``reasoning.status.summary`` 被后一条覆盖。
    """
    assert "reasoning.delta" in EXTERNAL_EVENT_MAP["reasoning.available"]
    assert "reasoning.status" in EXTERNAL_EVENT_MAP["reasoning.available"]


# --------------------------------------------------------------------------- #
# 2. 翻译器：逐条、逐字段
# --------------------------------------------------------------------------- #


def test_each_external_event_yields_exactly_one_public_event() -> None:
    translated = _translate_sample()
    assert len(translated) == len(EXTERNAL_HTTP_SAMPLE_STREAM)
    for payload, event in zip(EXTERNAL_HTTP_SAMPLE_STREAM, translated, strict=True):
        allowed = EXTERNAL_EVENT_MAP[payload["event"]]
        assert event.type in allowed, (
            f"{payload['event']!r} 翻成了 {event.type!r}，不在映射表 {allowed} 内"
        )


def test_every_payload_field_is_either_mapped_or_explicitly_listed() -> None:
    """报文里的每个字段都必须有交代：要么映进公共事件，要么在「无公共归宿」名单里。"""
    unexplained: list[str] = []
    for payload in EXTERNAL_HTTP_SAMPLE_STREAM:
        for field in payload:
            if field in _STRUCTURAL_FIELDS or field in UNMAPPED_EXTERNAL_FIELDS:
                continue
            if field not in {"delta", "text", "tool", "preview", "error"}:
                unexplained.append(f"{payload['event']}.{field}")
    assert not unexplained, (
        "这些原生字段既没映射也没被列举，等于被悄悄吃掉了：" + ", ".join(unexplained)
    )
    # 反向：名单不得虚设——它列的字段确实出现在样本流里。
    seen = {field for payload in EXTERNAL_HTTP_SAMPLE_STREAM for field in payload}
    assert UNMAPPED_EXTERNAL_FIELDS <= seen


def test_synthesised_ids_are_stable_and_shared() -> None:
    """外部流不带 messageId/callId，合成的 ID 必须确定且把同一对象串起来。"""
    translated = _translate_sample()
    run_id = EXTERNAL_HTTP_SAMPLE_STREAM[0]["run_id"]
    message_ids = {
        e.message_id for e in translated if e.type in ("message.delta", "reasoning.delta")
    }
    assert message_ids == {synthetic_message_id(run_id)}, (
        "所有正文片段与思考片段必须挂在同一条合成消息上（N §7.3 规则 1/3）"
    )
    call_ids = {e.call_id for e in translated if e.type.startswith("tool.")}
    assert len(call_ids) == 1, "同一次工具调用的 started/completed 必须共用一个 callId"
    # 确定性：同一条流翻两次，结果逐字相同（重连重放不得换 ID）。
    assert _translate_sample() == translated


def test_unknown_external_event_is_refused_not_guessed() -> None:
    """未知外部事件不得猜一个公共事件出来——那是 extension.event 的活儿（规则 7）。"""
    with pytest.raises(KeyError):
        ExternalHttpStreamTranslator().translate(
            {"event": "context.compacted", "run_id": "run_x"}
        )


# --------------------------------------------------------------------------- #
# 3. 端到端：MockDriver 播剧本 → Reducer 归并
# --------------------------------------------------------------------------- #


async def test_external_shaped_script_replays_losslessly_through_the_driver() -> None:
    """整条外部流经 MockDriver 与 Reducer 之后，用户可见内容逐字还原。"""
    harness = MockDriverContractHarness()
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)

    await harness.arrange(driver, conversation, ContractScenario.EXTERNAL_HTTP_SHAPED)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="build it"))
        envelopes = await collect_until_run_terminal(driver, runtime)
    finally:
        await driver.stop_runtime(runtime)

    # 事件粒度：外部流的每条事件都在，加上 Driver 合成的那条 run.started。
    assert len(envelopes) == len(EXTERNAL_HTTP_SAMPLE_STREAM) + 1
    assert envelopes[0].event.type == "run.started"
    for envelope in envelopes:
        assert envelope.event.type in AGENT_EVENT_TYPES

    expected_text = "".join(
        p["delta"] for p in EXTERNAL_HTTP_SAMPLE_STREAM if p["event"] == "message.delta"
    )
    expected_reasoning = "".join(
        p["text"]
        for p in EXTERNAL_HTTP_SAMPLE_STREAM
        if p["event"] == "reasoning.available"
    )
    external_tool = next(
        p for p in EXTERNAL_HTTP_SAMPLE_STREAM if p["event"] == "tool.started"
    )

    state = reduce_events(TimelineState.initial(conversation.id), envelopes)
    messages = [i for i in state.items if isinstance(i, MessageItem)]
    tools = [i for i in state.items if isinstance(i, ToolItem)]
    assert len(messages) == 1 and len(tools) == 1

    assert messages[0].text == expected_text
    assert messages[0].reasoning_text == expected_reasoning
    assert tools[0].name == external_tool["tool"]
    assert tools[0].input == {"preview": external_tool["preview"]}
    assert tools[0].status == "completed" and tools[0].is_terminal
    assert state.run_state == "completed"
    assert state.active_run_id == EXTERNAL_HTTP_SAMPLE_STREAM[0]["run_id"]
    # 没有一条事件需要靠 extension.event 兜底——这才叫「无损映射」。
    assert not events_of_type(envelopes, "extension.event")


async def test_external_shaped_replay_is_idempotent() -> None:
    """规则 9：这条流重放两次，正文与思考都不得翻倍。"""
    harness = MockDriverContractHarness()
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)

    await harness.arrange(driver, conversation, ContractScenario.EXTERNAL_HTTP_SHAPED)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="build it"))
        envelopes = await collect_until_run_terminal(driver, runtime)
    finally:
        await driver.stop_runtime(runtime)

    once = reduce_events(TimelineState.initial(conversation.id), envelopes)
    twice = reduce_events(once, envelopes)
    assert twice.items == once.items
    assert twice.dropped_duplicates == len(envelopes)
