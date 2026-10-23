import asyncio
import json
import sys
from pathlib import Path

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import devin_runtime as runtime
from drivers.acp.client import AcpAgentSpec


class Connection:
    on_notification = None


def event(connection, name, message, level="info"):
    connection.on_notification("_cognition.ai/output", {
        "channel": f"MCP: {name}", "message": message, "level": level, "sessionId": "",
    })


def connecting(connection, name):
    event(connection, name, f"Connecting to MCP server '{name}'")


def connected(connection, name):
    event(connection, name, f"MCP server '{name}' connected successfully")


async def test_requires_each_requested_native_connection_and_preserves_observer():
    connection = Connection()
    seen = []
    previous = lambda *args: seen.append(args)
    connection.on_notification = previous
    metadata = {"mcpServers": [{"name": "one"}, {"name": "two"}]}
    assert await runtime.before_runtime_session(connection, None, None, metadata) is metadata
    connecting(connection, "one")
    connected(connection, "one")
    waiter = asyncio.create_task(runtime.after_runtime_session(connection, None, None, metadata, "session"))
    await asyncio.sleep(0)
    assert not waiter.done()
    connecting(connection, "two")
    connected(connection, "two")
    await waiter
    assert len(seen) == 4
    assert connection.on_notification is previous


async def test_native_failure_aborts_even_after_successful_session_new():
    connection = Connection()
    metadata = {"mcpServers": [{"name": "broken"}]}
    await runtime.before_runtime_session(connection, None, None, metadata)
    connecting(connection, "broken")
    event(connection, "broken", "MCP server 'broken' connection failed: /private/raw/path", "warn")
    with pytest.raises(UnsupportedCapabilityError, match="MCP 连接失败：broken") as exc:
        await runtime.after_runtime_session(connection, None, None, metadata, "created")
    assert "/private/" not in str(exc.value)
    assert connection.on_notification is None


async def test_unrelated_or_unconfirmed_messages_do_not_confirm_startup(monkeypatch):
    monkeypatch.setattr(runtime, "STATUS_TIMEOUT", .01)
    connection = Connection()
    metadata = {"mcpServers": [{"name": "one"}]}
    await runtime.before_runtime_session(connection, None, None, metadata)
    connected(connection, "one")  # No connecting lifecycle.
    connecting(connection, "other")
    connected(connection, "other")
    with pytest.raises(UnsupportedCapabilityError, match="未确认"):
        await runtime.after_runtime_session(connection, None, None, metadata, "created")
    assert connection.on_notification is None


async def test_cancellation_restores_handler_and_empty_mcp_needs_no_extension():
    connection = Connection()
    await runtime.before_runtime_session(connection, None, None, {})
    await runtime.after_runtime_session(connection, None, None, {}, "created")
    assert connection.on_notification is None
    metadata = {"mcpServers": [{"name": "pending"}]}
    await runtime.before_runtime_session(connection, None, None, metadata)
    waiter = asyncio.create_task(runtime.after_runtime_session(connection, None, None, metadata, "created"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert connection.on_notification is None


async def test_simultaneous_connections_never_confirm_each_other(monkeypatch):
    monkeypatch.setattr(runtime, "STATUS_TIMEOUT", .01)
    one, two = Connection(), Connection()
    metadata = {"mcpServers": [{"name": "fixture"}]}
    await runtime.before_runtime_session(one, None, None, metadata)
    await runtime.before_runtime_session(two, None, None, metadata)
    connecting(one, "fixture")
    connected(one, "fixture")
    await runtime.after_runtime_session(one, None, None, metadata, "one")
    with pytest.raises(UnsupportedCapabilityError, match="未确认"):
        await runtime.after_runtime_session(two, None, None, metadata, "two")


async def test_model_messages_or_session_scoped_output_do_not_confirm_mcp(monkeypatch):
    monkeypatch.setattr(runtime, "STATUS_TIMEOUT", .01)
    connection = Connection()
    metadata = {"mcpServers": [{"name": "fixture"}]}
    await runtime.before_runtime_session(connection, None, None, metadata)
    connecting(connection, "fixture")
    message = "MCP server 'fixture' connected successfully"
    connection.on_notification("session/update", {
        "sessionId": "session", "update": {"sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": message}},
    })
    connection.on_notification("_cognition.ai/output", {
        "sessionId": "session", "channel": "MCP: fixture", "level": "info", "message": message,
    })
    event(connection, "fixture", message, "warn")
    with pytest.raises(UnsupportedCapabilityError, match="未确认"):
        await runtime.after_runtime_session(connection, None, None, metadata, "session")


@pytest.mark.parametrize("servers", [None, [None], [{"name": "x\n"}], [{"name": "x"}, {"name": "x"}]])
async def test_invalid_requested_names_rejected_before_native_launch(servers):
    connection = Connection()
    with pytest.raises(UnsupportedCapabilityError):
        await runtime.before_runtime_session(connection, None, None, {"mcpServers": servers})
    assert connection.on_notification is None


async def test_project_names_are_stable_private_and_unrelated_native_servers_are_preserved(tmp_path):
    command = tmp_path / "native.py"
    command.write_text("""import sys
assert sys.argv[1:3] == ['mcp', 'get']
name = sys.argv[3]
if name == 'conflict':
 print('Server: conflict')
else:
 print("Error: Server '%s' not found" % name, file=sys.stderr)
 sys.exit(1)
""")
    spec = AcpAgentSpec(command=(sys.executable, str(command), "acp"), cwd=str(tmp_path), env={"KAUS_SKILL_STATE_ROOT": str(tmp_path / "runtime")})
    first = {"mcpServers": [{"name": "conflict"}]}
    prepared = await runtime.prepare_runtime_spec(spec, None, first)
    assert prepared.env["XDG_CACHE_HOME"].startswith(str(tmp_path / "runtime"))
    assert "XDG_CACHE_HOME" not in spec.env
    scoped = first['mcpServers'][0]['name']
    assert scoped.startswith('kaus_') and scoped != 'conflict'
    assert first['_kausDevinMcpAliases'] == {scoped: 'conflict'}
    second = {"mcpServers": [{"name": "conflict"}]}
    await runtime.prepare_runtime_spec(spec, None, second)
    assert first == second
    await runtime.prepare_runtime_spec(spec, None, first)
    assert first == second


async def test_private_namespace_native_collision_is_not_silently_accepted(monkeypatch):
    async def exists(*args): return True
    monkeypatch.setattr(runtime, '_native_name_exists', exists)
    with pytest.raises(UnsupportedCapabilityError, match='连接名称冲突：project'):
        await runtime.prepare_runtime_spec(AcpAgentSpec(command=('devin', 'acp')), None, {'mcpServers': [{'name': 'project'}]})


async def test_empty_projection_does_not_require_native_config_introspection():
    spec = AcpAgentSpec(command=("not-installed", "acp"))
    assert await runtime.prepare_runtime_spec(spec, None, {"mcpServers": []}) is spec


async def test_native_config_check_uses_session_workspace_and_bounds_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "CONFIG_TIMEOUT", .05)
    script = tmp_path / 'wait.py'
    script.write_text('import time\ntime.sleep(60)')
    spec = AcpAgentSpec(command=(sys.executable, str(script), "acp"), cwd=str(tmp_path))
    with pytest.raises(UnsupportedCapabilityError, match="超时|核验"):
        await runtime.prepare_runtime_spec(spec, None, {"mcpServers": [{"name": "project"}]})


async def test_private_cache_refreshes_only_owned_names_and_never_changes_native_cache(tmp_path, monkeypatch):
    async def exists(*args): return False
    monkeypatch.setattr(runtime, '_native_name_exists', exists)
    original = tmp_path / 'native-cache'; original.mkdir()
    native_file = original / 'descriptions.json'; native_file.write_text('native unchanged')
    spec = AcpAgentSpec(command=('devin', 'acp'), env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'kaus'),
        'XDG_CACHE_HOME': str(original)})
    metadata = {'mcpServers': [{'name': 'selected'}]}
    first = await runtime.prepare_runtime_spec(spec, None, metadata)
    cache = Path(first.env['XDG_CACHE_HOME']) / 'devin/cli/mcp/descriptions.json'
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({metadata['mcpServers'][0]['name']: 'old command', 'native': 'preserved'}))
    second = await runtime.prepare_runtime_spec(spec, None, metadata)
    assert second.env == first.env
    assert json.loads(cache.read_text()) == {'native': 'preserved'}
    assert native_file.read_text() == 'native unchanged'
    assert spec.env['XDG_CACHE_HOME'] == str(original)
    assert await runtime.prepare_runtime_spec(spec, None, {'mcpServers': []}) is spec
