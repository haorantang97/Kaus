"""Driver 层单测：会话账本、resume、显式不支持、权限回执、cancel。

契约套件（``drivers/contract_tests/test_acp_driver_contract.py``）已经覆盖通用
形状；本文件补的是**只有 ACP 才有**的那几件事。
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import (
    TURN_ALREADY_RUNNING,
    CreateSessionOptions,
    InteractionResponse,
    MessageInput,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from drivers.contract_tests.harness import collect_until_run_terminal
from runtime.capability_matrix import SupportLevel


@pytest.fixture
def harness() -> FakeAcpHarness:
    return FakeAcpHarness()


def make_driver(scenario: str = "text-stream") -> AcpDriver:
    workdir = tempfile.gettempdir()
    return AcpDriver(
        fake_agent_spec(scenario, cwd=workdir),
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )


# --------------------------------------------------------------------------- #
# probe / 能力
# --------------------------------------------------------------------------- #


async def test_probe_reports_agent_version_and_driver_kind() -> None:
    driver = make_driver()
    result = await driver.probe()
    assert driver.driver_kind == "acp"
    assert result.state == "ready" and result.installed
    assert result.version == "0.1.0"
    assert result.driver_version
    assert await driver.get_capabilities() == result.capabilities


async def test_probe_of_a_missing_agent_is_unavailable_not_a_crash() -> None:
    driver = AcpDriver(
        fake_agent_spec().model_copy(update={"command": ("no-such-acp-agent-xyz",)})
    )
    result = await driver.probe()
    assert result.state == "unavailable"
    assert result.installed is False
    assert result.message


async def test_session_discovery_support_is_own_process(harness: FakeAcpHarness) -> None:
    driver = make_driver()
    verdict = driver.session_discovery_support()
    assert verdict.level is SupportLevel.PARTIAL
    capabilities = await driver.get_capabilities()
    # 「可核对、不可发现」现在是能力轴上的一个取值，不再是布尔 + 一句备注。
    assert capabilities.sessions.list.value == "own_process"


# --------------------------------------------------------------------------- #
# 会话账本与 session/list 的核对
# --------------------------------------------------------------------------- #


async def test_list_is_backed_by_the_driver_ledger_not_by_session_list(
    harness: FakeAcpHarness,
) -> None:
    """实测：新进程的 ``session/list`` 看不见别的进程建的会话，但会话还在。

    因此 ``create_native_session``（短命连接，建完就关）之后 ``list_native_sessions``
    （另一条短命连接）必须**仍然**能报出这个会话——靠的是 Driver 自己的账本。
    """
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    session = await driver.create_native_session(
        binding, CreateSessionOptions(title="ledger")
    )
    listed = await driver.list_native_sessions(binding)
    assert [s.native_session_id for s in listed] == [session.native_session_id]

    report = driver.discovery_reports[binding.id]
    assert report.confirmed == (), "跨进程 session/list 看不见它，这是实测常态"
    assert report.ledger_only == (session.native_session_id,)
    assert report.method_error is None


async def test_sessions_are_scoped_per_binding(harness: FakeAcpHarness) -> None:
    driver = make_driver()
    project = harness.make_project()
    primary = harness.make_binding(project, driver)
    twin = harness.make_binding(project, driver, discriminator="twin")
    a = await driver.create_native_session(primary, CreateSessionOptions())
    b = await driver.create_native_session(twin, CreateSessionOptions())
    assert a.native_session_id != b.native_session_id
    primary_ids = {s.native_session_id for s in await driver.list_native_sessions(primary)}
    twin_ids = {s.native_session_id for s in await driver.list_native_sessions(twin)}
    assert primary_ids == {a.native_session_id}
    assert twin_ids == {b.native_session_id}


async def test_foreign_binding_is_rejected(harness: FakeAcpHarness) -> None:
    driver = make_driver()
    project = harness.make_project()
    foreign = harness.make_foreign_binding(project)
    with pytest.raises(ValueError):
        await driver.get_model_catalog(foreign)
    with pytest.raises(ValueError):
        await driver.list_native_sessions(foreign)


async def test_model_catalog_is_filled_from_the_session_result(
    harness: FakeAcpHarness,
) -> None:
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    # 批次三十三（AD-157）：还没聊过也要有目录——第一次问就现探一次。
    assert [m.model_id for m in (await driver.get_model_catalog(binding)).models] == [
        "fake:small",
        "fake:large",
    ]
    await driver.create_native_session(binding, CreateSessionOptions())
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["fake:small", "fake:large"]
    assert catalog.mode == "constrained"


# --------------------------------------------------------------------------- #
# 显式不支持
# --------------------------------------------------------------------------- #


async def test_load_native_history_is_explicitly_unsupported(
    harness: FakeAcpHarness,
) -> None:
    """R-02：不返回一份缺工具结果与权限决策的假历史。"""
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    capabilities = await driver.get_capabilities()
    assert capabilities.sessions.history.is_unsupported
    with pytest.raises(UnsupportedCapabilityError):
        await driver.load_native_history(binding, "whatever")


async def test_external_cli_is_explicitly_unsupported(harness: FakeAcpHarness) -> None:
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    with pytest.raises(UnsupportedCapabilityError):
        await driver.build_external_cli_launch(conversation)


async def test_non_permission_interaction_is_explicitly_unsupported(
    harness: FakeAcpHarness,
) -> None:
    driver = make_driver("permission")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        with pytest.raises(UnsupportedCapabilityError):
            await driver.resolve_interaction(
                runtime, "whatever", InteractionResponse(kind="question", text="hi")
            )
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# resume：session/resume 而不是 session/load
# --------------------------------------------------------------------------- #


async def test_resume_uses_session_resume_across_processes(
    harness: FakeAcpHarness,
) -> None:
    """假 agent 复刻了实测行为：``session/load`` 恒被参数校验拒掉。

    因此这条能跑通，就证明 Driver 走的是 ``session/resume``——而且是在**另一个
    进程**里 resume 上一个进程创建的会话。
    """
    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    session = await driver.create_native_session(binding, CreateSessionOptions())
    conversation = harness.make_conversation(project, binding).model_copy(
        update={"native_session_id": session.native_session_id}
    )
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert runtime.native_session_id == session.native_session_id
        assert runtime.metadata["resumed"] is True
        envelopes = await collect_until_run_terminal_after_prompt(driver, runtime)
        assert envelopes[0].event.type == "session.resumed"
    finally:
        await driver.stop_runtime(runtime)


async def collect_until_run_terminal_after_prompt(driver, runtime):
    await driver.send_message(runtime, MessageInput(text="hello"))
    return await collect_until_run_terminal(driver, runtime, timeout=30.0)


async def _one_run_id(driver: AcpDriver, conversation) -> str:
    """接上 → 跑一轮 → 断开，返回这一轮的 run id。"""
    runtime = await driver.start_runtime(conversation, "card")
    try:
        envelopes = await collect_until_run_terminal_after_prompt(driver, runtime)
    finally:
        await driver.stop_runtime(runtime)
    started = [e for e in envelopes if e.event.type == "run.started"]
    assert started, "这一轮没有 run.started"
    return started[0].event.run_id


async def test_resuming_the_same_session_after_a_restart_does_not_reuse_the_run_id(
    harness: FakeAcpHarness,
) -> None:
    """MV-01 / AD-174：后端重启后续接同一条会话，第二轮**不许**和第一轮同名。

    重启用「换一个全新的 ``AcpDriver`` 实例」来模拟：进程内的两个计数器
    （Driver 的 ``_runtime_counter``、翻译器的 ``_run_index``）都随之从头数，
    而 ``native_session_id`` 是同一个——这正是真机上「重启前后同一条
    ``nativeSessionId`` 上出现了两个 ``:r1``」的形状。

    修之前这条必红：两轮都叫 ``<session>:r1``。修之后唯一性来自 Driver 在
    attach 时现铸的 ``run_token``，两次 attach 两枚令牌，撞不上。
    """
    first_process = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, first_process)
    session = await first_process.create_native_session(binding, CreateSessionOptions())
    conversation = harness.make_conversation(project, binding).model_copy(
        update={"native_session_id": session.native_session_id}
    )

    before_restart = await _one_run_id(first_process, conversation)

    # —— 后端重启：换一个全新的 Driver 实例，会话 id 原样带过去 ——
    second_process = make_driver()
    after_restart = await _one_run_id(second_process, conversation)

    assert before_restart != after_restart, (
        f"重启前后撞名了：{before_restart}；run id 里不许出现进程内计数器（AD-174）"
    )
    # 两轮都是「这次接上之后的第 1 轮」——`:rN` 是**人读的轮次**，不是唯一性来源。
    assert before_restart.endswith(":r1") and after_restart.endswith(":r1")
    # 会话 id 那一段确实是同一个：撞名的前提成立，这条才量到了东西。
    assert before_restart.startswith(f"{session.native_session_id}:")
    assert after_restart.startswith(f"{session.native_session_id}:")


# --------------------------------------------------------------------------- #
# 权限往返与 cancel（走真的 stdio）
# --------------------------------------------------------------------------- #


async def test_permission_round_trip_over_stdio(harness: FakeAcpHarness) -> None:
    driver = make_driver("permission")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        async def resolve(envelope) -> None:
            await driver.resolve_interaction(
                runtime,
                envelope.event.request.request_id,
                InteractionResponse(kind="permission", option_id="deny"),
            )

        await driver.send_message(runtime, MessageInput(text="run something"))
        envelopes = await collect_until_run_terminal(
            driver, runtime, on_interaction=resolve, timeout=30.0
        )
    finally:
        await driver.stop_runtime(runtime)

    requested = [e for e in envelopes if e.event.type == "permission.requested"]
    resolved = [e for e in envelopes if e.event.type == "permission.resolved"]
    assert len(requested) == 1 and len(resolved) == 1
    assert resolved[0].event.decision == "deny"
    # 回执真的到了 agent 那边：它据此把工具标成失败。
    completed = [e for e in envelopes if e.event.type == "tool.completed"]
    assert completed and completed[0].event.is_error is True


async def test_resolving_an_unknown_request_raises(harness: FakeAcpHarness) -> None:
    driver = make_driver("permission")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        from drivers.base import InteractionNotFoundError

        with pytest.raises(InteractionNotFoundError):
            await driver.resolve_interaction(
                runtime, "nope", InteractionResponse(kind="permission", option_id="allow")
            )
    finally:
        await driver.stop_runtime(runtime)


async def test_cancel_is_a_notification_and_converges_to_run_interrupted(
    harness: FakeAcpHarness,
) -> None:
    """``session/cancel`` 无回执：终态由 ``session/prompt`` 的 stopReason 带回来。"""
    driver = make_driver("interrupt")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        collector = asyncio.create_task(
            collect_until_run_terminal(driver, runtime, timeout=30.0)
        )
        await driver.send_message(runtime, MessageInput(text="long task"))
        await asyncio.sleep(0.2)
        await driver.interrupt(runtime)
        envelopes = await collector
    finally:
        await driver.stop_runtime(runtime)

    interrupted = [e for e in envelopes if e.event.type == "run.interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0].event.reason == "cancelled"
    # 终态只发一次：Driver 的兜底与 agent 的 stopReason 不得重复。
    assert len([e for e in envelopes if e.event.type in {"run.completed", "run.failed"}]) == 0


async def test_second_send_while_running_is_refused(harness: FakeAcpHarness) -> None:
    driver = make_driver("interrupt")
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="one"))
        # R7（批次三十七）：这一条**必须**是专用异常而不是裸 RuntimeError——
        # 接入层按类型把它翻成 409 + 一句人话，漏成 RuntimeError 就是 500。
        with pytest.raises(TurnAlreadyRunningError) as caught:
            await driver.send_message(runtime, MessageInput(text="two"))
        assert caught.value.failure is not None
        assert caught.value.failure.code == TURN_ALREADY_RUNNING
        assert caught.value.failure.hint
    finally:
        await driver.stop_runtime(runtime)


async def test_stopped_runtime_is_unknown(harness: FakeAcpHarness) -> None:
    from drivers.base import RuntimeNotFoundError

    driver = make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    await driver.stop_runtime(runtime)
    with pytest.raises(RuntimeNotFoundError):
        await driver.send_message(runtime, MessageInput(text="hi"))
    # 幂等：重复 stop 不炸。
    await driver.stop_runtime(runtime)


async def test_attachments_become_content_blocks() -> None:
    from drivers.acp.driver import _prompt_blocks
    from drivers.base import AttachmentRef

    blocks = _prompt_blocks(
        MessageInput(
            text="look",
            attachments=(
                AttachmentRef(kind="file", ref="/tmp/a.txt", mime_type="text/plain"),
                AttachmentRef(kind="context_packet", ref="packet-1"),
            ),
        )
    )
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1]["type"] == "resource_link" and blocks[1]["uri"] == "/tmp/a.txt"
    # ACP 没有 context_packet 这种块：退化成可读文本，而不是发它不认识的块。
    assert blocks[2] == {"type": "text", "text": "[context_packet] packet-1"}


# --------------------------------------------------------------------------- #
# session/set_model（批次二十六第 5 件⑤）
# --------------------------------------------------------------------------- #


def _driver_with_set_model(supported: bool) -> AcpDriver:
    """按怪癖表造一个 Driver + 同源装扮的假 agent。

    两边同源、路径不同（一边读数据类，一边真的收发 JSON-RPC），所以「声明支持
    但其实发不出去」这种分歧会被真的测出来，而不是双方各自照着同一个布尔演戏。
    """
    from drivers.acp.presets import AcpPreset, AgentQuirks
    from drivers.acp.testing.fake_acp_agent import dress_from_quirks

    quirks = AgentQuirks(
        supports_set_model=supported,
        # AD-158：能力位说「协议里那个方法认不认」，下发面走哪条由 model_switch 说
        # 了算——两位必须同源，否则 Driver 会声明支持却一次 RPC 都发不出去。
        model_switch="set_model" if supported else "none",
    )
    preset = AcpPreset(
        id="contract-only", label="Contract Only", command=("noop",), quirks=quirks
    )
    workdir = tempfile.gettempdir()
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=workdir, dress=dress_from_quirks(quirks)),
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        preset=preset,
    )


async def test_set_conversation_model_sends_it_to_a_live_session(
    harness: FakeAcpHarness,
) -> None:
    """支持这一档时真的发出去，并回报换成了哪一个。"""
    driver = _driver_with_set_model(True)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        capabilities = await driver.get_capabilities()
        assert capabilities.models.conversation_scoped.is_supported
        assert await driver.set_conversation_model(runtime, "fake:large") == "fake:large"
    finally:
        await driver.stop_runtime(runtime)


async def test_set_conversation_model_rejection_is_not_unsupported(
    harness: FakeAcpHarness,
) -> None:
    """被拒 ≠ 不支持：路是通的，只是这个模型它不接受，且要说得出为什么。"""
    from drivers.base import ModelRejectedError

    driver = _driver_with_set_model(True)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        with pytest.raises(ModelRejectedError) as raised:
            await driver.set_conversation_model(runtime, "fake:nonexistent")
        assert raised.value.reason and "unknown model" in raised.value.reason
        # 不能是 UnsupportedCapabilityError 的子类，否则接入层会把它压成 501。
        assert not isinstance(raised.value, UnsupportedCapabilityError)
    finally:
        await driver.stop_runtime(runtime)


async def test_set_conversation_model_is_unsupported_when_the_quirk_says_so(
    harness: FakeAcpHarness,
) -> None:
    """怪癖表说不认这个方法就**一次 RPC 都不发**，能力表也如实报 unsupported。"""
    driver = _driver_with_set_model(False)
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        capabilities = await driver.get_capabilities()
        assert capabilities.models.conversation_scoped.is_unsupported
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_model(runtime, "fake:large")
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# 批次三十四：configOptions 换模型 / effort 后缀（AD-158）
# --------------------------------------------------------------------------- #


def _driver_with(quirks, *, dress_extra: dict | None = None) -> AcpDriver:
    """按一份怪癖造 Driver + 同源装扮的假 agent（``dress_extra`` 再叠一层）。"""
    from drivers.acp.presets import AcpPreset
    from drivers.acp.testing.fake_acp_agent import dress_from_quirks

    preset = AcpPreset(
        id="contract-only", label="Contract Only", command=("noop",), quirks=quirks
    )
    dress = dress_from_quirks(quirks)
    dress.update(dress_extra or {})
    workdir = tempfile.gettempdir()
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=workdir, dress=dress),
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        preset=preset,
    )


async def test_config_option_model_values_are_passed_through_verbatim(
    harness: FakeAcpHarness,
) -> None:
    """JSON 元组字符串**原样送回去**，一个字符都不改（AD-158）。

    真机上有一家把模型的取值给成 ``["provider","model"]`` 这种字符串。诱惑是把它
    解析成两段再拼回去（看起来更「懂」），但那样一来我们就得替引擎决定分隔符、
    引号与转义——三样都猜对才不出错，猜错一样就是一次没人看得懂的拒绝。目录里
    那个 ``value`` 是引擎自己给的，原样发是唯一不会错的做法。

    这条用例同时验了另一件事：模型目录**真的**从 ``configOptions`` 里读出来了，
    并且读到的就是那串原文（不是被谁顺手规整过的样子）。
    """
    from drivers.acp.presets import AgentQuirks
    from drivers.acp.testing.fake_acp_agent import TUPLE_MODEL_VALUES

    quirks = AgentQuirks(supports_set_model=False, model_switch="config_option")
    driver = _driver_with(
        quirks, dress_extra={"configOptions": True, "tupleModelValues": True}
    )
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        catalog = await driver.get_model_catalog(binding)
        assert [model.model_id for model in catalog.models] == TUPLE_MODEL_VALUES
        target = TUPLE_MODEL_VALUES[1]
        assert await driver.set_conversation_model(runtime, target) == target
    finally:
        await driver.stop_runtime(runtime)


async def test_config_option_id_comes_from_the_category_not_a_hardcoded_name(
    harness: FakeAcpHarness,
) -> None:
    """``optionId`` 按 ``category=model`` 那一项的 ``id`` 发（AD-158）。

    ``category`` 是协议侧的分类字段，``id`` 是各家自己起的名字。取证到的几家都
    把它叫 ``model``，但那是巧合——按 ``id`` 发、按 ``category`` 找，两件事分开
    才不会在下一家改名时静默失效。
    """
    from drivers.acp.presets import AgentQuirks

    quirks = AgentQuirks(supports_set_model=False, model_switch="config_option")
    driver = _driver_with(quirks, dress_extra={"configOptions": True})
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        snapshot = driver._catalog[binding.id]  # noqa: SLF001 - 断言取数来源
        assert snapshot.model_option_id == "model"
        assert await driver.set_conversation_model(runtime, "fake:large") == "fake:large"
    finally:
        await driver.stop_runtime(runtime)


async def test_effort_suffix_ids_display_without_the_suffix_but_send_with_it(
    harness: FakeAcpHarness,
) -> None:
    """后缀是**发送**的形状，不是**显示**的名字（AD-158）。

    目录里的 id 带后缀时：``model_id`` 保留原文（RPC 要它），显示名去掉后缀
    （用户要看的是模型本身）。两者混成一件事，界面上就会出现「同一个模型有六个」
    或者「换模型总是被拒」，取决于混错了哪一边。
    """
    from drivers.acp.driver import _strip_effort_suffix
    from drivers.acp.presets import AgentQuirks
    from drivers.base import ModelDescriptor

    assert _strip_effort_suffix("fake:large[low]") == "fake:large"
    assert _strip_effort_suffix("fake:large") == "fake:large"
    # 不是「结尾一对方括号」的一律不动——宁可少剥一个也不要剁短一个正常 id。
    assert _strip_effort_suffix("fake:large]") == "fake:large]"
    assert _strip_effort_suffix("[low]") == "[low]"

    quirks = AgentQuirks(
        supports_set_model=True, model_switch="set_model", model_id_format="effort_suffix"
    )
    driver = _driver_with(quirks)
    labelled = driver._label_model(  # noqa: SLF001 - 这一步就是被测对象
        ModelDescriptor(model_id="fake:large[low]")
    )
    assert labelled.model_id == "fake:large[low]"
    assert labelled.display_name == "fake:large"

    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        # 装扮里裸 id 回 -32603，所以这条能过就说明后缀真的补上了。
        assert await driver.set_conversation_model(runtime, "fake:large") == "fake:large"
    finally:
        await driver.stop_runtime(runtime)


async def test_close_before_resume_is_silent_when_the_engine_has_no_such_method(
    harness: FakeAcpHarness,
) -> None:
    """不认 ``session/close`` 的引擎照常续接，且**不留 warning**（AD-158）。

    这一步是为某一家加的，别家身上它只是一次注定 -32601 的 RPC。要是那一条也
    记进 warning，每次续接都会挂一条与用户无关的告警——那正是「告警多到没人看」
    的起点。
    """
    from drivers.acp.presets import AgentQuirks

    quirks = AgentQuirks(supports_session_resume=True, resume_requires_close=True)
    driver = _driver_with(quirks, dress_extra={"resumeRequiresClose": False})
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    session = await driver.create_native_session(binding, CreateSessionOptions())
    resumed = conversation.model_copy(
        update={"native_session_id": session.native_session_id}
    )
    runtime = await driver.start_runtime(resumed, "card")
    try:
        assert runtime.metadata["resumed"] is True
        assert not driver.warnings
    finally:
        await driver.stop_runtime(runtime)
