"""MockDriver 的 ``eventId`` 跨进程唯一性（N §7.1）。

背景（批次八第 4 件）
--------------------
``eventId`` 原本是 ``evt-<runtime_id>-<seq>``，而 ``runtime_id`` 里的计数器是
**进程内**的：同一个领域库连着用两次（先跑一遍冒烟脚本、退出、再跑一遍），
第二个进程的第一条会话会重新拿到 ``…-runtime-1``，于是它的 ``eventId`` 与库里
上一次运行留下的逐字相同 → Session Host 判为重复投递 → 事件全被丢掉 →
整轮收敛成 ``run.failed``。

修法是给每个 Driver 实例一次性的 uuid4 种子，进 event id（不进 ``runtime_id``
——那是给人看的身份）。本文件把「两个进程」化约为「两个 Driver 实例」：这正是
进程重启后的状态，且不必真的 fork。
"""

from __future__ import annotations

import re

from app.conversations.models import Conversation
from drivers.mock.driver import MockDriver
from drivers.mock.fixtures import text_stream_script

CONVERSATION = Conversation.create(
    project_id="project:workbench",
    agent_binding_id="binding:workbench:mock",
    title="事件 id",
)


async def _first_round_event_ids(driver: MockDriver) -> list[str]:
    driver.set_script(CONVERSATION.id, text_stream_script())
    handle = await driver.start_runtime(CONVERSATION, "card")
    stream = driver.events(handle)
    await driver.send_message(handle, "你好")
    collected: list[str] = []
    async for envelope in stream:
        collected.append(envelope.event_id)
        if envelope.event.type in ("run.completed", "run.failed", "run.interrupted"):
            break
    await driver.stop_runtime(handle)
    return collected


async def test_event_ids_differ_across_driver_instances() -> None:
    """「重启进程后接着用同一个库」不得撞 id——两个实例的 id 集合必须不相交。"""
    first = await _first_round_event_ids(MockDriver())
    second = await _first_round_event_ids(MockDriver())

    assert first, "剧本至少要产出一条事件，否则这个用例什么都没验"
    assert set(first).isdisjoint(set(second))


async def test_event_ids_are_unique_within_one_instance() -> None:
    """同一实例内也仍然唯一：种子相同，靠 runtime 计数器 + sequence 区分。"""
    driver = MockDriver()
    first = await _first_round_event_ids(driver)
    second = await _first_round_event_ids(driver)

    assert set(first).isdisjoint(set(second))
    assert len(set(first)) == len(first)


async def test_event_id_shape_keeps_runtime_and_sequence_readable() -> None:
    """形态仍然是可读的 ``evt-<种子>-<runtime>-<seq>``，种子只是多出来的一段。"""
    driver = MockDriver()
    ids = await _first_round_event_ids(driver)

    pattern = re.compile(r"^evt-[0-9a-f]{8}-mock-runtime-\d+-\d+$")
    assert all(pattern.match(event_id) for event_id in ids), ids
