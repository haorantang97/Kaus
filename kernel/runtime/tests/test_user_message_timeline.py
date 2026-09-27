"""用户消息进时间线（批次八第 6 件）。

选的是哪条路，为什么
--------------------
草稿页打的第一句话，跳到会话页之后就没了——内核时间线里只有 Backend 吐出来的
东西，没有用户自己发的那条。三条候选路：

1. **新增一个事件类型**（比如 ``user.message``）：不行。AgentEventEnvelope v1.1
   的 30 条 union 已于 2026-09-02 冻结，新增事件属于 v1.2
   （`runtime/ENVELOPE_CHANGELOG.md` 变更规则 1），且 `test_envelope_frozen.py`
   会当场变红。
2. **复用 ``message.started`` / ``message.delta``，带 ``role: "user"``**：也不行。
   ``MessageStarted.role`` 的类型是 ``Literal["assistant"]``，把它放宽成
   ``assistant | user`` 是**改一个已发布字段的类型**——变更规则 2 允许的是
   「新增可选字段」，不是改已有字段。另外 ``messageId`` 属于 Backend 的命名空间，
   Session Host 自己造一个塞进去，早晚和真 Driver 的 id 撞上。
3. **``extension.event``（namespace ``kaus``、name ``user.message``）**：选这条。
   N §7.3 规则 7 与裁决表 #5 明说它是原生/实验事件的**唯一合法出口**，
   ``run.spawned`` 进 union 之前走的就是它；namespace 用产品自己的名字，
   不占任何一家 Backend 的。

Reducer 认得这一对 namespace/name，把它归成 ``role="user"`` 的 :class:`MessageItem`
（而不是通用事件卡）；事件走与 Driver 事件完全相同的入库路径，所以 ``?after=``
重放同样拿得到它。

前端配套（未做，见收尾报告）：`web/src/lib/timeline/reducer.ts` 是本 reducer 的
镜像（AD-83），它此刻仍把 ``extension.event`` 渲染成通用事件卡，且 ``MessageItem``
的 role 写死 ``assistant``。本批次不改 `web/`。
"""

from __future__ import annotations

from drivers.base import MessageInput
from runtime.event_envelope import ExtensionEvent
from runtime.event_reducer import (
    USER_MESSAGE_NAME,
    USER_MESSAGE_NAMESPACE,
    TimelineState,
    reduce_event,
)
from runtime.tests.test_session_host import (  # 复用同一套世界与剧本
    drive_until_run_terminal,
    make_world,
)
from runtime.tests.test_event_reducer import env as make_envelope
from drivers.mock.fixtures import text_stream_script


# --------------------------------------------------------------------------- #
# Reducer 侧
# --------------------------------------------------------------------------- #


def test_user_message_extension_becomes_a_user_role_message_item() -> None:
    state = TimelineState.initial("conversation:x")
    state = reduce_event(
        state,
        make_envelope(
            ExtensionEvent(
                namespace=USER_MESSAGE_NAMESPACE,
                name=USER_MESSAGE_NAME,
                data={"text": "帮我看看这段代码", "role": "user"},
            ),
            0,
        ),
    )

    items = state.items_of("message")
    assert len(items) == 1
    assert items[0].role == "user"
    assert items[0].text == "帮我看看这段代码"
    # 用户消息没有流式增量，一落地就是终态。
    assert items[0].is_terminal is True
    # 归成消息卡，**不是**通用事件卡。
    assert state.items_of("extension") == ()


def test_other_extension_events_still_become_generic_cards() -> None:
    """只认这一对 namespace/name；别的扩展事件照旧走 Generic Event Card（N §8.2）。"""
    state = TimelineState.initial("conversation:x")
    state = reduce_event(
        state,
        make_envelope(ExtensionEvent(namespace="acme", name="user.message"), 0),
    )
    state = reduce_event(
        state,
        make_envelope(
            ExtensionEvent(namespace=USER_MESSAGE_NAMESPACE, name="something.else"), 1
        ),
    )

    assert len(state.items_of("extension")) == 2
    assert state.items_of("message") == ()


def test_a_malformed_payload_does_not_crash_the_reducer() -> None:
    """N §8.2：未知但可显示的事件不得让页面崩掉——data 不是对象时按空文本处理。"""
    state = TimelineState.initial("conversation:x")
    state = reduce_event(
        state,
        make_envelope(
            ExtensionEvent(
                namespace=USER_MESSAGE_NAMESPACE, name=USER_MESSAGE_NAME, data="裸字符串"
            ),
            0,
        ),
    )

    assert [item.text for item in state.items_of("message")] == [""]


# --------------------------------------------------------------------------- #
# Session Host 侧
# --------------------------------------------------------------------------- #


async def test_send_message_puts_the_user_line_on_the_timeline() -> None:
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "第一句话")
    await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    state = host.timeline(conversation.id)
    said = [item for item in state.items_of("message") if item.role == "user"]
    assert [item.text for item in said] == ["第一句话"]
    # 顺序：用户那句在助手回复之前。
    assistant = [item for item in state.items_of("message") if item.role == "assistant"]
    assert said[0].order < assistant[0].order

    await host.aclose()


async def test_the_user_line_survives_an_after_replay() -> None:
    """``?after=`` 重放拿得到它——它是 Event Store 里一条正经事件，不是内存装饰。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "重放也要看得到")
    await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    replayed = await world.event_store.replay(conversation.id)
    rebuilt = TimelineState.initial(conversation.id)
    for envelope in replayed:
        rebuilt = reduce_event(rebuilt, envelope)

    assert [
        item.text for item in rebuilt.items_of("message") if item.role == "user"
    ] == ["重放也要看得到"]

    await host.aclose()


async def test_the_client_ref_is_carried_back_verbatim() -> None:
    """AD-94：给了 ``clientRef`` 就原样出现在事件的 ``data`` 里。

    「原样」是重点——公共层不重新编号、不加前缀、不校验形状。页面拿它把本地
    占位换成正式条目，**按编号换不按文本换**，所以任何加工都会让对账落空。
    """
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(
        conversation.id, MessageInput(text="带编号的一句", client_ref="ref-abc-123")
    )
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    first = events[0].event
    assert first.namespace == USER_MESSAGE_NAMESPACE
    assert first.data["clientRef"] == "ref-abc-123"
    assert first.data["text"] == "带编号的一句"

    await host.aclose()


async def test_without_a_client_ref_the_key_is_absent() -> None:
    """没给就**不放这个键**（不是放 ``null``）：wire 上只有一种「没有」。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "没有编号")
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    assert "clientRef" not in events[0].event.data

    await host.aclose()


async def test_the_client_ref_survives_an_after_replay() -> None:
    """``?after=`` 重放拿得到编号——页面刷新后仍能把在途占位对上号。"""
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(
        conversation.id, MessageInput(text="重放也要带编号", client_ref="ref-replay")
    )
    await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    replayed = await world.event_store.replay(conversation.id)
    refs = [
        envelope.event.data.get("clientRef")
        for envelope in replayed
        if envelope.event.type == "extension.event"
        and envelope.event.name == USER_MESSAGE_NAME
    ]
    assert refs == ["ref-replay"]

    await host.aclose()


async def test_the_user_line_is_not_attached_to_a_run() -> None:
    """它是**发起**这一轮的东西，run.started 还没发生——runId 必须是 null。

    挂到上一轮的 runId 上会让它显示在上一轮里；这也是 `POST /messages` 的
    ``runId`` 探测（N §13.1）不会把它误当成本轮首帧的原因。
    """
    world = make_world()
    _, conversation = await world.add_backend(script=text_stream_script())
    host = world.host

    await host.start_runtime(conversation)
    subscription = host.subscribe(conversation.id)
    await host.send_message(conversation.id, "喂")
    events = await drive_until_run_terminal(host, subscription, conversation.id)
    subscription.close()

    first = events[0]
    assert first.event.type == "extension.event"
    assert first.event.namespace == USER_MESSAGE_NAMESPACE
    assert first.run_id is None

    await host.aclose()
