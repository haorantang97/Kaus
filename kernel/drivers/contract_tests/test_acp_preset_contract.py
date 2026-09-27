"""八个 ACP 预设，每个各跑一遍同一份契约（批次二十五第 5 件）。

与 :mod:`drivers.contract_tests.test_acp_driver_contract` 的分工
---------------------------------------------------------------
那一份跑的是**完整**的通用 Driver 契约，被测对端是「协议默认装扮」的假 agent，
问的是「这个 Driver 说不说得清 ACP」。

这一份问的是另一个问题：**预设目录里那八行，每一行是不是真的能用**。所以它
参数化跑八遍，每遍换一份装扮：Driver 拿到预设（按怪癖表分支），假 agent 拿到
由**同一份**怪癖翻出来的装扮（真的表现成那样）。两边同源但路径不同——一边读
数据类，一边真的收发 JSON-RPC——所以对不上时是真的对不上。

覆盖的七件（任务书第 5 件点名的那串）
------------------------------------
``new`` / ``prompt`` / ``cancel`` / ``resume`` / ``permission`` / ``set_mode`` /
``fs 越界拒绝``，外加每个预设**只属于它自己**的那条怪癖断言
（:func:`test_this_preset_has_its_own_quirk_signature`）。

一条纪律：**不 spawn 任何真实引擎。** 预设里的 ``npx …`` 命令一次都不会被执行
——被拉起来的自始至终是那个假 agent，预设只贡献 id、怪癖与续接模板。这条由
:func:`test_no_real_engine_command_is_ever_spawned` 机械守住。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from drivers.acp.capabilities import DEFAULT_THOUGHT_LEVEL
from drivers.acp.presets import PRESETS, preset_ids
from drivers.acp.testing.harness import (
    DRESS_MODES,
    DRESS_THOUGHT_LEVELS,
    FakeAcpHarness,
)
from drivers.base import (
    CreateSessionOptions,
    InteractionResponse,
    MessageInput,
    ModelRejectedError,
    UnsupportedCapabilityError,
)
from drivers.contract_tests.harness import (
    ContractScenario,
    collect_in_background,
    collect_until_run_terminal,
    events_of_type,
)
from runtime.event_reducer import TimelineState, ToolItem, reduce_events

PRESET_IDS = preset_ids()


@pytest.fixture(params=PRESET_IDS)
def preset(request: pytest.FixtureRequest):
    return PRESETS[request.param]


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    (tmp_path / "notes").mkdir()
    return str(tmp_path)


@pytest.fixture
def harness(preset, workspace: str) -> FakeAcpHarness:
    return FakeAcpHarness(preset=preset, workspace_root=workspace)


@pytest.fixture
def wired(harness: FakeAcpHarness):
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    return driver, binding, conversation


# --------------------------------------------------------------------------- #
# 目录本身
# --------------------------------------------------------------------------- #


def test_the_catalog_has_explicitly_reviewed_presets() -> None:
    """新增条目必须进入这份明确清单，并接受下方参数化协议契约。"""
    assert set(PRESET_IDS) == {
        "claude-code", "codex", "opencode", "gemini", "antigravity", "qwen",
        "openclaw", "pi", "hermes-acp", "dsh", "deepseek-acp", "kilo",
        "goose", "cursor", "copilot", "devin", "omp", "grok", "crush", "commandcode", "alma", "zcode",
    }


def test_no_real_engine_command_is_ever_spawned(harness: FakeAcpHarness) -> None:
    """夹具拉起来的必须是假 agent，预设里的真命令一次都不执行。"""
    command = harness.make_driver().agent_spec.command
    assert command[0].endswith("python") or "python" in command[0]
    assert "fake_acp_agent.py" in command[1]


# --------------------------------------------------------------------------- #
# new / prompt / cancel / resume
# --------------------------------------------------------------------------- #


async def test_session_new_then_prompt(harness: FakeAcpHarness, wired) -> None:
    driver, _binding, conversation = wired
    await harness.arrange(driver, conversation, ContractScenario.TEXT_STREAM)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="hi"))
        events = await collect_until_run_terminal(driver, runtime, timeout=20.0)
    finally:
        await driver.stop_runtime(runtime)
    assert events_of_type(events, "run.started")
    assert events_of_type(events, "run.completed")
    text = "".join(e.event.text for e in events_of_type(events, "message.delta"))
    assert text == "Hello, world."


async def test_cancel_converges_to_a_terminal_event(
    harness: FakeAcpHarness, wired
) -> None:
    driver, _binding, conversation = wired
    await harness.arrange(driver, conversation, ContractScenario.INTERRUPT)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="go"))
        collector = await collect_in_background(driver, runtime, timeout=20.0)
        await driver.interrupt(runtime)
        events = await collector
    finally:
        await driver.stop_runtime(runtime)
    assert events_of_type(events, "run.interrupted")


async def test_resume_uses_the_session_id_we_already_have(
    harness: FakeAcpHarness, preset, wired
) -> None:
    """续接：会话 id 由我们持有，不靠「最新会话」去猜。

    预设声明不支持续接时**必须**如实抛 ``UnsupportedCapabilityError``——那正是
    「不用空对象伪装支持」（N §13.1）在这条路上的样子。
    """
    driver, binding, conversation = wired
    session = await driver.create_native_session(binding, CreateSessionOptions())
    resumed = conversation.model_copy(
        update={"native_session_id": session.native_session_id}
    )
    quirks = preset.quirks
    if not quirks.supports_session_resume and not quirks.supports_session_load:
        with pytest.raises(UnsupportedCapabilityError):
            await driver.start_runtime(resumed, "card")
        return
    runtime = await driver.start_runtime(resumed, "card")
    try:
        assert runtime.native_session_id == session.native_session_id
        assert runtime.metadata["resumed"] is True
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# permission
# --------------------------------------------------------------------------- #


async def test_permission_round_trip(harness: FakeAcpHarness, wired) -> None:
    driver, _binding, conversation = wired
    await harness.arrange(driver, conversation, ContractScenario.PERMISSION)
    runtime = await driver.start_runtime(conversation, "card")

    async def _resolve(envelope) -> None:
        await driver.resolve_interaction(
            runtime,
            envelope.event.request.request_id,
            InteractionResponse(kind="permission", option_id="allow"),
        )

    try:
        await driver.send_message(runtime, MessageInput(text="run it"))
        events = await collect_until_run_terminal(
            driver, runtime, on_interaction=_resolve, timeout=20.0
        )
    finally:
        await driver.stop_runtime(runtime)
    assert events_of_type(events, "permission.requested")
    assert events_of_type(events, "permission.resolved")
    assert events_of_type(events, "run.completed")


# --------------------------------------------------------------------------- #
# set_mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("approval_mode", ["ask", "auto"])
async def test_set_mode_follows_the_quirk_table_not_the_agent_name(
    preset, workspace: str, approval_mode: str
) -> None:
    """支持这一档的预设切得到；不支持的**一次都不发**，两者都不算失败。

    AD-158 之后这条用例分两路走，因为 ``session/set_mode`` 在真机上有两种语义：
    审批档的引擎按 Binding 的 ``approval_mode`` 切，思考档的引擎按推理强度切、
    **一个字都不碰审批**。用同一个断言盖住两者，正好会漏掉「映射表挑错了一张」
    这种在界面上完全看不见的错。
    """
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, approval_mode=approval_mode
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        semantics = preset.quirks.mode_semantics
        if not preset.quirks.supports_set_mode or semantics == "none":
            assert runtime.metadata["acpModeId"] is None
            assert not driver.warnings, "不支持 set_mode 的预设不该产生 warning"
        elif semantics == "thought_level":
            # 审批档是 auto，但这台引擎的 mode 是思考档——它必须原封不动，
            # 落在 Binding 没写 reasoning_effort 时的缺省档上。
            assert runtime.metadata["acpModeId"] == DEFAULT_THOUGHT_LEVEL
            assert DEFAULT_THOUGHT_LEVEL in DRESS_THOUGHT_LEVELS
            assert runtime.metadata["acpModeId"] not in DRESS_MODES
            assert not driver.warnings
        else:
            # 显式映射的缺项必须保持不支持，不能猜一个同名的原生 auto。
            expected = (preset.approval_mode_ids.get(approval_mode)
                        if preset.approval_mode_ids else
                        {"ask": "default", "auto": "bypassPermissions"}[approval_mode])
            assert runtime.metadata["acpModeId"] == expected
            if expected is None:
                assert any(w.startswith("set_mode_no_match") for w in driver.warnings)
            else:
                assert not driver.warnings
    finally:
        await driver.stop_runtime(runtime)


async def test_thought_level_presets_follow_the_binding_reasoning_effort(
    preset, workspace: str
) -> None:
    """思考档语义：切的是 Binding 的推理强度，不是审批档（AD-158）。

    Binding 同时写了 ``approval_mode=deny`` 与 ``reasoning_effort=low``：审批档
    在装扮的档位表里**一个都匹配不上**，所以只要 Driver 拿错了那张映射表，这里
    就会得到 ``None`` 加一条 warning，而不是 ``low``。
    """
    if preset.quirks.mode_semantics != "thought_level":
        pytest.skip("该预设的 mode 是审批档（或没有 mode），另有用例覆盖")
    harness = FakeAcpHarness(
        preset=preset,
        workspace_root=workspace,
        approval_mode="deny",
        reasoning_effort="low",
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert runtime.metadata["acpModeId"] == "low"
        assert not driver.warnings
    finally:
        await driver.stop_runtime(runtime)


async def test_thought_level_presets_warn_when_the_effort_has_no_match(
    preset, workspace: str
) -> None:
    """思考档挑不出对应项 → 不发、记 warning（与审批档同一条纪律，AD-158）。

    ``xhigh`` 故意不在装扮的档位表里，所以这条走的正是「没有对应项」那条分支。
    """
    if preset.quirks.mode_semantics != "thought_level":
        pytest.skip("该预设的 mode 是审批档（或没有 mode），另有用例覆盖")
    assert "xhigh" not in DRESS_THOUGHT_LEVELS
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, reasoning_effort="xhigh"
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert runtime.metadata["acpModeId"] is None
        assert any(w.startswith("set_mode_no_match") for w in driver.warnings)
    finally:
        await driver.stop_runtime(runtime)


async def test_set_mode_warns_instead_of_sending_an_unknown_mode_id(
    preset, workspace: str
) -> None:
    """审批档在 ``availableModes`` 里没有对应项 → 不发、记 warning（第 3 件 ①）。

    ``deny`` 那一档故意不在装扮的 mode 列表里，所以支持 set_mode 的预设走的正是
    这条分支：发一个 agent 不认识的 modeId 换来的是一次 -32602 加一个谁也不知道
    现在处于哪一档的会话。
    """
    if preset.quirks.mode_semantics == "thought_level":
        pytest.skip("思考档语义不读审批档，另有两条用例覆盖（AD-158）")
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, approval_mode="deny"
    )
    assert "deny" not in DRESS_MODES
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert runtime.metadata["acpModeId"] is None
        if preset.quirks.supports_set_mode and preset.quirks.mode_semantics == "approval":
            assert any(w.startswith("set_mode_no_match") for w in driver.warnings)
        else:
            assert not driver.warnings
    finally:
        await driver.stop_runtime(runtime)


# --------------------------------------------------------------------------- #
# fs 边界（AD-152）
# --------------------------------------------------------------------------- #


async def test_client_fs_is_declared_only_when_the_quirk_and_a_root_agree(
    preset, workspace: str
) -> None:
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert runtime.metadata["clientFs"] is preset.quirks.needs_client_fs
    finally:
        await driver.stop_runtime(runtime)


async def test_out_of_bounds_read_is_refused_and_in_bounds_write_lands(
    preset, workspace: str
) -> None:
    """圈内写落盘、圈外读被拒——两条都要，只验一条等于没验边界。

    ``auto`` 档跑这条：审批本身另有用例，这里要看的是**边界**。
    """
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, approval_mode="auto"
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    harness.arrange_script(driver, "client-fs")
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="touch the disk"))
        events = await collect_until_run_terminal(driver, runtime, timeout=20.0)
    finally:
        await driver.stop_runtime(runtime)
    text = "".join(e.event.text for e in events_of_type(events, "message.delta"))
    written = Path(workspace) / "notes" / "scratch.txt"
    if not preset.quirks.needs_client_fs:
        # 没声明 fs 能力的预设：agent 压根不会发这类请求，磁盘上什么都不会多。
        assert text == "fs: not declared"
        assert not written.exists()
        return
    assert "in-bounds write ok" in text
    assert written.read_text(encoding="utf-8") == "written by the agent\n"
    assert "out-of-bounds read rejected" in text
    assert "LEAKED" not in text


async def test_fs_write_goes_through_approval_when_the_mode_says_ask(
    preset, workspace: str
) -> None:
    """``ask`` 档：写之前先发一张审批卡，用户点了才落盘（AD-152）。"""
    if not preset.quirks.needs_client_fs:
        pytest.skip("该预设不声明客户端 fs，没有可审批的写")
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, approval_mode="ask"
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    harness.arrange_script(driver, "client-fs")
    runtime = await driver.start_runtime(conversation, "card")
    seen: list[str] = []

    async def _resolve(envelope) -> None:
        seen.append(envelope.event.request.title)
        await driver.resolve_interaction(
            runtime,
            envelope.event.request.request_id,
            InteractionResponse(kind="permission", option_id="allow"),
        )

    try:
        await driver.send_message(runtime, MessageInput(text="write it"))
        events = await collect_until_run_terminal(
            driver, runtime, on_interaction=_resolve, timeout=20.0
        )
    finally:
        await driver.stop_runtime(runtime)
    assert seen and "scratch.txt" in seen[0]
    assert events_of_type(events, "permission.resolved")
    assert (Path(workspace) / "notes" / "scratch.txt").exists()


# --------------------------------------------------------------------------- #
# 每个预设自己的那一条
# --------------------------------------------------------------------------- #


async def test_this_preset_has_its_own_quirk_signature(preset, workspace: str) -> None:
    """每个预设至少有一条**只属于它**的断言：声明与实际行为必须一致。

    这条用例的价值不在某一次断言，而在于它把「目录里写的」与「Driver 实际做的」
    绑在了一起：改了目录里的一位、忘了改 Driver（或反过来），这里就会红。
    """
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    capabilities = await driver.get_capabilities()

    # ① 续接模板 ⇔ launch.external：这一位是「在 CLI 里打开」入口的开关。
    expected = "supported" if preset.resume_argv_template else "unsupported"
    assert capabilities.external_cli.supported.value == expected
    if preset.resume_argv_template:
        argv = preset.resume_argv("sess-42")
        assert argv is not None and "sess-42" in argv
        assert "{native_session_id}" not in " ".join(argv)

    # ② 会话级换模型 ⇔ model_switch（AD-158：两条下发面有一条通就算）。
    assert capabilities.models.conversation_scoped.value == (
        "unsupported" if preset.quirks.model_switch == "none" else "supported"
    )

    # ③ 思考流 ⇔ thought_chunks。
    assert capabilities.card.reasoning.value == (
        "supported" if preset.quirks.thought_chunks else "unsupported"
    )

    # ④ 登录模型：八个预设一律 own-auth，状态报不出来（AD-93 / AD-145）。
    assert capabilities.auth.model == "own-auth"
    assert capabilities.auth.state_reporting.value == "unknown"

    # ⑤ env_keys_hint 只有**名字**，一个值都没有（AD-10 / AD-48）。
    for name in preset.env_keys_hint:
        assert "=" not in name and name.isupper()


async def test_tool_output_merge_follows_the_cumulative_quirk(
    preset, workspace: str
) -> None:
    """AD-49：全量装扮替换、增量装扮拼接，最终看到的输出必须一样。

    ``tool_update_cumulative=False`` 的那一档由这里显式造出来（目录里八行目前
    都是 ``True``，因为实测到的都是全量）——分支存在就必须被走到，否则它是死代码。
    """
    from drivers.acp.presets import with_quirks

    outputs: dict[bool, str] = {}
    for cumulative in (True, False):
        variant = with_quirks(preset, tool_update_cumulative=cumulative)
        harness = FakeAcpHarness(preset=variant, workspace_root=workspace)
        driver = harness.make_driver()
        project = harness.make_project()
        binding = harness.make_binding(project, driver)
        conversation = harness.make_conversation(project, binding)
        await harness.arrange(driver, conversation, ContractScenario.TOOL_LIFECYCLE)
        runtime = await driver.start_runtime(conversation, "card")
        try:
            await driver.send_message(runtime, MessageInput(text="read"))
            events = await collect_until_run_terminal(driver, runtime, timeout=20.0)
        finally:
            await driver.stop_runtime(runtime)
        updates = events_of_type(events, "tool.updated")
        assert updates, "工具更新一条都没有，这条用例就白跑了"
        assert all(e.event.cumulative is cumulative for e in updates)
        timeline = reduce_events(TimelineState.initial(conversation.id), events)
        tools = [item for item in timeline.items if isinstance(item, ToolItem)]
        assert len(tools) == 1
        outputs[cumulative] = str(tools[0].output)
    assert outputs[True] == outputs[False], (
        "全量与增量两种装扮下，工具卡的最终输出必须一致——"
        f"拿到 {outputs!r}；不一致说明 cumulative 那一位没被真的照做"
    )


async def test_running_the_bench_upgrades_declared_to_bench(
    preset, workspace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第 4 件的收尾：契约跑一遍，摸得着的那几题从 ``declared`` 升到 ``bench``。

    两条都要断言，缺一条这个机制就是摆设：
    ① 声明与实测**对得上**（有漂移就是目录里那一行写错了）；
    ② 对得上的那几题取证等级真的被升上去了，而 ``live``（真机实测）不会被
       假 agent 冲掉——只升不降。
    """
    from drivers.contract_tests.drift import (
        apply_bench_verification,
        compare_declared_with_observed,
        observe_capabilities,
    )
    from runtime.capability_matrix import capability_state_at

    # This bench checks launch construction, not an installed native product.
    # Keep PATH independent of the developer's installed agents. These stubs
    # fail if invoked; all actual protocol traffic must use fake_acp_agent.py.
    native_bin = tmp_path / "native-bin"
    native_bin.mkdir()
    for executable in ("claude", "codex", "opencode"):
        stub = native_bin / executable
        stub.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(native_bin) + os.pathsep + os.defpath)

    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    # 「站外 CLI 拼不拼得出命令」这题要有会话 id 才问得成立——拿一条没有 id 的
    # 会话去问，量到的是「这条会话还没跑过」，不是「这个 Backend 没有 CLI 面」。
    session = await driver.create_native_session(binding, CreateSessionOptions())
    conversation = conversation.model_copy(
        update={"native_session_id": session.native_session_id}
    )

    declared = await driver.get_capabilities()
    observations = await observe_capabilities(driver, binding, conversation)
    report = compare_declared_with_observed(driver.backend_id, declared, observations)
    assert report.in_sync, report.describe()
    assert report.confirmed, "一题都没实测到，这条用例就白跑了"

    upgraded = apply_bench_verification(declared, observations)
    for path in report.confirmed:
        before = capability_state_at(declared, path)
        after = capability_state_at(upgraded, path)
        assert after.verification == ("live" if before.verification == "live" else "bench")


# --------------------------------------------------------------------------- #
# 批次三十四：中继真机跑出来的四条形状（AD-158）
# --------------------------------------------------------------------------- #


#: 目录里**实测发增量**的那几行（AD-158 一行、AD-160 又添一行）。用推导而不是
#: 写死 id：下一次真机再逮到一家发增量的，它自动进这条用例，不用有人记得回来改。
INCREMENTAL_PRESET_IDS: tuple[str, ...] = tuple(
    sorted(
        preset_id
        for preset_id, row in PRESETS.items()
        if not row.quirks.tool_update_cumulative
    )
)


@pytest.mark.parametrize("incremental_id", INCREMENTAL_PRESET_IDS)
async def test_the_incremental_engines_really_append_without_duplicating_text(
    incremental_id: str, workspace: str
) -> None:
    """AD-49 那条默认值的反例：目录里发增量的**每一行**都在这里跑一遍。

    此前 ``tool_update_cumulative=False`` 这条分支只被
    :func:`test_tool_output_merge_follows_the_cumulative_quirk` 里**人工造出来**的
    变体走过；批次三十四逮到第一行真的发增量的，批次三十六又添一行（AD-160）。
    这条用例因此按**目录本身**参数化，而不是点名某一行——增量这条路上多一家，
    它就自动多跑一遍，不依赖谁记得回来补：

    ① 信封上 ``cumulative=False``（下游 reducer 靠它选合并语义）；
    ② 两帧更新是**接着写**的，不是各自一份全量——把它们按增量合起来正好是完整
       的那句话，而任何一帧都不包含前一帧的内容（重复文本正是搞错这一位的症状）。
    """
    preset = PRESETS[incremental_id]
    assert preset.quirks.tool_update_cumulative is False, "这条用例挑的就是这样的行"

    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    await harness.arrange(driver, conversation, ContractScenario.TOOL_LIFECYCLE)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="read"))
        events = await collect_until_run_terminal(driver, runtime, timeout=20.0)
    finally:
        await driver.stop_runtime(runtime)

    updates = events_of_type(events, "tool.updated")
    assert len(updates) >= 2, "增量至少要有两帧，否则「追加」这件事没被测到"
    assert all(e.event.cumulative is False for e in updates)
    fragments = [str(e.event.output) for e in updates]
    assert "".join(fragments) == "read a file"
    # 增量帧不许复述前一帧：那正是把增量当全量发时看到的样子。
    assert fragments[1] not in fragments[0]
    assert fragments[0] not in fragments[1]

    # 落到时间线上就是一句完整的话，没有 "readread a file" 这种重复。
    upto_completion = []
    for envelope in events:
        if envelope.event.type == "tool.completed":
            break
        upto_completion.append(envelope)
    timeline = reduce_events(TimelineState.initial(conversation.id), upto_completion)
    tools = [item for item in timeline.items if isinstance(item, ToolItem)]
    assert len(tools) == 1
    assert tools[0].output == "read a file"


def test_the_incremental_path_covers_more_than_one_preset_row() -> None:
    """参数化空转是这类用例最安静的失效方式（AD-160）。

    上面那条按目录推导出参数：推空了它就一次都不跑，而 pytest 不会为此报错。
    这里把「至少两行走这条路」钉住——批次三十四一行、批次三十六一行，任何一行
    被顺手改回全量都会先红在这里，而不是等到工具卡上出现重复文本。
    """
    assert len(INCREMENTAL_PRESET_IDS) >= 2, INCREMENTAL_PRESET_IDS
    for preset_id in INCREMENTAL_PRESET_IDS:
        assert PRESETS[preset_id].quirks.tool_update_cumulative is False


async def test_config_option_presets_switch_models_without_set_model(
    preset, workspace: str
) -> None:
    """``model_switch="config_option"``：走 ``session/set_config_option``（AD-158）。

    装扮里 ``session/set_model`` 一律回 ``-32601``（这些引擎真机上就是这样），
    所以这条用例能成立，只可能是因为 Driver 真的走了另一条路。
    """
    if preset.quirks.model_switch != "config_option":
        pytest.skip("该预设不走 configOptions 换模型")
    assert preset.quirks.supports_set_model is False, "两条路不该同时声明"
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        capabilities = await driver.get_capabilities()
        # 能力表说得出「这条会话能换模型」——否则界面上连下拉都不会有。
        assert capabilities.models.conversation_scoped.is_supported
        assert await driver.set_conversation_model(runtime, "fake:large") == "fake:large"
    finally:
        await driver.stop_runtime(runtime)


async def test_config_option_presets_report_the_engines_own_reason_when_refused(
    preset, workspace: str
) -> None:
    """被拒 ≠ 不支持，在 configOptions 这条路上同样成立（AD-158）。"""
    if preset.quirks.model_switch != "config_option":
        pytest.skip("该预设不走 configOptions 换模型")
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        with pytest.raises(ModelRejectedError) as raised:
            await driver.set_conversation_model(runtime, "fake:nonexistent")
        assert raised.value.reason and "unknown value" in raised.value.reason
        assert not isinstance(raised.value, UnsupportedCapabilityError)
    finally:
        await driver.stop_runtime(runtime)


async def test_effort_suffix_presets_send_a_suffixed_model_id(
    preset, workspace: str
) -> None:
    """``model_id_format="effort_suffix"``：裸 id 会被引擎拒掉（AD-158）。

    装扮复刻真机原话：``session/set_model`` 收到不带 ``[effort]`` 的 id 就回
    ``-32603 Unsupported format``。所以这条用例能过，只可能是因为 Driver 真的
    按这条会话的推理档补了后缀；补错档位也会红（装扮里那几档是闭集）。
    """
    if preset.quirks.model_id_format != "effort_suffix":
        pytest.skip("该预设的 modelId 是普通形状")
    harness = FakeAcpHarness(
        preset=preset, workspace_root=workspace, reasoning_effort="low"
    )
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert await driver.set_conversation_model(runtime, "fake:large") == "fake:large"
        # 已经带后缀的 id 原样发，不套第二层。
        assert (
            await driver.set_conversation_model(runtime, "fake:small[high]")
            == "fake:small[high]"
        )
    finally:
        await driver.stop_runtime(runtime)


async def test_resume_requires_close_presets_still_resume_in_the_same_process(
    preset, workspace: str
) -> None:
    """``resume_requires_close``：先 ``session/close`` 再 resume（AD-158）。

    装扮复刻真机：对**还活着**的会话直接 resume 回 ``-32602 already active``。
    所以这条用例能过，只可能是因为 Driver 真的先关了一次；而对不需要这一步的
    引擎，那一次 ``session/close`` 回 ``-32601``，必须被静默放过（不进 warning，
    否则每条续接都会挂一条看不懂的告警）。
    """
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    session = await driver.create_native_session(binding, CreateSessionOptions())
    resumed = conversation.model_copy(
        update={"native_session_id": session.native_session_id}
    )
    quirks = preset.quirks
    if not quirks.supports_session_resume and not quirks.supports_session_load:
        pytest.skip("该预设续接不了，另有用例覆盖")
    runtime = await driver.start_runtime(resumed, "card")
    try:
        assert runtime.metadata["resumed"] is True
    finally:
        await driver.stop_runtime(runtime)
    assert not [w for w in driver.warnings if w.startswith("resume_close_failed")]


def test_every_preset_row_is_internally_consistent() -> None:
    """新旧两组位不许各说各的（AD-158）。

    三条不变式，任何一条破了都意味着界面与实际行为会分叉：

    ① ``supports_set_model`` ⇔ ``model_switch == "set_model"``——旧那一位问的是
       「协议里那个方法认不认」，新这一位是下发时走哪条路，同一件事的两面；
    ② mode 可表示工作模式、权限或思考；可通过 set_mode 或 config option 写入；
    ③ ``verified_bits`` 只能是怪癖位的名字（写错一个名字，取证等级就会静默失效）。
    """
    from drivers.acp.presets import QUIRK_BITS

    for preset_row in PRESETS.values():
        quirks = preset_row.quirks
        assert quirks.supports_set_model == (quirks.model_switch == "set_model"), (
            preset_row.id
        )
        assert quirks.mode_semantics in {"none", "approval", "thought_level"}
        if quirks.prefer_session_load:
            assert quirks.supports_session_load, preset_row.id
        if preset_row.approval_mode_ids:
            assert quirks.mode_semantics == "approval" or preset_row.approval_option_id, preset_row.id
            assert set(preset_row.approval_mode_ids) <= {"ask", "auto", "deny", "bypass", "read_only", "plan"}
            assert all(isinstance(value, str) and value for value in preset_row.approval_mode_ids.values())
        unknown = preset_row.verified_bits - set(QUIRK_BITS)
        assert not unknown, f"{preset_row.id} 的 verified_bits 里有不认识的位 {unknown}"


async def test_verified_bits_upgrade_the_matrix_from_declared_to_live(
    preset, workspace: str
) -> None:
    """取证等级真的落到能力矩阵上（AD-158）。

    ``verified_bits`` 存在的唯一用途就是这个：矩阵上那一格该写 ``live`` 还是
    ``declared``。不断言这一条，这个字段就只是一份没人读的备忘录。
    """
    harness = FakeAcpHarness(preset=preset, workspace_root=workspace)
    driver = harness.make_driver()
    capabilities = await driver.get_capabilities()
    assert capabilities.card.reasoning.verification == (
        "live" if preset.verified("thought_chunks") else "declared"
    )
    assert capabilities.models.conversation_scoped.verification == (
        "live" if preset.verified("supports_set_model") else "declared"
    )


def test_workspace_fixture_is_a_real_temp_dir(workspace: str) -> None:
    """边界测试的根必须是**真目录**：realpath 校验对不存在的路径没有意义。"""
    assert Path(workspace).is_dir()
    assert not Path(workspace).is_relative_to(Path(tempfile.gettempdir()) / "nonexistent")
