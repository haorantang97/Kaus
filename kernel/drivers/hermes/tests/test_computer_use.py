import asyncio
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.hermes import computer_use, computer_use_probe, computer_use_runtime as runtime, skill_adapter


def decision(status="ready", source="native"):
    return {"_kausComputerUse": {"desktop": {"status": status, "source": source}}}


@pytest.mark.parametrize("persona", [None, "writer"])
async def test_isolated_project_keeps_native_browser_binary_discoverable(tmp_path, monkeypatch, persona):
    import shutil
    from app.projects.models import AgentBinding
    from drivers.base import ProjectionResult

    native = tmp_path / "native"
    (native / "bin").mkdir(parents=True)
    browser = native / "bin/browser-use"
    browser.write_text("#!/bin/sh\nexit 0\n")
    browser.chmod(0o700)
    source = native / "profiles" / persona if persona else native
    source.mkdir(parents=True, exist_ok=True)
    (source / "config.yaml").write_text("browser:\n  backend: browser-use\n")
    config_before = (source / "config.yaml").read_bytes()
    binding = AgentBinding.create(project="project:demo", backend="backend:hermes", native_scope_ref=persona)
    spec = AcpAgentSpec(command=("hermes", "acp"), env={"HERMES_HOME": str(native), "PATH": "/usr/bin:/bin", "KEEP": "yes"})
    monkeypatch.setattr(skill_adapter.skill_projection, "project_skills", lambda **kw: ProjectionResult(binding_id=binding.id))
    monkeypatch.setattr(skill_adapter, "command_for", lambda *args: spec.command)
    monkeypatch.setattr(skill_adapter, "guarded_spec", lambda selected, names: selected)

    plan = await skill_adapter.project(spec, binding, None, tmp_path)

    assert plan.spec.env["HERMES_HOME"] != str(source)
    assert shutil.which("browser-use", path=plan.spec.env["PATH"]) == str(browser)
    assert plan.spec.env["KEEP"] == "yes"
    assert spec.env["PATH"] == "/usr/bin:/bin"
    assert (source / "config.yaml").read_bytes() == config_before
    assert not (plan.root / "bin").exists()


def test_managed_binary_path_has_no_duplicates_or_cwd_search(tmp_path):
    (tmp_path / "bin").mkdir()
    assert skill_adapter.native_binary_path("", tmp_path, tmp_path) == str(tmp_path / "bin")


def test_native_control_is_opt_in_and_explicit_unselection_wins(monkeypatch):
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    spec = AcpAgentSpec(command=("hermes", "acp"), env={"PRESERVE": "yes"})
    assert computer_use.prepare_spec(spec, {}) is spec
    enabled = computer_use.prepare_spec(spec, decision())
    assert enabled.command[3:] == ("native", "acp")
    assert enabled.env["PRESERVE"] == "yes"
    assert enabled.env[computer_use.DESKTOP_ENV] == "1"
    assert computer_use.prepare_spec(enabled, decision()).command == enabled.command
    assert computer_use.prepare_spec(enabled, decision("disabled")).env[computer_use.DESKTOP_ENV] == "0"
    assert computer_use.prepare_spec(enabled, {"_kausExecutionPolicy": "context-only"}).env[computer_use.DESKTOP_ENV] == "0"


@pytest.mark.parametrize("launcher", ["skill_launcher.py", "group_launcher.py"])
async def test_native_control_preserves_skill_group_and_project_mcp(launcher):
    path = str(Path(computer_use.__file__).with_name(launcher))
    spec = AcpAgentSpec(command=("/native/python", "-B", path, "planning"),
                        env={"HERMES_HOME": "/private/project", "HERMES_ACP_SKIP_CONFIGURED_MCP": "1"})
    metadata = {**decision(), "mcpServers": [{"name": "existing-project-service"}]}
    prepared = await skill_adapter.prepare_runtime_spec(spec, None, metadata)
    assert prepared.command[3:] == (path, "planning")
    assert prepared.env["HERMES_HOME"] == "/private/project"
    assert json.loads(prepared.env["KAUS_HERMES_MCP_NAMES"]) == ["existing-project-service"]
    again = await skill_adapter.prepare_runtime_spec(prepared, None, metadata)
    assert again.command == prepared.command


def test_unknown_launchers_are_not_silently_replaced():
    with pytest.raises(UnsupportedCapabilityError, match="启动器"):
        computer_use.prepare_spec(AcpAgentSpec(command=("custom-hermes", "acp")), decision())


@pytest.mark.parametrize("capability", ["browser", "desktop"])
def test_project_off_adds_enforced_child_policy_even_without_native_activation(monkeypatch, capability):
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    spec = AcpAgentSpec(command=("hermes", "acp"))
    prepared = computer_use.prepare_spec(spec, {
        "_kausComputerUse": {"controls": {capability: False},
                            capability: {"status": "disabled"}}})
    assert prepared.command[3:] == ("native", "acp")
    assert prepared.env[f"KAUS_COMPUTER_USE_{capability.upper()}"] == "0"
    assert capability in skill_adapter.COMPUTER_USE_POLICY_SCOPES


def test_context_only_suppresses_both_native_surfaces(monkeypatch):
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    prepared = computer_use.prepare_spec(AcpAgentSpec(command=("hermes", "acp")),
                                         {"_kausExecutionPolicy": "context-only"})
    assert prepared.env[computer_use.BROWSER_ENV] == "0"
    assert prepared.env[computer_use.DESKTOP_ENV] == "0"
    assert prepared.command[3:] == ("native", "acp")


@pytest.mark.parametrize("source", ["project", "fallback"])
def test_selected_non_native_browser_excludes_native_tools_and_can_return_to_native(monkeypatch, source):
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    original = AcpAgentSpec(command=("hermes", "acp"), env={"KEEP": "same"})
    prepared = computer_use.prepare_spec(original, {"_kausComputerUse": {
        "browser": {"source": source, "status": "ready"}, "controls": {"browser": True}}})
    assert prepared.env[computer_use.BROWSER_ENV] == "0"
    assert prepared.env["KEEP"] == "same"
    restored = computer_use.prepare_spec(prepared, {"_kausComputerUse": {
        "browser": {"source": "native", "status": "ready"}}})
    assert restored.env[computer_use.BROWSER_ENV] == "1"
    assert restored.command == prepared.command


def test_unknown_native_probe_keeps_browser_resource_owned_without_claiming_ready():
    from drivers.computer_use.policy import from_native, Candidate, select_provider
    candidates = from_native(computer_use._unavailable("native_probe_incomplete"))
    fallback = Candidate("installed-browser", "browser", "fallback", "ready")
    choice = select_provider("browser", (*candidates, fallback))
    assert choice.status == "unknown"
    assert choice.candidate.source == "native"
    assert choice.candidate.exposed is True


def test_off_filters_native_construction_late_refresh_and_dispatch_without_cua_dependency(monkeypatch):
    monkeypatch.setenv(computer_use.BROWSER_ENV, "0")
    monkeypatch.setenv(computer_use.DESKTOP_ENV, "0")
    calls = []
    class Base:
        def __init__(self, **kwargs):
            self.settings = kwargs
    def definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=True):
        calls.append(("definitions", disabled_toolsets))
        return [{"function": {"name": name}} for name in ("browser_exec", "browser_click", "computer_use", "read_file", "mcp_other")]
    def dispatch(function_name, function_args, disabled_toolsets=None):
        calls.append(("dispatch", function_name, disabled_toolsets))
        return "executed"
    model_tools = SimpleNamespace(get_tool_definitions=definitions, handle_function_call=dispatch)
    run_agent = SimpleNamespace(AIAgent=Base)
    toolsets = {"browser": ["browser_click", "browser_exec"], "computer_use": ["computer_use"]}
    monkeypatch.setitem(sys.modules, "model_tools", model_tools)
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)
    monkeypatch.setitem(sys.modules, "toolsets", SimpleNamespace(resolve_toolset=lambda name: toolsets[name]))
    # Disabled-only startup must work on Hermes installations with no CU module.
    monkeypatch.setitem(sys.modules, "tools.computer_use", None)
    runtime.install()
    first = run_agent.AIAgent
    runtime.install()
    assert run_agent.AIAgent is first
    assert first(disabled_toolsets=["memory", "session_search"]).settings["disabled_toolsets"] == [
        "memory", "session_search", "browser", "computer_use"]
    # ACP late refresh and tool listing may omit the agent's disabled list.
    available = model_tools.get_tool_definitions(["hermes-acp", "computer_use"], quiet_mode=True)
    assert [row["function"]["name"] for row in available] == ["read_file", "mcp_other"]
    assert calls[-1] == ("definitions", ["browser", "computer_use"])
    assert "computer_tool_disabled" in run_agent.handle_function_call("browser_exec", {})
    assert "computer_tool_disabled" in model_tools.handle_function_call("computer_use", {"action": "screenshot"})
    assert not any(item[0] == "dispatch" for item in calls)
    assert run_agent.handle_function_call("read_file", {}, disabled_toolsets=["memory"]) == "executed"
    assert calls[-1] == ("dispatch", "read_file", ["memory", "browser", "computer_use"])


def test_native_dependency_and_user_exclusion_are_not_bypassed():
    original = ["hermes-acp", "mcp-existing"]
    assert runtime.desktop_toolsets(original, ["memory"], {}, available=True) == [*original, "computer_use"]
    assert original == ["hermes-acp", "mcp-existing"]
    for config, disabled, available in [
        ({}, [], False),
        ({}, ["computer_use"], True),
        ({"agent": {"disabled_toolsets": ["computer_use"]}}, [], True),
        ({"platform_toolsets": {"acp": []}}, [], True),
    ]:
        with pytest.raises(RuntimeError, match="未就绪"):
            runtime.desktop_toolsets(original, disabled, config, available=available)


def test_composite_native_toolsets_preserve_browser_policy():
    resolver = lambda name: {"hermes-acp": ["browser_click"], "browser": ["browser_click"],
                             "computer_use": ["computer_use"]}.get(name, [])
    config = {"platform_toolsets": {"acp": ["hermes-acp"]}}
    assert not runtime.disabled_by_config(config, "browser", resolver=resolver)
    assert runtime.disabled_by_config(config, "computer_use", resolver=resolver)


def fake_native(monkeypatch):
    requests = []
    class Base:
        def __init__(self, **kwargs):
            self.settings = kwargs
    tool = SimpleNamespace(_request_approval=lambda *args: None, check_computer_use_requirements=lambda: True)
    class Server:
        def __init__(self, conn, states):
            self._conn = conn
            self.session_manager = SimpleNamespace(get_session=states.get)
        async def prompt(self, prompt, session_id, **kwargs):
            return await asyncio.to_thread(tool._request_approval, "click", {"coordinate": [10, 20]}, session_id)
    acp = SimpleNamespace(update_tool_call=lambda *args, **kwargs: SimpleNamespace(**kwargs))
    schema = SimpleNamespace(PermissionOption=lambda **kwargs: SimpleNamespace(**kwargs))
    run_agent = SimpleNamespace(AIAgent=Base)
    for key, value in {
        "acp": acp, "acp.schema": schema,
        "acp_adapter": SimpleNamespace(), "acp_adapter.server": SimpleNamespace(HermesACPAgent=Server),
        "hermes_cli": SimpleNamespace(), "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: {}),
        "tools": SimpleNamespace(), "tools.computer_use": SimpleNamespace(tool=tool),
        "run_agent": run_agent,
    }.items():
        monkeypatch.setitem(sys.modules, key, value)
    monkeypatch.setenv(computer_use.DESKTOP_ENV, "1")
    return Server, tool, run_agent, requests


async def test_session_scoped_permissions_deny_without_owner_and_never_leak(monkeypatch):
    Server, tool, run_agent, requests = fake_native(monkeypatch)
    async def request_permission(**request):
        requests.append(request)
        await asyncio.sleep(.01)
        return SimpleNamespace(outcome=SimpleNamespace(
            outcome="selected", option_id="allow_once" if request["session_id"] == "one" else "deny"))
    states = {name: SimpleNamespace(cancel_event=threading.Event()) for name in ("one", "two")}
    server = Server(SimpleNamespace(request_permission=request_permission), states)
    runtime.install()
    first_prompt = Server.prompt
    runtime.install()
    assert Server.prompt is first_prompt
    agent = run_agent.AIAgent(enabled_toolsets=["hermes-acp"], disabled_toolsets=["memory", "session_search"])
    assert agent.settings["disabled_toolsets"] == ["memory", "session_search"]
    assert agent.settings["enabled_toolsets"] == ["hermes-acp", "computer_use"]
    assert "computer_permission_context_missing" in tool._request_approval("click", {}, "one")
    results = await asyncio.gather(server.prompt([], "one"), server.prompt([], "two"))
    assert results[0] is None
    assert "computer_control_denied" in results[1]
    assert {item["session_id"] for item in requests} == {"one", "two"}
    assert all({option.option_id for option in item["options"]} == {"allow_once", "deny"} for item in requests)
    assert "computer_permission_context_missing" in tool._request_approval("click", {}, "one")


async def test_cancel_pending_permission_prevents_late_execution(monkeypatch):
    Server, tool, _, _ = fake_native(monkeypatch)
    started = asyncio.Event()
    request_cancelled = asyncio.Event()
    state = SimpleNamespace(cancel_event=threading.Event())
    async def request_permission(**request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()
    server = Server(SimpleNamespace(request_permission=request_permission), {"one": state})
    runtime.install()
    pending = asyncio.create_task(server.prompt([], "one"))
    await asyncio.wait_for(started.wait(), 1)
    state.cancel_event.set()
    assert "computer_control_cancelled" in await asyncio.wait_for(pending, 1)
    await asyncio.wait_for(request_cancelled.wait(), 1)


async def test_closed_connection_and_cancelled_outcome_deny(monkeypatch):
    Server, tool, _, _ = fake_native(monkeypatch)
    state = SimpleNamespace(cancel_event=threading.Event())
    runtime.install()
    assert "computer_control_denied" in await Server(None, {"one": state}).prompt([], "one")
    conn = SimpleNamespace(request_permission=AsyncMock(return_value=SimpleNamespace(
        outcome=SimpleNamespace(outcome="cancelled"))))
    assert "computer_control_denied" in await Server(conn, {"one": state}).prompt([], "one")


async def test_native_session_rotation_keeps_stable_acp_approval_owner(monkeypatch):
    Server, tool, _, requests = fake_native(monkeypatch)
    state = SimpleNamespace(cancel_event=threading.Event(), agent=SimpleNamespace(session_id="native-before"))
    async def native_prompt(self, prompt, session_id, **kwargs):
        assert tool._request_approval("click", {}, "another-session") is not None
        first = await asyncio.to_thread(tool._request_approval, "click", {}, state.agent.session_id)
        state.agent.session_id = "native-after-compression"
        second = await asyncio.to_thread(tool._request_approval, "click", {}, state.agent.session_id)
        return first, second
    Server.prompt = native_prompt
    async def request_permission(**request):
        requests.append(request)
        return SimpleNamespace(outcome=SimpleNamespace(outcome="selected", option_id="allow_once"))
    runtime.install()
    result = await Server(SimpleNamespace(request_permission=request_permission), {"public-acp": state}).prompt([], "public-acp")
    assert result == (None, None)
    assert [request["session_id"] for request in requests] == ["public-acp", "public-acp"]


def test_cross_session_action_cannot_reuse_another_prompt_approval():
    token = runtime._permission.set({"session_id": "one", "cancelled": lambda: False,
                                     "request": lambda *args: "allow_once"})
    try:
        assert "computer_permission_context_missing" in runtime.request_approval("click", {}, "two")
    finally:
        runtime._permission.reset(token)


async def test_probe_incomplete_does_not_claim_support(monkeypatch):
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: None)
    missing = await skill_adapter.probe_computer_use(AcpAgentSpec(command=("hermes", "acp")))
    assert missing["desktop"]["status"] == "unavailable"
    assert missing["desktop"]["requirements"] == ["hermes_runtime_missing"]


@pytest.mark.parametrize("native_disabled", [False, True])
def test_unconfigured_cua_driver_allows_authorized_fallback_but_keeps_native_policy(monkeypatch, native_disabled):
    from drivers.computer_use.policy import Candidate, from_native, select_provider
    config = {"agent": {"disabled_toolsets": ["computer_use"]}} if native_disabled else {}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", SimpleNamespace(load_config_readonly=lambda: config))
    native_tool = SimpleNamespace(check_computer_use_requirements=lambda: True)
    monkeypatch.setitem(sys.modules, "tools.computer_use", SimpleNamespace(tool=native_tool))
    monkeypatch.setitem(sys.modules, "tools.computer_use.tool", native_tool)
    monkeypatch.setattr(computer_use_probe, "desktop_readiness_status", lambda: {
        "ready": False, "accessibility": False, "screen_recording": False})
    monkeypatch.setattr(computer_use_probe, "browser_readiness", lambda: {"status": "unavailable"})
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    probe = computer_use_probe.probe()
    fallback = Candidate("peekaboo", "desktop", "fallback", "ready", verified=True)
    choice = select_provider("desktop", (*from_native(probe), fallback))
    if native_disabled:
        assert probe["desktop"]["status"] == "disabled"
        assert choice.status == "disabled"
        return
    assert probe["desktop"]["status"] == "needs_setup"
    assert probe["desktop"]["permissionSetup"] == {
        "host": "cua-driver", "permissions": ["screen-recording", "accessibility"]}
    assert probe["desktop"]["installed"] is True
    assert not probe["desktop"].get("exposed")
    assert probe["desktop"]["selected_tool_enabled"] is False
    assert choice.selected is fallback
    prepared = computer_use.prepare_spec(AcpAgentSpec(command=("hermes", "acp")), {
        "_kausComputerUse": {"desktop": choice.to_dict()}})
    assert prepared.env[computer_use.DESKTOP_ENV] == "0"
    blocked = select_provider("desktop", (*from_native(probe), fallback), enabled=False)
    assert blocked.status == "disabled"


@pytest.mark.parametrize('data,ready', [
    ({'accessibility': True, 'screen_recording': True}, True),
    ({'accessibility': False, 'screen_recording': False}, False),
    ({'accessibility': True}, None),
    ({'accessibility': 'true', 'screen_recording': True}, None),
    ({}, None), (None, None),
])
def test_macos_readiness_uses_only_native_permission_status_not_full_doctor(monkeypatch, data, ready):
    calls = []
    def read(binary, *args, **kwargs):
        calls.append((binary, args, kwargs))
        return data
    def forbidden():
        pytest.fail('Settings readiness must not run the full doctor')
    monkeypatch.setattr(computer_use_probe.sys, 'platform', 'darwin')
    monkeypatch.setitem(sys.modules, 'tools.computer_use.permissions', SimpleNamespace(
        _resolve_driver_cmd=lambda override: '/native/cua-driver', _json_out=read,
        computer_use_status=forbidden, _doctor=forbidden))
    result = computer_use_probe.desktop_readiness_status()
    assert result['ready'] is ready
    assert result['installed'] is True
    assert calls == [('/native/cua-driver', ('permissions', 'status', '--json'), {'timeout': 5})]


def test_macos_permission_status_compatibility_retains_native_sanitized_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(computer_use_probe.sys, 'platform', 'darwin')
    monkeypatch.setitem(sys.modules, 'tools.computer_use.permissions', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tools.computer_use.cua_backend', SimpleNamespace(
        resolve_cua_driver_cmd=lambda override: '/native/cua-driver',
        cua_driver_child_env=lambda: {'PATH': '/native/bin', 'SANITIZED': 'yes'}))
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"accessibility": false, "screen_recording": false}')
    monkeypatch.setattr(computer_use_probe.subprocess, 'run', run)
    assert computer_use_probe.desktop_readiness_status()['ready'] is False
    assert calls[0][0] == ['/native/cua-driver', 'permissions', 'status', '--json']
    assert calls[0][1]['env'] == {'PATH': '/native/bin', 'SANITIZED': 'yes'}
    assert calls[0][1]['timeout'] == 5


def test_non_macos_readiness_keeps_native_runtime_health_check(monkeypatch):
    monkeypatch.setattr(computer_use_probe.sys, 'platform', 'linux')
    monkeypatch.setitem(sys.modules, 'tools.computer_use.permissions', SimpleNamespace(
        computer_use_status=lambda: {'ready': True, 'installed': True}))
    assert computer_use_probe.desktop_readiness_status() == {'ready': True, 'installed': True}


def test_unknown_permissions_never_become_ready_or_erase_browser_status(monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(load_config_readonly=lambda: {}))
    monkeypatch.setitem(sys.modules, 'tools.computer_use.tool', SimpleNamespace(
        check_computer_use_requirements=lambda: True))
    monkeypatch.setattr(computer_use_probe, 'browser_readiness', lambda: {'status': 'ready'})
    monkeypatch.setattr(computer_use_probe, 'desktop_readiness_status', lambda: {'installed': True, 'ready': None})
    result = computer_use_probe.probe()
    assert result['browser']['status'] == 'ready'
    assert result['desktop']['status'] == 'unknown'
    assert result['desktop']['requirements'] == ['computer_permission_status_unavailable']
    assert 'permissionSetup' not in result['desktop']


def test_browser_exec_replacement_does_not_get_misclassified_as_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "tools.browser_tool", SimpleNamespace(
        check_browser_routed_requirements=lambda: False))
    monkeypatch.setitem(sys.modules, "tools.browser_use_cli", SimpleNamespace(
        is_browser_use_cli_mode=lambda: True, _find_cli=lambda: ["browser-use"]))
    assert computer_use_probe.browser_ready()
    sys.modules["tools.browser_use_cli"]._find_cli = lambda: None
    assert not computer_use_probe.browser_ready()
    sys.modules["tools.browser_use_cli"]._find_cli = lambda: ["/local/bin/uvx", "browser-use"]
    report = computer_use_probe.browser_readiness()
    assert report["status"] == "needs_setup"
    assert report["requirements"] == ["browser_cli_install_required"]
    assert report["installed"] is False
    # Hermes still advertises this tool. Do not silently inject another one.
    assert report["exposed"] is True
    assert report["selected_tool_enabled"] is True


async def test_driver_readiness_does_not_create_project_home(tmp_path, monkeypatch):
    from app.projects.models import AgentBinding
    from drivers.hermes.driver import HermesDriver
    owner = HermesDriver(hermes_root=tmp_path)
    binding = AgentBinding.create(project="synthetic", backend="hermes",
                                   runtime_config={"project_execution": "isolated"})
    owner.register_binding(binding)
    probe = AsyncMock(return_value={"browser": {"status": "unknown"}, "desktop": {"status": "unknown"}})
    monkeypatch.setattr(computer_use, "probe_native", probe)
    await owner.computer_use_status(binding)
    assert not (tmp_path / ".kaus-scopes").exists()
    assert probe.call_args.args[0].env["HERMES_HOME"] == str(tmp_path)


@pytest.mark.parametrize("policy", ["auto", "off", "blocked", None])
async def test_legacy_http_rejects_explicit_computer_policy_without_migrating_or_writing(tmp_path, policy):
    from app.capabilities.models import EffectiveCapabilities, EffectiveCapability, BlockedCapability
    from app.conversations.models import Conversation
    from app.projects.models import AgentBinding
    from drivers.hermes.driver import HermesDriver
    owner = HermesDriver(hermes_root=tmp_path)
    binding = AgentBinding.create(project="synthetic", backend="hermes")
    owner.register_binding(binding)
    conversation = Conversation.create(title="Legacy", project_id=binding.project_id, agent_binding_id=binding.id)
    entries, blocked = (), ()
    if policy == "blocked":
        blocked = (BlockedCapability(capability_type="policies", capability_id="computer_use",
                                     blocked_by_project_id=binding.project_id),)
    elif policy:
        entries = (EffectiveCapability(capability_type="policies", capability_id="computer_use",
                    config={"value": {"browser": policy}}, source_project_id=binding.project_id, inherited=False),)
    effective = EffectiveCapabilities(project_id=binding.project_id, entries=entries, blocked=blocked)
    if policy:
        with pytest.raises(UnsupportedCapabilityError, match="独立执行"):
            await owner.session_options_for(conversation, effective)
    else:
        assert await owner.session_options_for(conversation, effective) is None
    assert binding.runtime_config == {}
    assert not list(tmp_path.iterdir())


def test_installed_native_source_preserves_hook_contract():
    """Optional installed-source contract check, without importing or running it."""
    location = os.environ.get("HERMES_NATIVE_SOURCE_FOR_TESTS")
    if not location:
        pytest.skip("Set HERMES_NATIVE_SOURCE_FOR_TESTS for the installed-source contract")
    source = Path(location)
    server = (source / "acp_adapter/server.py").read_text()
    tree = ast.parse(server)
    prompt = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "prompt")
    args = {item.arg for item in prompt.args.args}
    assert {"prompt", "session_id"} <= args
    # The runtime permission context must survive the native executor boundary.
    assert "contextvars.copy_context()" in ast.get_source_segment(server, prompt)
    tool = (source / "tools/computer_use/tool.py").read_text()
    handler = next(n for n in ast.walk(ast.parse(tool)) if isinstance(n, ast.FunctionDef) and n.name == "handle_computer_use")
    content = ast.get_source_segment(tool, handler)
    assert "_request_approval(action, args, session_id)" in content
    assert content.index("_request_approval(action, args, session_id)") < content.index("_get_backend(")


def test_local_http_computer_policy_uses_source_platform_allowlist(monkeypatch):
    from drivers.hermes import computer_use_runtime as runtime
    config = {"platform_toolsets": {"acp": ["all"], "api_server": ["writing"]}}
    resolver = lambda name: {"writing": ["write_file"], "computer_use": ["computer_use"]}[name]
    monkeypatch.setenv("KAUS_HERMES_HTTP_TRANSPORT", "1")
    assert runtime.disabled_by_config(config, "computer_use", resolver=resolver)
    config["platform_toolsets"]["api_server"] = ["all"]
    config["platform_toolsets"]["acp"] = []
    assert not runtime.disabled_by_config(config, "computer_use", resolver=resolver)
    monkeypatch.delenv("KAUS_HERMES_HTTP_TRANSPORT")
    assert runtime.disabled_by_config(config, "computer_use", resolver=resolver)
