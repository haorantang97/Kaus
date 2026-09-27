"""Driver 编排层里那些契约套件覆盖不到的分支。"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.base import (
    CreateSessionOptions,
    DriverError,
    InteractionResponse,
    MessageInput,
)
from drivers.contract_tests.harness import collect_until_run_terminal
from drivers.hermes import capabilities as capabilities_mod
from drivers.hermes.driver import HermesDriver
from drivers.hermes.testing.fake_api_server import CAPABILITIES
from drivers.hermes.testing.harness import FakeHermesHarness


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


async def _ready(rig) -> tuple[HermesDriver, object, object]:
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    return driver, project, binding


# --------------------------------------------------------------------------- #
# 审批（规格 §2.8）
# --------------------------------------------------------------------------- #


async def test_approval_400_is_treated_as_resolved_elsewhere_not_an_error(rig) -> None:
    """规格 §2.8：``invalid_approval_choice`` 说明审批已被别处解决，**不向用户报错**。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(
            __import__(
                "drivers.hermes.testing.fake_api_server", fromlist=["hold_script"]
            ).hold_script
        )
        await driver.send_message(runtime, MessageInput(text="do it"))
        # 假服务器恒定以 400 回应（本轮无待决审批），Driver 必须收敛成卡片状态。
        await driver.resolve_interaction(
            runtime,
            "appr_1",
            InteractionResponse(kind="permission", option_id="allow_once"),
        )
        events = []
        async for envelope in driver.events(runtime):
            events.append(envelope)
            if envelope.event.type == "permission.resolved":
                break
        assert events[-1].event.decision == "resolved_elsewhere"
    finally:
        await driver.stop_runtime(runtime)


@pytest.mark.parametrize("error_code", ["approval_not_active", "approval_not_pending"])
async def test_approval_409_is_treated_as_resolved_elsewhere_not_an_error(
    rig, monkeypatch, error_code: str
) -> None:
    """Hermes 0.21.0 changed the no-pending response from 400 to 409."""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        rig.servers["default"].set_script(
            __import__(
                "drivers.hermes.testing.fake_api_server", fromlist=["hold_script"]
            ).hold_script
        )
        await driver.send_message(runtime, MessageInput(text="do it"))

        client = driver._state(runtime).supervisor.client()  # noqa: SLF001
        original_request = client.request

        async def request(method, path, **kwargs):
            if method == "POST" and path.endswith("/approval"):
                import json

                from drivers.hermes.http_client import HttpResponse

                return HttpResponse(
                    status=409,
                    headers={},
                    body=json.dumps(
                        {"error": {"message": "already resolved", "code": error_code}}
                    ),
                )
            return await original_request(method, path, **kwargs)

        monkeypatch.setattr(client, "request", request)
        await driver.resolve_interaction(
            runtime,
            "appr_1",
            InteractionResponse(kind="permission", option_id="allow_once"),
        )
        async for envelope in driver.events(runtime):
            if envelope.event.type == "permission.resolved":
                assert envelope.event.decision == "resolved_elsewhere"
                break
    finally:
        await driver.stop_runtime(runtime)


async def test_option_ids_map_to_the_measured_choice_set(rig) -> None:
    """规格 §2.8 的映射表；取值集合已由 400 响应实测。"""
    from drivers.hermes.translator import APPROVAL_CHOICES, OPTION_TO_CHOICE

    assert set(OPTION_TO_CHOICE.values()) == set(APPROVAL_CHOICES)
    assert OPTION_TO_CHOICE == {
        "allow_once": "once",
        "allow_session": "session",
        "allow_always": "always",
        "deny": "deny",
    }


# --------------------------------------------------------------------------- #
# 能力映射（规格 §6）
# --------------------------------------------------------------------------- #


def test_capability_translation_from_the_measured_payload() -> None:
    report = capabilities_mod.translate_capabilities(CAPABILITIES)
    caps = report.capabilities
    assert caps.structured_events.is_supported
    assert caps.sessions.list.value == "all" and caps.sessions.create
    assert caps.sessions.history and caps.sessions.branch.is_supported
    # 冷续接：靠回合上的会话头接回上下文，站内没有活着的会话进程。
    assert caps.sessions.resume.value == "cold"
    # 批次六：原来的三条 partial 各自的去处。
    assert caps.card.tools.calls.is_supported, "tool_progress_events 给得出调用与进度"
    assert caps.card.tools.output.is_unsupported, "事件不带工具输出（AD-68）"
    assert caps.card.reasoning.is_unknown, "AD-56 的三分支还没收窄 → 说不清就是未知"
    # AD-60：协议原生审批 + 取证只到假引擎。
    assert caps.card.permissions.value == "protocol"
    assert caps.card.permissions.verification == "bench"
    for state in (caps.card.tools.output, caps.card.reasoning, caps.card.permissions):
        assert (state.note or "").strip(), "note 留在 detail 层，供 API 与能力面板解释"
    assert caps.models.mode == "constrained"
    projection = caps.capability_projection
    assert projection["skills"].value == "partial"
    assert "memory" not in projection
    assert projection["cron"].value == "unsupported"
    # AD-25：backend-scoped 能力项。
    assert projection["hermes:fast_mode"].value == "native"
    # 未声明的能力类型必须收敛为 UNKNOWN，不假定支持。
    assert caps.support_for("definitely-not-declared").value == "unknown"


def test_endpoints_are_read_from_the_capabilities_table_not_hardcoded() -> None:
    """规格 §6：``endpoints`` 的尾部在报告里被截断（§8-⑦）→ 必须运行时读整张表。"""
    report = capabilities_mod.translate_capabilities(
        {"endpoints": {"runs": {"method": "POST", "path": "/custom/runs"}}}
    )
    assert report.endpoints.path("runs") == "/custom/runs"
    # 表里没有的落回默认值，而不是崩掉。
    assert report.endpoints.path("run_stop", run_id="run_1") == "/v1/runs/run_1/stop"


def test_permissions_need_both_features_to_be_true() -> None:
    """规格 §6.1：``approval_events`` 与 ``run_approval_response`` 缺一 → false。

    「能看见但解决不了」不算支持。
    """
    base = dict(CAPABILITIES["features"])
    base["run_approval_response"] = False
    report = capabilities_mod.translate_capabilities({"features": base})
    assert report.capabilities.card.permissions.is_unsupported


def test_empty_capabilities_declare_nothing_as_supported() -> None:
    caps = capabilities_mod.translate_capabilities(None).capabilities
    assert caps.structured_events.is_unsupported
    assert caps.card.streaming.is_unsupported and caps.card.interrupt.is_unsupported
    assert caps.sessions.list.is_unsupported


# --------------------------------------------------------------------------- #
# 投射与 Drift（规格 §2.4）
# --------------------------------------------------------------------------- #


async def test_projection_lists_every_capability_somewhere(rig) -> None:
    from app.capabilities.models import EffectiveCapabilities, EffectiveCapability

    driver, project, binding = await _ready(rig)
    # 批次二十四：落点由**写表**决定（`drivers.hermes.projection_map`），
    # 不再按 capability_projection 的声明猜——所以这里用写表里真有的坐标。
    entries = tuple(
        EffectiveCapability(
            capability_type=capability_type,
            capability_id=capability_id,
            config={"value": {"demo": True}},
            source_project_id=project.id,
            inherited=False,
        )
        for capability_type, capability_id in (
            ("mcp", "mcp_servers"),
            ("memory", "memory"),
            ("hooks", "hooks"),
            ("never-declared-type", "demo"),
        )
    )
    result = await driver.materialize_project_capabilities(
        project, binding, EffectiveCapabilities(project_id=project.id, entries=entries)
    )
    touched = {e.capability_type for e in (*result.applied, *result.unsupported)}
    assert touched == {"mcp", "memory", "hooks", "never-declared-type"}
    applied = {e.capability_type: e for e in result.applied}
    # 能落地的指向 config.yaml 的键路径；不含任何 Secret。
    assert applied["mcp"].target_ref.endswith("config.yaml#mcp_servers")
    assert applied["mcp"].key_path == "mcp_servers"
    assert applied["mcp"].action == "set"
    # 写表里没有的类型必须显式落 unsupported，并说清楚为什么。
    missing = {e.capability_type: e for e in result.unsupported}
    assert missing["never-declared-type"].reason == "not_mapped"
    # 默认 dry-run：算得出改动，但一个字节都没写。
    assert result.dry_run is True and result.backup_path is None
    assert result.warnings, "dry-run 这件事必须写在 warnings 里"


async def test_drift_reports_stale_rather_than_pretending_to_be_in_sync(rig) -> None:
    driver, project, binding = await _ready(rig)
    report = await driver.inspect_drift(project, binding)
    assert report.entries and report.entries[0].state == "stale"
    assert report.in_sync is False
    assert report.in_sync == (not report.entries)


# --------------------------------------------------------------------------- #
# 会话与并发
# --------------------------------------------------------------------------- #


async def test_created_session_id_is_returned_not_guessed(rig) -> None:
    """v1.0 §8.7：``POST /api/sessions`` 直接返回确定 id，不按「最新会话」猜。"""
    driver, project, binding = await _ready(rig)
    first = await driver.create_native_session(binding, CreateSessionOptions(title="A"))
    second = await driver.create_native_session(binding, CreateSessionOptions(title="B"))
    assert first.native_session_id != second.native_session_id
    assert first.native_session_id.startswith("api_")
    listed = {s.native_session_id: s for s in await driver.list_native_sessions(binding)}
    assert first.native_session_id in listed
    assert listed[first.native_session_id].title == "A"


async def test_second_send_while_a_run_is_in_flight_is_refused(rig) -> None:
    from drivers.hermes.testing.fake_api_server import hold_script

    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    rig.servers["default"].set_script(hold_script)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="one"))
        with pytest.raises(DriverError):
            await driver.send_message(runtime, MessageInput(text="two"))
    finally:
        await driver.stop_runtime(runtime)


async def test_stop_runtime_does_not_stop_the_gateway(rig) -> None:
    """规格 §2.6：``stop_runtime`` 关 SSE、释放句柄，**不停进程**。"""
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    await driver.stop_runtime(runtime)
    # gateway 还活着：还能继续建会话。
    assert (await driver.create_native_session(binding, CreateSessionOptions())).native_session_id


async def test_history_falls_back_when_http_is_gone(rig, tmp_path: Path) -> None:
    """规格 §4.2 第 3 级：HTTP 与直读都不可用 → 显式报缺，不假装成功。"""
    driver, project, binding = await _ready(rig)
    rig.servers["default"].stop()
    history = await driver.load_native_history(binding, "api_1788339747_a7afdfa5")
    assert history.complete is False
    assert history.missing == ("history",)


async def test_message_completed_uses_the_backend_output(rig) -> None:
    driver, project, binding = await _ready(rig)
    conversation = rig.make_conversation(project, binding)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        await driver.send_message(runtime, MessageInput(text="hi"))
        envelopes = await collect_until_run_terminal(driver, runtime)
    finally:
        await driver.stop_runtime(runtime)
    completed = [e for e in envelopes if e.event.type == "message.completed"]
    assert completed and completed[-1].event.text == "HERMES-PROBE-TOOL-MARKER"


# --------------------------------------------------------------------------- #
# 批次四十三：随会话送来的 MCP 到了也不落盘
# --------------------------------------------------------------------------- #


async def test_session_metadata_never_reaches_the_config_file(rig) -> None:
    """``metadata["mcpServers"]`` 到了这台 Driver 手上也**不写文件**（批次四十三）。

    这家引擎走的是「写进自己的持久配置」那条投影路（``materialize_project_
    capabilities``），不是「随建会话送一次」那条。所以两件事都要成立：

    1. 普通 HTTP 对话的 ``session_options_for`` 返回 None；独立执行使用
       组合驱动的临时会话投射，不修改持久配置；
    2. 就算有人直接把带 ``mcpServers`` 的 ``metadata`` 递进 ``create_native_
       session``，``config.yaml`` 也一个字节都不变。
    """
    driver, project, binding = await _ready(rig)
    home = Path(binding.runtime_config["api_server"]["hermes_home"])
    config_path = home / "config.yaml"
    before = config_path.read_bytes() if config_path.exists() else None

    from app.capabilities.models import EffectiveCapabilities
    conversation = rig.make_conversation(project, binding)
    assert await driver.session_options_for(conversation, EffectiveCapabilities(project_id=project.id)) is None

    await driver.create_native_session(
        binding,
        CreateSessionOptions(
            title="带着 MCP 来的",
            metadata={
                "mcpServers": [
                    {"name": "files", "command": "node", "args": [], "env": []}
                ]
            },
        ),
    )

    after = config_path.read_bytes() if config_path.exists() else None
    assert after == before, "随会话送来的 MCP 不该落到这台引擎的配置文件里"
