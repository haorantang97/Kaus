import copy
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec, AcpRpcError
from drivers.base import UnsupportedCapabilityError
from drivers.openclaw import computer_use as cua


def spec(*args):
    return AcpAgentSpec(command=("fixture", "acp", *args), inherit_env=False, env={})


def native_config():
    return {"agents": {"ownership": "explicit", "entries": {
        "source": {"default": True, "workspace": "/native",
                   "tools": {"profile": "coding", "alsoAllow": ["browser", "computer"]}}
    }}, "browser": {"enabled": True}}


def inventory(*names):
    return {"groups": [{"tools": [{"id": name} for name in names]}]}


class Gateway:
    responses = {}
    calls = []
    closed = False

    def __init__(self, *_args, **_kwargs):
        pass

    async def call(self, method, params):
        self.calls.append((method, params))
        value = self.responses.get(method, AcpRpcError(-32601, "synthetic absent method"))
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    async def close(self):
        type(self).closed = True

    async def session_key(self, session_id, *, require_existing=False):
        result = await self.call("sessions.resolve", {"key":session_id,"allowMissing":not require_existing})
        if result.get("ok") is True and isinstance(result.get("key"), str):
            return result["key"]
        raise AcpRpcError(-32000, "synthetic session unavailable")


@pytest.fixture
def gateway(monkeypatch):
    Gateway.responses = {"config.get": {"config": native_config()},
                         "tools.effective": inventory("browser", "computer"),
                         "browser.request": {"enabled": True, "running": True, "cdpReady": True},
                         "computer.status": {"configured": False, "available": False},
                         "node.list": {"nodes": [{"connected": True,
                                                  "commands": ["computer.act", "screen.snapshot"]}]}}
    Gateway.calls = []
    Gateway.closed = False
    monkeypatch.setattr(cua, "ReadinessGateway", Gateway)
    return Gateway


def test_copy_policy_excludes_executor_credentials_and_source_scope():
    config = native_config()
    source = config["agents"]["entries"]["source"]
    source.update(agentDir="/native/private", skills=["private"], env={"SECRET": "secret"})
    source["tools"].update(github={"token": "secret"}, exec={"host": "node"},
                          byProvider={"fixture/model": {"deny": ["computer"]}},
                          toolsBySender={"person@example.test": {"deny": ["browser"]}},
                          sandbox={"tools": {"alsoAllow": ["computer"]}})
    original = copy.deepcopy(config)
    actual = cua.inherited_agent_policy(config, spec())
    assert set(actual) == {"tools"}
    assert set(actual["tools"]) == {"profile", "alsoAllow", "byProvider", "toolsBySender", "sandbox"}
    assert actual["tools"]["sandbox"]["tools"]["alsoAllow"] == ["computer"]
    assert config == original and "secret" not in str(actual)


def test_source_uses_explicit_route_and_never_first_arbitrary_entry():
    config = native_config()
    config["agents"]["entries"]["source"].pop("default")
    config["agents"]["entries"]["other"] = {"tools": {"deny": ["computer"]}}
    assert cua.source_agent_id(config, spec()) is None
    assert cua.inherited_agent_policy(config, spec()) == {}
    assert cua.source_agent_id(config, spec("--session", "agent:other:private")) == "other"
    binding = SimpleNamespace(runtime_config={"native_agent_id": "source"})
    assert cua.source_agent_id(config, spec("--session", "agent:other:private"), binding) == "source"
    config["agents"]["defaults"] = {"systemAgent": {"agentId": "other"}}
    assert cua.source_agent_id(config, spec()) == "other"


def test_redacted_or_unknown_policy_is_not_silently_dropped():
    with pytest.raises(UnsupportedCapabilityError):
        cua.tool_policy({"deny": ["__OPENCLAW_REDACTED__"]})
    with pytest.raises(UnsupportedCapabilityError):
        cua.tool_policy({"profile": "future-policy"})
    with pytest.raises(UnsupportedCapabilityError):
        cua.tool_policy({"profile": []})
    with pytest.raises(UnsupportedCapabilityError):
        cua.tool_policy({"deny": "computer"})


async def test_ready_requires_exposed_tools_and_live_services(gateway):
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == actual["desktop"]["status"] == "ready"
    assert actual["desktop"]["enabled"] is True
    assert actual["desktop"]["resource"] == "desktop:local"
    assert gateway.closed
    assert set(name for name, _ in gateway.calls) == {
        "config.get", "tools.effective", "browser.request", "computer.status", "node.list"}
    assert ("browser.request", {"method": "GET", "path": "/"}) in gateway.calls


async def test_native_explicit_disable_and_denial_prevent_service_probe(gateway):
    config = native_config()
    config["browser"]["enabled"] = False
    config["agents"]["entries"]["source"]["tools"]["deny"] = ["computer"]
    gateway.responses["config.get"] = {"config": config}
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == "disabled"
    assert actual["desktop"]["status"] == "disabled"
    assert actual["desktop"]["enabled"] is False
    assert not {"browser.request", "computer.status", "node.list"} & {name for name, _ in gateway.calls}


async def test_native_ui_group_policy_does_not_disable_computer(gateway):
    config = native_config()
    config["tools"] = {"deny": ["group:ui"]}
    gateway.responses["config.get"] = {"config": config}
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == "disabled"
    assert actual["desktop"]["status"] == "ready"


async def test_configuration_alone_is_not_ready_but_keeps_lease_requirement(gateway):
    gateway.responses["tools.effective"] = AcpRpcError(-32601, "synthetic absent method")
    gateway.responses["computer.status"] = AcpRpcError(-32601, "synthetic absent method")
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == actual["desktop"]["status"] == "unknown"
    assert actual["desktop"]["enabled"] is True
    assert actual["desktop"]["resource"] == "desktop:local"


async def test_unavailable_configured_gateway_never_falls_back_to_connected_node(gateway):
    gateway.responses["computer.status"] = {"configured": True, "available": False}
    actual = await cua.probe_native(spec())
    assert actual["desktop"]["status"] == "unavailable"
    assert actual["desktop"]["requirements"] == ["native-computer-provider"]


async def test_disconnected_or_observe_only_node_is_not_ready(gateway):
    gateway.responses["node.list"] = {"nodes": [
        {"connected": False, "commands": ["computer.act", "screen.snapshot"]},
        {"connected": True, "commands": ["screen.snapshot"]},
    ]}
    actual = await cua.probe_native(spec())
    assert actual["desktop"]["status"] == "unavailable"


async def test_catalog_absence_does_not_claim_tool_ready(gateway):
    gateway.responses["tools.effective"] = inventory("browser")
    actual = await cua.probe_native(spec())
    assert actual["desktop"]["status"] == "unknown"
    assert actual["desktop"]["requirements"] == ["native-tool-inventory"]


async def test_stopped_browser_is_reported_without_starting_it(gateway):
    gateway.responses["browser.request"] = {"enabled": True, "running": False, "cdpReady": False}
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == "unavailable"
    assert actual["browser"]["requirements"] == ["native-browser-start"]


async def test_gateway_failure_is_sanitized_and_closed(gateway):
    gateway.responses["config.get"] = AcpRpcError(-32000, "/private/config?secret=fixture")
    actual = await cua.probe_native(spec())
    assert actual["browser"]["status"] == actual["desktop"]["status"] == "unknown"
    assert actual["browser"]["exposed"] and actual["desktop"]["exposed"]
    assert "secret" not in str(actual) and gateway.closed


async def test_unknown_inventory_without_explicit_grant_remains_exposed(gateway):
    config=native_config()
    config["agents"]["entries"]["source"].pop("tools")
    gateway.responses["config.get"]={"config":config}
    gateway.responses["tools.effective"]=AcpRpcError(-32601,"synthetic absent method")
    actual=await cua.probe_native(spec())
    assert actual["browser"]["enabled"] is actual["desktop"]["enabled"] is None
    assert all(actual[cap]["status"]=="unknown" and actual[cap]["exposed"] for cap in ("browser","desktop"))


async def test_readiness_gateway_rejects_browser_mutations():
    gateway = cua.ReadinessGateway(spec())
    with pytest.raises(ValueError, match="Only browser status"):
        await gateway.call("browser.request", {"method": "POST", "path": "/start"})
    with pytest.raises(ValueError, match="Only browser status"):
        await gateway.call("browser.request", {"method": "GET", "path": "/cookies"})


OWNED = "agent:kaus-" + "a" * 24 + ":kaus"


@pytest.mark.parametrize("status,expected", [
    ("running", False), ("queued", False), ("killed", True), ("done", True), (None, False),
])
async def test_stop_requires_gateway_terminal_status(gateway, monkeypatch, status, expected):
    monkeypatch.setattr(cua, "StopGateway", Gateway)
    gateway.responses["sessions.abort"] = {"ok": True, "status": "aborted"}
    gateway.responses["sessions.describe"] = {"session": {"key": OWNED, "status": status}}
    assert await cua.stop_computer_use(spec(), "acp-uuid", {"_meta": {"sessionKey": OWNED}}) is expected
    assert gateway.calls == [
        ("sessions.abort", {"key": OWNED, "clearQueued": True}),
        ("sessions.describe", {"key": OWNED}),
    ]
    assert gateway.closed


async def test_stop_never_touches_unowned_source_session(gateway, monkeypatch):
    monkeypatch.setattr(cua, "StopGateway", Gateway)
    assert not await cua.stop_computer_use(spec(), "agent:source:main")
    assert not await cua.stop_computer_use(spec(), "acp-uuid")
    assert all(method == 'sessions.resolve' for method,_ in gateway.calls)


@pytest.mark.parametrize("status,expected",[("done",True),("running",False)])
async def test_old_session_only_checks_completion_never_aborts(gateway, monkeypatch, status, expected):
    monkeypatch.setattr(cua,"StopGateway",Gateway)
    key="agent:source:original"
    gateway.responses["sessions.resolve"]={"ok":True,"key":key}
    gateway.responses["sessions.describe"]={"session":{"key":key,"status":status}}
    assert await cua.stop_computer_use(spec(),"acp-uuid") is expected
    assert [name for name,_ in gateway.calls]==["sessions.resolve","sessions.describe"]


async def test_terminal_parent_with_active_descendants_does_not_release(gateway, monkeypatch):
    monkeypatch.setattr(cua, "StopGateway", Gateway)
    gateway.responses["sessions.abort"] = {"ok": True}
    gateway.responses["sessions.describe"] = {"session": {
        "key": OWNED, "status": "killed",
        "swarm": {"groups": [{"running": 1, "queued": 0}]},
    }}
    assert not await cua.stop_computer_use(spec(), OWNED)
    gateway.responses["sessions.describe"]["session"] = {
        "key": OWNED, "status": "killed", "childSessions": ["agent:child:task"],
    }
    # An unverified or inaccessible child cannot count as stopped.
    assert not await cua.stop_computer_use(spec(), OWNED)


@pytest.mark.parametrize("child_status,expected", [("running", False), ("done", True), (None, False)])
async def test_success_completion_only_observes_owned_tree_without_aborting(gateway, monkeypatch, child_status, expected):
    child = "agent:child:task"

    class TreeGateway(Gateway):
        async def call(self, method, params):
            self.calls.append((method, params))
            assert method == "sessions.describe"
            key = params["key"]
            return {"session": {"key": key, "status": "done" if key == OWNED else child_status,
                                "childSessions": [child] if key == OWNED else []}}

    monkeypatch.setattr(cua, "CompletionGateway", TreeGateway)
    assert await cua.confirm_computer_use_complete(spec(), "bridge-id", {"_meta": {"sessionKey": OWNED}}) is expected
    assert gateway.calls == [("sessions.describe", {"key": OWNED}), ("sessions.describe", {"key": child})]


async def test_completion_gateway_cannot_abort():
    with pytest.raises(ValueError, match="Unsupported Gateway"):
        await cua.CompletionGateway(spec()).call("sessions.abort", {"key": OWNED, "clearQueued": True})


async def test_recovery_requires_this_turn_not_previous_terminal_snapshot(gateway, monkeypatch):
    monkeypatch.setattr(cua, "CompletionGateway", Gateway)
    gateway.responses["sessions.describe"] = {"session": {"key": OWNED, "status": "done", "lastRunId": "previous-run"}}
    native = spec()
    reference = await cua.computer_use_reference(native, "bridge-id", {"_meta": {"sessionKey": OWNED}})
    assert reference["baselineKnown"] is True and reference["previousRunId"] == "previous-run"
    assert not await cua.recover_computer_use(native, reference)
    gateway.responses["sessions.describe"]["session"].update(status="running", lastRunId="current-run")
    assert not await cua.recover_computer_use(native, reference)
    gateway.responses["sessions.describe"]["session"].update(status="done")
    assert await cua.recover_computer_use(native, reference)
    assert set(method for method, _ in gateway.calls) == {"sessions.describe"}


async def test_recovery_does_not_copy_auth_or_use_a_different_connection(gateway, monkeypatch):
    monkeypatch.setattr(cua, "CompletionGateway", Gateway)
    native = spec().with_env(OPENCLAW_GATEWAY_TOKEN="private-token")
    gateway.responses["sessions.describe"] = {"session": {"key": OWNED, "status": "done", "lastRunId": "old"}}
    reference = await cua.computer_use_reference(native, "bridge-id", {
        "_meta": {"sessionKey": OWNED}, "_kausOpenClawOwnership": {"receipt": "private-receipt"}})
    assert "private-token" not in json.dumps(reference) and "private-receipt" not in json.dumps(reference)
    gateway.calls.clear()
    assert not await cua.recover_computer_use(spec(), reference)
    assert gateway.calls == []


async def test_unknown_dispatch_baseline_is_not_guessed_from_old_done(gateway, monkeypatch):
    monkeypatch.setattr(cua, "CompletionGateway", Gateway)
    gateway.responses["sessions.describe"] = AcpRpcError(-32000, "offline")
    reference = await cua.computer_use_reference(spec(), "bridge-id", {"_meta": {"sessionKey": OWNED}})
    assert reference["baselineKnown"] is False
    gateway.responses["sessions.describe"] = {"session": {"key": OWNED, "status": "done", "lastRunId": "old"}}
    assert not await cua.recover_computer_use(spec(), reference)


async def test_success_completion_holds_active_descendant_even_when_child_list_is_omitted(gateway, monkeypatch):
    monkeypatch.setattr(cua, "CompletionGateway", Gateway)
    gateway.responses["sessions.describe"] = {"session": {
        "key": OWNED, "status": "done", "hasActiveSubagentRun": True}}
    assert not await cua.confirm_computer_use_complete(spec(), "bridge-id", {"_meta": {"sessionKey": OWNED}})


async def test_stop_gateway_rejects_broad_or_injected_cancellation():
    gateway = cua.StopGateway(spec())
    with pytest.raises(ValueError, match="owned Kaus"):
        await gateway.call("sessions.abort", {"key": "global", "clearQueued": True})
    with pytest.raises(ValueError, match="owned Kaus"):
        await gateway.call("sessions.abort", {"key": OWNED, "clearQueued": True, "agentId": "source"})


def test_native_sidecar_only_forwards_status_and_owned_stop(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    native = tmp_path / "fixture.mjs"
    native.write_text("export async function callGatewayFromCliRuntime(method,options,params) "
                      "{ return { method, params }; }\n")
    calls = [
        ("browser.request", {"method": "GET", "path": "/"}),
        ("browser.request", {"method": "POST", "path": "/start"}),
        ("browser.request", {"method": "GET", "path": "/cookies"}),
        ("sessions.abort", {"key": OWNED, "clearQueued": True}),
        ("sessions.abort", {"key": "agent:source:main", "clearQueued": True}),
        ("computer.act", {"action": "click"}),
    ]
    requests = "\n".join(json.dumps({"id": index, "method": method,
        "params": {"arguments": params, "timeout": 1}})
        for index, (method, params) in enumerate(calls)) + "\n"
    result = subprocess.run([node, str(Path(cua.__file__).with_name("gateway_sidecar.mjs")),
                             str(native)], input=requests, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["id"] for row in rows if "result" in row] == [0, 3]
    assert all("error" in rows[index] for index in (1, 2, 4, 5))
