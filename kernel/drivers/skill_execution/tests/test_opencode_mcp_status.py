"""The native MCP registry must acknowledge every projected server."""
import asyncio
import json
from pathlib import Path

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import kilo, opencode, opencode_mcp as scope


def spec_for(tmp_path, prefix):
    config = {"plugin": ["file:///synthetic/user-plugin.mjs", "file:///synthetic/skill-guard.mjs"],
              "permission": {"skill": {"*": "deny"}}, "provider": {"fixture": {"option": "retained"}}}
    return AcpAgentSpec(command=("synthetic",), inherit_env=False,
        env={prefix + "_CONFIG_CONTENT": json.dumps(config), "KAUS_SKILL_STATE_ROOT": str(tmp_path / "state")})


def servers(*names):
    return {"mcpServers": [{"name": name, "command": "synthetic-command", "args": ["SYNTHETIC_SECRET"],
                           "env": [{"name": "TOKEN", "value": "SYNTHETIC_SECRET"}]} for name in names]}


async def reply(spec, transform):
    directory = Path(spec.env[scope.CONTROL_ENV])
    while not (directory / "request.json").exists():
        await asyncio.sleep(0.001)
    assert directory.stat().st_mode & 0o777 == 0o700
    request = json.loads((directory / "request.json").read_text())
    result = {**request, "states": {name: "connected" for name in request.pop("names")}}
    transform(result)
    (directory / "response.json").write_text(json.dumps(result))


@pytest.mark.parametrize("adapter,prefix", [(opencode, "OPENCODE"), (kilo, "KILO")])
async def test_hooks_use_process_private_receipts_preserving_config(tmp_path, adapter, prefix):
    original = spec_for(tmp_path, prefix)
    prepared = await adapter.prepare_runtime_spec(original, None, servers("one", "two"))
    config = json.loads(prepared.env[prefix + "_CONFIG_CONTENT"])
    before = json.loads(original.env[prefix + "_CONFIG_CONTENT"])
    assert {key: value for key, value in config.items() if key != "plugin"} == {key: value for key, value in before.items() if key != "plugin"}
    assert config["plugin"][:-1] == before["plugin"]
    directory = Path(prepared.env[scope.CONTROL_ENV])
    assert not directory.exists()
    task = asyncio.create_task(reply(prepared, lambda result: None))
    await adapter.after_runtime_session(None, prepared, None, servers("one", "two"), "session")
    await task
    assert not directory.exists()
    assert "SYNTHETIC_SECRET" not in json.dumps(dict(prepared.env))
    assert "SYNTHETIC_SECRET" not in "".join(path.read_text() for path in (tmp_path / "state").rglob("*") if path.is_file())


async def test_restarting_or_removing_projection_does_not_reuse_receipt_or_delete_active_run(tmp_path):
    original = spec_for(tmp_path, "OPENCODE")
    first = await scope.prepare(original, servers("one"), prefix="OPENCODE")
    second = await scope.prepare(first, servers("one"), prefix="OPENCODE")
    assert first.env[scope.CONTROL_ENV] != second.env[scope.CONTROL_ENV]
    Path(first.env[scope.CONTROL_ENV]).mkdir(mode=0o700)
    assert Path(first.env[scope.CONTROL_ENV]).exists()
    assert len(json.loads(second.env["OPENCODE_CONFIG_CONTENT"])["plugin"]) == 3
    empty = await scope.prepare(second, servers(), prefix="OPENCODE")
    assert not empty.env[scope.CONTROL_ENV] and not empty.env[scope.PLUGIN_ENV]
    assert json.loads(empty.env["OPENCODE_CONFIG_CONTENT"]) == json.loads(original.env["OPENCODE_CONFIG_CONTENT"])
    assert not Path(second.env[scope.CONTROL_ENV]).exists()
    await scope.verify(empty, servers(), "session")
    assert await scope.prepare(original, {}, prefix="OPENCODE") is original


@pytest.mark.parametrize("state,message", [("failed", "MCP 连接失败"), ("disabled", "MCP 连接失败"),
    ("unavailable", "MCP 连接失败"), ("needs_auth", "MCP 需要授权"), ("needs_client_registration", "MCP 需要授权")])
async def test_native_failure_and_auth_are_not_accepted_as_ready(tmp_path, state, message):
    prepared = await scope.prepare(spec_for(tmp_path, "KILO"), servers("one"), prefix="KILO")
    task = asyncio.create_task(reply(prepared, lambda result: result.update(states={"one": state})))
    with pytest.raises(UnsupportedCapabilityError, match=message):
        await scope.verify(prepared, servers("one"), "session")
    await task
    assert not Path(prepared.env[scope.CONTROL_ENV]).exists()


@pytest.mark.parametrize("update", [{"nonce": "old-receipt"}, {"sessionId": "other-session"},
    {"states": {}}, {"states": {"one": ["connected"]}}, {"states": {"one": "connected", "extra": "connected"}}])
async def test_stale_other_session_missing_or_malformed_receipt_fails_closed(tmp_path, update):
    prepared = await scope.prepare(spec_for(tmp_path, "OPENCODE"), servers("one"), prefix="OPENCODE")
    task = asyncio.create_task(reply(prepared, lambda result: result.update(update)))
    with pytest.raises(UnsupportedCapabilityError):
        await scope.verify(prepared, servers("one"), "session")
    await task


async def test_no_native_receipt_times_out_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, "STATUS_TIMEOUT", 0.05)
    prepared = await scope.prepare(spec_for(tmp_path, "OPENCODE"), servers("one"), prefix="OPENCODE")
    with pytest.raises(UnsupportedCapabilityError, match="校验超时"):
        await scope.verify(prepared, servers("one"), "session")
    assert not Path(prepared.env[scope.CONTROL_ENV]).exists()


async def test_duplicate_names_are_rejected_before_launch(tmp_path):
    with pytest.raises(UnsupportedCapabilityError, match="名称重复"):
        await scope.prepare(spec_for(tmp_path, "OPENCODE"), servers("same", "same"), prefix="OPENCODE")
    assert not (tmp_path / "state").exists()
