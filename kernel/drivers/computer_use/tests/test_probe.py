import asyncio
import json
import os
import sys

import pytest

from drivers.computer_use import probe_fallbacks
from drivers.computer_use.probe import permission_requirements


def grants(screen=True, accessibility=True):
    return {"accessibility": accessibility, "screen_recording": screen, "source": {"attribution": "driver-daemon", "bundle_id": "com.trycua.driver"}}


def peekaboo_grants(screen=True, accessibility=True):
    return {"success": True, "data": {"permissions": [
        {"name": "Screen Recording", "isRequired": True, "isGranted": screen},
        {"name": "Accessibility", "isRequired": True, "isGranted": accessibility},
        {"name": "Event Synthesizing", "isRequired": False, "isGranted": False},
    ]}}


async def test_probe_only_lists_tools_and_reuses_documented_launchers(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.settings.managed_executable", lambda: "/installed/cua-driver")
    calls = []
    async def tools(server, timeout):
        calls.append(server)
        return {"browser_navigate", "browser_snapshot", "browser_click"} if "playwright" in server["name"] else {"get_window_state", "click", "type_text"}
    async def permissions(command, timeout):
        calls.append(command)
        return grants()
    candidates = await probe_fallbacks(which=lambda name: "/installed/" + name, platform="darwin",
                                       list_tools=tools, permissions=permissions)
    assert all(candidate.status == "ready" for candidate in candidates)
    assert all(candidate.verified for candidate in candidates)
    assert len(calls) == 3
    assert candidates[1].mcp_server["args"][-2:] == ["--driver", "/installed/cua-driver"]


async def test_missing_permissions_stop_before_mcp_start(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.settings.managed_executable", lambda: "/installed/cua-driver")
    async def permissions(command, timeout):
        return grants(accessibility=False)
    async def tools(*_):
        pytest.fail("MCP must not start before required permission is present")
    candidates = await probe_fallbacks(capabilities=["desktop"], platform="darwin",
                                       which=lambda name: "/installed/" + name,
                                       list_tools=tools, permissions=permissions)
    assert candidates[0].status == "needs_setup"
    assert candidates[0].requirements == ("accessibility",)


async def test_absent_fallback_does_not_execute_anything():
    async def tools(*_):
        pytest.fail("No provider is installed")
    candidates = await probe_fallbacks(which=lambda _: None, platform="darwin", list_tools=tools)
    assert all(candidate.status == "unavailable" for candidate in candidates)


async def test_protocol_failure_and_wrong_catalog_do_not_claim_readiness():
    async def fail(*_):
        raise RuntimeError("/private/path credentialish diagnostic")
    async def wrong(*_):
        return {"browser_click"}
    for function, requirement in ((fail, "mcp-probe"), (wrong, "mcp-tool-catalog")):
        candidates = await probe_fallbacks(capabilities=["browser"], which=lambda _: "/tool",
                                           list_tools=function)
        assert candidates[0].requirements == (requirement,)
        assert not candidates[0].verified
        assert "private" not in str(candidates[0].to_dict())


async def test_probe_cancellation_propagates():
    async def cancel(*_):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await probe_fallbacks(capabilities=["browser"], which=lambda _: "/tool", list_tools=cancel)


def test_malformed_permission_response_does_not_grant_access():
    assert permission_requirements({}) == ("permission-probe",)
    assert permission_requirements(grants()) == ()
    assert permission_requirements(grants(screen=False)) == ("screen-recording",)
    assert permission_requirements(grants(screen=None)) == ("permission-probe",)
    assert permission_requirements({"data": {"permissions": [
        {"name": "Screen Recording", "isGranted": True}]}}) == ("permission-probe",)


async def test_permission_timeout_is_unknown_not_missing_grant_or_mcp_failure(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.settings.managed_executable", lambda: "/installed/cua-driver")
    async def timed_out(*_):
        raise TimeoutError
    async def tools(*_):
        pytest.fail("Unknown permission host must not launch desktop MCP")
    candidates = await probe_fallbacks(capabilities=["desktop"], platform="darwin",
                                      which=lambda _: "/installed/peekaboo",
                                      permissions=timed_out, list_tools=tools)
    assert candidates[0].status == "unknown"
    assert candidates[0].requirements == ("permission-probe",)
    assert not candidates[0].verified


async def test_fixed_app_probe_and_mcp_use_the_same_host(tmp_path, monkeypatch):
    from drivers.computer_use import probe, settings
    from unittest.mock import AsyncMock
    command = str(tmp_path / "CuaDriver.app/Contents/MacOS/cua-driver")
    monkeypatch.setattr(settings, "managed_executable", lambda: command)
    permission_call = AsyncMock(return_value=grants())
    monkeypatch.setattr(probe, "_cua_permissions", permission_call)
    calls = []
    async def tools(server, timeout):
        calls.append(server)
        return {"get_window_state", "click", "type_text"}
    candidates = await probe_fallbacks(capabilities=["desktop"], platform="darwin", list_tools=tools)
    permission_call.assert_awaited_once_with(command, 8)
    assert calls[0]["args"][-2:] == ["--driver", command]
    assert candidates[0].status == "ready"


async def test_fixed_app_permission_command_rejects_local_grants(tmp_path, monkeypatch):
    from drivers.computer_use import probe, peekaboo_host
    from unittest.mock import AsyncMock
    command = tmp_path / "peekaboo"
    command.write_text("#!" + sys.executable + "\nimport json\nprint(" + repr(json.dumps(
        {**peekaboo_grants(), "data": {**peekaboo_grants()["data"], "source": "local"}})) + ")\n")
    command.chmod(0o700)
    start = AsyncMock()
    monkeypatch.setattr(peekaboo_host, "start_app_host", start)
    with pytest.raises(ValueError, match="Permission host unavailable"):
        await probe._permissions(str(command), 2, socket="/synthetic/bridge.sock")
    start.assert_awaited_once_with("/synthetic/bridge.sock", 2)


async def test_real_stdio_probe_never_calls_a_tool(tmp_path):
    from drivers.computer_use.probe import _list_tools
    script = tmp_path / "server.py"
    log = tmp_path / "methods.jsonl"
    script.write_text("""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    with open(sys.argv[1], 'a') as log:
        log.write(json.dumps(method) + '\\n')
    if 'id' not in request:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'fake', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}}
                            for name in ['browser_navigate', 'browser_snapshot', 'browser_click']]}
    elif method == 'ping':
        result = {}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                          'error': {'code': -32601, 'message': 'Unsupported'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
""")
    names = await _list_tools({"command": sys.executable, "args": [str(script), str(log)]}, 5)
    assert names == {"browser_navigate", "browser_snapshot", "browser_click"}
    methods = [json.loads(line) for line in log.read_text().splitlines()]
    assert "initialize" in methods and "tools/list" in methods
    assert "tools/call" not in methods


async def test_permission_subprocess_is_reaped_on_timeout(tmp_path, monkeypatch):
    from drivers.computer_use.probe import _permissions
    command = tmp_path / "peekaboo"
    command.write_text("#!/bin/sh\nexec sleep 30\n")
    command.chmod(0o700)
    processes = []
    create = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(TimeoutError):
        await _permissions(str(command), .15)
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


async def test_setup_probe_cache_can_be_explicitly_refreshed(tmp_path, monkeypatch):
    import drivers.computer_use.probe as module
    command = tmp_path / "playwright-mcp"
    command.write_text("synthetic executable metadata")
    calls = []
    async def tools(*_):
        calls.append("probe")
        return {"browser_navigate", "browser_snapshot", "browser_click"}
    monkeypatch.setattr(module, "_list_tools", tools)
    options = {"capabilities": ["browser"], "which": lambda _: str(command)}
    first = await probe_fallbacks(**options, context_id="one")
    second = await probe_fallbacks(**options, context_id="two")
    assert calls == ["probe"]
    assert first[0].resource_id != second[0].resource_id
    await probe_fallbacks(**options, force=True)
    assert calls == ["probe", "probe"]


def test_cua_actual_structured_permissions_take_precedence_over_human_content():
    from mcp.types import CallToolResult
    from drivers.computer_use.probe import cua_permission_payload
    actual = {"content": [{"type": "text", "text": "❌ Accessibility: NOT granted."}],
        "structuredContent": grants(screen=False, accessibility=False), "isError": False}
    result = CallToolResult.model_validate(actual)
    value = cua_permission_payload(result)
    assert permission_requirements(value) == ("screen-recording", "accessibility")
    assert value == actual["structuredContent"]


def test_cua_legacy_json_content_can_follow_non_json_diagnostic():
    from mcp.types import CallToolResult
    from drivers.computer_use.probe import cua_permission_payload
    result = CallToolResult.model_validate({"content": [
        {"type": "text", "text": "Permission status"},
        {"type": "text", "text": json.dumps(grants())}], "isError": False})
    assert permission_requirements(cua_permission_payload(result)) == ()
