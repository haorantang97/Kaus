"""Projection acceptance with synthetic providers; no model or desktop access."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from drivers.computer_use import Candidate
from drivers.computer_use import integration, probe
from drivers.base import UnsupportedCapabilityError


class Effective:
    def __init__(self, servers=(), controls=None, blocked=()):
        self.entries = [
            SimpleNamespace(capability_type="mcp", capability_id=server["name"],
                            config={key: value for key, value in server.items() if key != "name"})
            for server in servers
        ]
        if controls is not None:
            self.entries.append(SimpleNamespace(
                capability_type="policies", capability_id="computer_use",
                config={"value": controls}))
        self.blocked = [
            SimpleNamespace(capability_type=kind, capability_id=name)
            for kind, name in blocked
        ]

    def get(self, kind, name):
        return next((entry for entry in self.entries
                     if entry.capability_type == kind and entry.capability_id == name), None)

    def is_blocked(self, kind, name):
        return any(entry.capability_type == kind and entry.capability_id == name
                   for entry in self.blocked)


def server(name="browser", capability="browser", *, mixed=False):
    return {
        "name": name, "command": "custom-mcp",
        "kausComputerUse": {"capabilities": [capability], "exclusive": not mixed},
    }


def fallback(capability, *, name=None, status="ready"):
    name = name or f"fallback-{capability}"
    return Candidate(
        name, capability, "fallback", status=status,
        mcp_server={"name": name, "command": "installed-mcp"},
        resource_id="desktop:local" if capability == "desktop" else "browser:context:test",
    )


@pytest.fixture
def providers(monkeypatch):
    state = SimpleNamespace(native={}, ambient=(), fallbacks=(), calls=[])

    async def native(*args, **kwargs):
        return deepcopy(state.native)

    async def fallbacks(**kwargs):
        state.calls.append(kwargs)
        allowed = kwargs.get("capabilities", ("browser", "desktop"))
        return tuple(value for value in state.fallbacks if value.capability in allowed)

    monkeypatch.setattr(integration, "native_status", native)
    monkeypatch.setattr(integration, "ambient_mcp", lambda *args: state.ambient)
    monkeypatch.setattr(probe, "probe_fallbacks", fallbacks)
    return state


async def prepare(effective, metadata=None, *, supports_mcp=True):
    return await integration.prepare_projection(
        SimpleNamespace(), SimpleNamespace(), None, SimpleNamespace(id="test"),
        effective, metadata or {}, supports_mcp=supports_mcp)


async def test_native_browser_replaces_only_equivalent_project_tools(providers):
    providers.native = {"browser": {"status": "ready", "provider": "engine", "exposed": True}}
    web = server()
    database = {"name": "database", "command": "db-mcp", "env": [{"name": "KEY", "value": "private"}]}
    metadata = {
        "mcpServers": [web, database], "_meta": {"skills": ["project-skill"]},
        "_kausProjectSkills": {"names": ["project-skill"]},
    }
    before = deepcopy(metadata)
    result = await prepare(Effective([web, database]), metadata)
    assert result["mcpServers"] == [database]
    assert result["_meta"] == metadata["_meta"]
    assert result["_kausProjectSkills"] == metadata["_kausProjectSkills"]
    assert result["_kausComputerUse"]["browser"]["source"] == "native"
    assert result["_kausComputerUse"]["resources"] == ["browser:shared", "desktop:local"]
    assert "private" not in str(result["_kausComputerUse"])
    assert metadata == before


async def test_project_mcp_wins_over_fallback_without_duplicate_mount(providers):
    web = server()
    providers.fallbacks = (fallback("browser"), fallback("desktop"))
    result = await prepare(Effective([web]), {"mcpServers": [web]})
    assert [value["name"] for value in result["mcpServers"]] == ["browser", "fallback-desktop"]
    assert result["_kausComputerUse"]["browser"]["source"] == "project"
    assert result["_kausComputerUse"]["desktop"]["source"] == "fallback"
    assert providers.calls == [{"context_id": "test", "capabilities": ("desktop",)}]


async def test_isolated_browser_does_not_lock_desktop(providers):
    web = {"name": "web", "command": "playwright-mcp", "args": ["--isolated"]}
    result = await prepare(Effective([web]), {"mcpServers": [web]})
    assert result["_kausComputerUse"]["resources"] == ["browser:context:test"]


async def test_one_fallback_with_two_capabilities_is_injected_once(providers):
    providers.fallbacks = (fallback("browser", name="both"), fallback("desktop", name="both"))
    result = await prepare(Effective())
    assert len(result["mcpServers"]) == 1
    assert result["_kausComputerUse"]["resources"] == ["browser:context:test", "desktop:local"]
    assert result["_kausComputerUse"]["autoInjectedServers"] == [{
        "name": "both", "capabilities": ["browser", "desktop"],
        "fingerprint": integration.server_fingerprint(result["mcpServers"][0]),
    }]


@pytest.mark.parametrize('same_declaration', [False, True])
async def test_preexisting_same_name_server_is_never_claimed_as_auto_fallback(providers, same_declaration):
    candidate = fallback('browser')
    providers.fallbacks = (candidate,)
    user = dict(candidate.mcp_server) if same_declaration else {
        'name': candidate.provider_id, 'command': 'user-configured-server'}
    metadata = {'mcpServers': [user], '_kausComputerUse': {
        'autoInjectedServers': [{'name': user['name'], 'capabilities': ['browser'],
                                 'fingerprint': integration.server_fingerprint(user)}]}}
    result = await prepare(Effective(), metadata)
    assert result['mcpServers'] == [user]
    assert result['_kausComputerUse']['autoInjectedServers'] == []


async def test_auto_fallback_receipt_has_exact_scope_without_credentials(providers):
    providers.fallbacks = (fallback('browser'), fallback('desktop'))
    result = await prepare(Effective())
    assert [(item['name'], item['capabilities']) for item in
            result['_kausComputerUse']['autoInjectedServers']] == [
                ('fallback-browser', ['browser']), ('fallback-desktop', ['desktop'])]
    assert all(set(item) == {'name', 'capabilities', 'fingerprint'} for item in
               result['_kausComputerUse']['autoInjectedServers'])


async def test_adapter_can_decline_fallback_without_changing_native_or_project_scope(providers):
    providers.native = {"browser": {"status": "ready", "exposed": True}}
    providers.fallbacks = (fallback("desktop"),)
    metadata = {"_meta": {"sessionKey": "owned-source-session"}}
    adapter = SimpleNamespace(computer_use_fallback_allowed=lambda effective, metadata: False)
    result = await integration.prepare_projection(
        SimpleNamespace(), adapter, None, SimpleNamespace(id="test"),
        Effective(), metadata)
    assert not providers.calls
    assert not result.get("mcpServers")
    assert result["_meta"] == metadata["_meta"]
    assert result["_kausComputerUse"]["browser"]["source"] == "native"


async def test_partial_native_replacement_preserves_both_scope_server(providers):
    both = {"name": "both", "command": "both-mcp",
            "kausComputerUse": {"capabilities": ["browser", "desktop"], "exclusive": True}}
    providers.native = {"browser": {"status": "ready", "exposed": True}}
    result = await prepare(Effective([both]), {"mcpServers": [both]})
    assert result["mcpServers"] == [both]
    assert result["_kausComputerUse"]["desktop"]["source"] == "project"
    assert "desktop:local" in result["_kausComputerUse"]["resources"]


async def test_complete_native_replacement_removes_dedicated_multi_scope_server(providers):
    both = {"name": "both", "command": "both-mcp",
            "kausComputerUse": {"capabilities": ["browser", "desktop"], "exclusive": True}}
    providers.native = {cap: {"status": "ready", "exposed": True}
                        for cap in ("browser", "desktop")}
    result = await prepare(Effective([both]), {"mcpServers": [both]})
    assert result["mcpServers"] == []
    assert result["_kausComputerUse"]["resources"] == ["browser:shared", "desktop:local"]
    assert not providers.calls


async def test_mixed_server_keeps_unrelated_tools_when_native_exists(providers):
    mixed = server("utility", mixed=True)
    providers.native = {"browser": {"status": "ready", "exposed": True}}
    result = await prepare(Effective([mixed]), {"mcpServers": [mixed]})
    assert result["mcpServers"] == [mixed]
    assert result["_kausComputerUse"]["resources"] == ["browser:shared", "desktop:local"]


async def test_project_block_prevents_fallback_without_disabling_native(providers):
    providers.native = {"browser": {"status": "ready", "exposed": True}}
    providers.fallbacks = (fallback("desktop"),)
    result = await prepare(Effective(blocked=[("mcp", "removed-server")]))
    assert not providers.calls
    assert not result.get("mcpServers")
    assert result["_kausComputerUse"]["browser"]["source"] == "native"
    assert result["_kausComputerUse"]["desktop"]["status"] == "unavailable"


async def test_off_removes_dedicated_project_provider_and_does_not_add_fallback(providers):
    web = {"name": "web", "command": "playwright-mcp", "args": ["--isolated"]}
    providers.fallbacks = (fallback("browser"),)
    result = await prepare(Effective([web], controls={"browser": "off"}), {"mcpServers": [web]})
    assert result["mcpServers"] == []
    assert result["_kausComputerUse"]["browser"]["status"] == "disabled"
    assert result["_kausComputerUse"]["resources"] == []


@pytest.mark.parametrize("denial", [{"disabled": True}, {"kausComputerUse": {
    "capabilities": ["browser"], "exclusive": True, "denied": True,
}}])
async def test_explicit_provider_refusal_does_not_add_alternative(providers, denial):
    web = {"name": "web", "command": "playwright-mcp", **denial}
    providers.fallbacks = (fallback("browser"),)
    result = await prepare(Effective([web]), {"mcpServers": [web]})
    assert result["mcpServers"] == []
    assert result["_kausComputerUse"]["browser"]["status"] in {"disabled", "denied"}


async def test_enabled_unknown_native_remains_exposed_and_locked(providers):
    providers.native = {"desktop": {"status": "unknown", "enabled": True, "provider": "openclaw"}}
    providers.fallbacks = (fallback("desktop"),)
    result = await prepare(Effective())
    assert not result.get("mcpServers")
    assert result["_kausComputerUse"]["desktop"]["provider"] == "openclaw"
    assert result["_kausComputerUse"]["desktop"]["status"] == "unknown"
    assert result["_kausComputerUse"]["resources"] == ["desktop:local"]


async def test_missing_setup_is_not_mounted_or_locked(providers):
    providers.fallbacks = (fallback("desktop", status="needs_setup"),)
    result = await prepare(Effective())
    assert not result.get("mcpServers")
    assert result["_kausComputerUse"]["desktop"]["status"] == "needs_setup"
    assert result["_kausComputerUse"]["resources"] == []


async def test_engine_without_mcp_support_does_not_probe_or_inject(providers):
    providers.fallbacks = (fallback("desktop"),)
    result = await prepare(Effective(), supports_mcp=False)
    assert not providers.calls
    assert not result.get("mcpServers")


async def test_context_only_does_not_discover_or_add_control_tools(providers):
    providers.native = {"desktop": {"status": "ready", "exposed": True}}
    providers.fallbacks = (fallback("desktop"),)
    metadata = {"_kausExecutionPolicy": "context-only", "_kausComputerUse": {"resources": ["desktop:local"]}}
    result = await prepare(Effective(), metadata)
    assert result == {"_kausExecutionPolicy": "context-only"}
    assert not providers.calls


async def test_quarantined_remote_resources_wait_for_confirmed_stop(monkeypatch):
    released = []
    outcomes = iter([False, True])
    lease = SimpleNamespace(resource_id="desktop:local", release=lambda: released.append(True))

    async def confirm(spec, session, metadata):
        assert session == "owned-session"
        assert metadata == {"owner": "runtime"}
        return next(outcomes)

    monkeypatch.setattr(integration, "_quarantined", {})
    integration.quarantine([lease], confirm, object(), "owned-session", {"owner": "runtime"})
    assert await integration.reconcile_resources() == ("desktop:local",)
    assert not released
    assert await integration.reconcile_resources() == ()
    assert released == [True]


async def test_remote_status_failure_retains_quarantined_lease(monkeypatch):
    released = []
    lease = SimpleNamespace(resource_id="desktop:local", release=lambda: released.append(True))

    async def confirm(*args):
        raise OSError("remote gateway unavailable")

    monkeypatch.setattr(integration, "_quarantined", {})
    integration.quarantine([lease], confirm, None, "owned-session", {})
    assert await integration.reconcile_resources() == ("desktop:local",)
    assert not released


async def test_reconciliation_snapshot_cannot_release_new_resource_owner(monkeypatch):
    released = []
    old = SimpleNamespace(resource_id="desktop:local", release=lambda: released.append("old"))
    new = SimpleNamespace(resource_id="desktop:local", release=lambda: released.append("new"))

    async def confirm(*args):
        integration.release_quarantine([old])
        integration.quarantine([new], confirm, None, "new-session", {})
        return True

    monkeypatch.setattr(integration, "_quarantined", {})
    integration.quarantine([old], confirm, None, "old-session", {})
    assert await integration.reconcile_resources() == ("desktop:local",)
    assert integration._quarantined["desktop:local"][0] is new
    assert released == ["old"]


async def test_mixed_mcp_cannot_silently_bypass_off(providers):
    mixed = server("utility", mixed=True)
    with pytest.raises(UnsupportedCapabilityError, match="MCP"):
        await prepare(Effective([mixed], controls={"browser": "off"}), {"mcpServers": [mixed]})


async def test_native_off_requires_enforceable_driver_policy(providers):
    providers.native = {"desktop": {"status": "ready", "exposed": True}}
    with pytest.raises(UnsupportedCapabilityError, match="项目权限"):
        await prepare(Effective(controls={"desktop": "off"}))
    result = await integration.prepare_projection(
        SimpleNamespace(), SimpleNamespace(COMPUTER_USE_POLICY_SCOPES={"desktop"}),
        None, SimpleNamespace(id="test"), Effective(controls={"desktop": "off"}), {})
    assert result["_kausComputerUse"]["controls"]["desktop"] is False
    assert result["_kausComputerUse"]["resources"] == []


async def test_partial_disabled_server_does_not_report_removed_tool_as_ready(providers):
    both = {"name": "both", "command": "both-mcp",
            "kausComputerUse": {"capabilities": ["browser", "desktop"], "exclusive": True}}
    with pytest.raises(UnsupportedCapabilityError, match="分别关闭"):
        await prepare(Effective([both], controls={"desktop": "off"}), {"mcpServers": [both]})


@pytest.mark.parametrize("result", ["error", "empty"])
async def test_incomplete_native_probe_keeps_control_resource_without_fallback(monkeypatch, result):
    async def native(*args, **kwargs):
        if result == "error":
            raise OSError("synthetic native service unavailable")
        return {}

    async def fallback_probe(**kwargs):
        raise AssertionError("Incomplete native probe must not launch a second controller")

    monkeypatch.setattr(integration, "_native_cache", {})
    monkeypatch.setattr(integration, "ambient_mcp", lambda *args: ())
    monkeypatch.setattr(probe, "probe_fallbacks", fallback_probe)
    metadata = await integration.prepare_projection(
        SimpleNamespace(command=("fixture",), cwd=None, env={}),
        SimpleNamespace(__name__="fixture", probe_computer_use=native),
        None, SimpleNamespace(id="native-incomplete"), Effective(), {})
    assert not metadata.get("mcpServers")
    plan = metadata["_kausComputerUse"]
    assert plan["browser"]["status"] == plan["desktop"]["status"] == "unknown"
    assert plan["resources"] == ["browser:shared", "desktop:local"]
