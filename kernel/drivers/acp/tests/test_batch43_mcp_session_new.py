"""项目 MCP 真的到得了 ``session/new``（批次四十三，端到端）。

这一份**不 mock 任何一层**：假 ACP agent 是真的子进程，它收到 ``mcpServers``
之后会真的把那台假 MCP 服务拉起来、走 ``initialize`` / ``tools/list`` /
``tools/call``，再把工具回的那句话当作本轮文本发回来。所以断言里出现 nonce，
等价于「这条链从能力表一路通到了工具输出」。

另外守两件小事：
- 续接会话（已有 ``native_session_id``）**不**投射——ACP 不允许在续接时换挂载；
- 怪癖位 ``mcp_via_session_new=false`` 时一个字都不送，并留一条 driverWarning。
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import replace

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AgentQuirks
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing import fake_mcp_server
from drivers.base import MessageInput
from drivers.contract_tests.harness import collect_until_run_terminal

import sys

PROJECT = "project:batch43"


def _driver(scenario: str = "mcp-ping", *, quirks: AgentQuirks | None = None) -> AcpDriver:
    workdir = tempfile.gettempdir()
    driver = AcpDriver(
        fake_agent_spec(scenario, cwd=workdir),
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )
    if quirks is not None:
        # 怪癖平时由预设 + initialize 决定；这里直接按住那一位，测的是
        # 「Driver 读到假时走哪条路」，不是「预设怎么填」。
        driver.quirks = quirks
    return driver


def _conversation(**fields) -> Conversation:
    return Conversation.create(
        project_id=PROJECT, agent_binding_id="binding:batch43:acp", title="会话", **fields
    )


def _effective(nonce: str) -> EffectiveCapabilities:
    """一条指向假 MCP 服务的 ``mcp`` 能力（stdio 形态）。"""
    return EffectiveCapabilities(
        project_id=PROJECT,
        entries=(
            EffectiveCapability(
                capability_type="mcp",
                capability_id="kaus-probe",
                config={
                    "value": {
                        "command": sys.executable,
                        "args": [
                            fake_mcp_server.__file__,
                            "--nonce",
                            nonce,
                        ],
                    }
                },
                source_project_id=PROJECT,
                inherited=False,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #


async def test_the_project_mcp_reaches_the_agent_and_the_tool_answers() -> None:
    driver = _driver()
    nonce = uuid.uuid4().hex[:12]
    conversation = _conversation()

    projection = await driver.session_options_for(conversation, _effective(nonce))
    assert projection is not None
    assert projection.summary == {"mcpServers": ["kaus-probe"]}
    servers = projection.options.metadata["mcpServers"]
    assert servers[0]["name"] == "kaus-probe" and servers[0]["env"] == []

    runtime = await driver.start_runtime(
        conversation, "card", session_options=projection.options
    )
    try:
        await driver.send_message(runtime, MessageInput(text="调用工具并原样复述"))
        events = await collect_until_run_terminal(driver, runtime, timeout=45.0)
    finally:
        await driver.stop_runtime(runtime)

    blob = "\n".join(
        getattr(envelope.event, "text", "") or "" for envelope in events
    )
    tool_titles = [
        getattr(envelope.event, "name", None)
        for envelope in events
        if envelope.event.type == "tool.started"
    ]
    assert fake_mcp_server.pong(nonce) in blob, "工具的原话没有回到时间线上"
    assert fake_mcp_server.TOOL_NAME in tool_titles


async def test_nothing_is_projected_when_resuming_an_existing_session() -> None:
    """ACP 不允许在续接时换挂载，所以这条路如实回 None（而不是算一份送不出去的）。"""
    driver = _driver()
    conversation = _conversation(native_session_id="already-there")
    assert await driver.session_options_for(conversation, _effective("x")) is None


async def test_the_quirk_bit_switches_the_whole_thing_off() -> None:
    """``mcp_via_session_new=false``：一个字都不送，并留下一条说得出口的 warning。"""
    driver = _driver(quirks=replace(AgentQuirks(), mcp_via_session_new=False))
    assert await driver.session_options_for(_conversation(), _effective("x")) is None
    assert any("mcp_via_session_new" in w for w in driver.warnings)


async def test_no_capability_means_no_projection() -> None:
    driver = _driver()
    empty = EffectiveCapabilities(project_id=PROJECT)
    assert await driver.session_options_for(_conversation(), empty) is None


# --------------------------------------------------------------------------- #
# 假 MCP 服务自己（纯函数，不起进程）
# --------------------------------------------------------------------------- #


def test_the_fake_mcp_server_answers_the_three_methods() -> None:
    nonce = "abc123"
    initialized = fake_mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, nonce
    )
    assert initialized["result"]["capabilities"] == {"tools": {}}
    listed = fake_mcp_server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, nonce
    )
    assert [t["name"] for t in listed["result"]["tools"]] == [fake_mcp_server.TOOL_NAME]
    called = fake_mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": fake_mcp_server.TOOL_NAME},
        },
        nonce,
    )
    assert called["result"]["content"][0]["text"] == f"pong-{nonce}"


def test_the_fake_mcp_server_does_not_pretend_to_know_other_methods() -> None:
    """N §13.1：不认识就说不认识，不返回空对象假装支持。"""
    answer = fake_mcp_server.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}, "n"
    )
    assert answer["error"]["code"] == -32601
    # 通知（没有 id）不该有回执。
    assert (
        fake_mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/x"}, "n")
        is None
    )
