"""stdio JSON-RPC 客户端的单测（对着真的假 agent 子进程跑）。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from drivers.acp.capabilities import initialize_params
from drivers.acp.client import (
    AcpAgentSpec,
    AcpConnection,
    AcpRpcError,
    AcpTransportError,
    call_with_param_shapes,
)
from drivers.acp.testing.fake_acp_agent import fake_agent_spec


async def test_spawn_initialize_and_close() -> None:
    connection = await AcpConnection.spawn(fake_agent_spec())
    try:
        result = await connection.call("initialize", initialize_params(), timeout=20)
        assert result["protocolVersion"] == 1
        assert result["agentInfo"]["name"] == "fake-acp-agent"
        assert connection.alive
        # 日志走 stderr、stdout 只有 JSON-RPC —— 这是 ACP 的硬要求。
        assert connection.protocol_noise() == ()
    finally:
        await connection.aclose()
    assert not connection.alive


async def test_rpc_error_keeps_code_and_data() -> None:
    """``-32602`` 的 ``data`` 点名了缺哪个参数，是形状协商的唯一依据，不能丢。"""
    connection = await AcpConnection.spawn(fake_agent_spec())
    try:
        await connection.call("initialize", initialize_params(), timeout=20)
        with pytest.raises(AcpRpcError) as excinfo:
            await connection.call("session/new", {"cwd": "/tmp"}, timeout=20)
        assert excinfo.value.code == -32602
        assert "mcpServers" in json.dumps(excinfo.value.data)
    finally:
        await connection.aclose()


async def test_unknown_method_is_method_not_found() -> None:
    connection = await AcpConnection.spawn(fake_agent_spec())
    try:
        await connection.call("initialize", initialize_params(), timeout=20)
        with pytest.raises(AcpRpcError) as excinfo:
            await connection.call("session/nope", {}, timeout=20)
        assert excinfo.value.code == -32601
    finally:
        await connection.aclose()


async def test_call_with_param_shapes_negotiates() -> None:
    connection = await AcpConnection.spawn(fake_agent_spec())
    try:
        await connection.call("initialize", initialize_params(), timeout=20)
        result, params = await call_with_param_shapes(
            connection,
            "session/new",
            [{"cwd": tempfile.gettempdir()}, {"cwd": tempfile.gettempdir(), "mcpServers": []}],
            timeout=20,
        )
        assert "sessionId" in result
        assert "mcpServers" in params, "协商必须落在真正被接受的那个形状上"
    finally:
        await connection.aclose()


async def test_call_with_param_shapes_reraises_last_error() -> None:
    connection = await AcpConnection.spawn(fake_agent_spec())
    try:
        await connection.call("initialize", initialize_params(), timeout=20)
        with pytest.raises(AcpRpcError):
            await call_with_param_shapes(
                connection, "session/new", [{}, {"cwd": None}], timeout=20
            )
    finally:
        await connection.aclose()


async def test_spawn_of_a_missing_binary_is_a_transport_error() -> None:
    spec = AcpAgentSpec(command=("definitely-not-a-real-agent-binary-xyz",))
    with pytest.raises(AcpTransportError):
        await AcpConnection.spawn(spec)


async def test_call_timeout_is_reported_not_hung() -> None:
    connection = await AcpConnection.spawn(fake_agent_spec("interrupt"))
    try:
        await connection.call("initialize", initialize_params(), timeout=20)
        session = await connection.call(
            "session/new", {"cwd": tempfile.gettempdir(), "mcpServers": []}, timeout=20
        )
        with pytest.raises(AcpTransportError):
            await connection.call(
                "session/prompt",
                {
                    "sessionId": session["sessionId"],
                    "prompt": [{"type": "text", "text": "hi"}],
                },
                timeout=0.3,
            )
    finally:
        await connection.aclose()


async def test_stdout_pollution_is_recorded_not_fatal() -> None:
    """agent 误把日志写进 stdout 时，连接不能静默死掉——要留下证据。"""
    script = textwrap.dedent(
        """
        import json, sys
        sys.stdout.write("startup banner, not JSON\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}) + "\\n")
            sys.stdout.flush()
        """
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "noisy_agent.py"
        path.write_text(script, encoding="utf-8")
        connection = await AcpConnection.spawn(
            AcpAgentSpec(command=(sys.executable, str(path)))
        )
        try:
            assert await connection.call("ping", {}, timeout=10) == {"ok": True}
            assert connection.protocol_noise() == ("startup banner, not JSON",)
        finally:
            await connection.aclose()


async def test_agent_request_without_handler_gets_method_not_found() -> None:
    """agent 发来的请求必须**有回执**，否则它会永远阻塞在那里。"""
    script = textwrap.dedent(
        """
        import json, sys
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "fs/read_text_file",
             "params": {"path": "/etc/passwd"}}) + "\\n")
        sys.stdout.flush()
        reply = json.loads(sys.stdin.readline())
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "method": "echo", "params": reply}) + "\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            pass
        """
    )
    seen: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "requesting_agent.py"
        path.write_text(script, encoding="utf-8")
        connection = await AcpConnection.spawn(
            AcpAgentSpec(command=(sys.executable, str(path)))
        )
        connection.on_notification = lambda method, params: seen.append((method, params))
        try:
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.02)
            assert seen, "客户端没有回执，agent 卡住了"
            (_method, echoed) = seen[0]
            assert echoed["error"]["code"] == -32601
        finally:
            await connection.aclose()
