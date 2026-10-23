"""Process-local native CU activation and ACP approvals, without upstream edits."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
import time
import uuid

_permission = ContextVar("kaus_hermes_desktop_permission", default=None)


def disabled_by_config(config, toolset, *, resolver=None):
    """Explicit native exclusions take precedence over a Kaus activation."""
    agent = config.get("agent") or {}
    disabled = agent.get("disabled_toolsets") or []
    if isinstance(disabled, str):
        disabled = [item.strip() for item in disabled.split(",")]
    if toolset in disabled or "all" in disabled:
        return True
    platform = "api_server" if os.environ.get("KAUS_HERMES_HTTP_TRANSPORT") == "1" else "acp"
    configured = (config.get("platform_toolsets") or {}).get(platform)
    # Keep the source transport's explicit allowlist. Local HTTP continuation
    # uses ACP delivery but retains its original api_server tool policy.
    if not isinstance(configured, list) or toolset in configured or "all" in configured:
        return False
    if not configured:
        return True
    if resolver is None:
        from toolsets import resolve_toolset
        resolver = resolve_toolset
    requested = set(resolver(toolset))
    available = {tool for name in configured for tool in resolver(name)}
    return not bool(requested & available)


def desktop_toolsets(enabled, disabled, config, *, available):
    """Return the selected native set, preserving all existing caller exclusions."""
    if not available or "computer_use" in (disabled or []) or disabled_by_config(config, "computer_use"):
        raise RuntimeError("Hermes 电脑操作未就绪")
    return list(dict.fromkeys([*(enabled or ["hermes-acp"]), "computer_use"]))


def _denied(code="computer_control_denied"):
    return json.dumps({"error": "Computer control was not approved", "code": code})


def request_approval(action, args, session_id=""):
    """The native mutation gate cannot fall through without this prompt's owner."""
    context = _permission.get()
    if context is None or not (
        context["owns_session"](session_id) if "owns_session" in context
        else context["session_id"] == session_id
    ):
        return _denied("computer_permission_context_missing")
    cancelled = context["cancelled"]
    if cancelled():
        return _denied("computer_control_cancelled")
    try:
        verdict = context["request"](action, args)
    except Exception:
        return _denied()
    if cancelled():
        return _denied("computer_control_cancelled")
    return None if verdict == "allow_once" else _denied()


def permission_requester(connection, loop, session_id, cancelled, *, timeout=60):
    """Bridge a worker's native action into the existing ACP permission stream."""
    def request(action, args):
        if connection is None or cancelled():
            return "deny"
        import acp
        from acp.schema import PermissionOption

        async def ask():
            response = await connection.request_permission(
                session_id=session_id,
                tool_call=acp.update_tool_call(
                    "computer-permission-" + uuid.uuid4().hex,
                    title="Computer control · " + action, kind="other", status="pending",
                    raw_input={**args, "tool": "computer_use", "action": action},
                ),
                options=[
                    PermissionOption(option_id="allow_once", kind="allow_once", name="Allow once"),
                    PermissionOption(option_id="deny", kind="reject_once", name="Deny"),
                ],
            )
            outcome = getattr(response, "outcome", None)
            if getattr(outcome, "outcome", None) != "selected":
                return "deny"
            return "allow_once" if getattr(outcome, "option_id", None) == "allow_once" else "deny"

        future = asyncio.run_coroutine_threadsafe(ask(), loop)
        deadline = time.monotonic() + timeout
        try:
            while not cancelled() and time.monotonic() < deadline:
                try:
                    return future.result(timeout=min(.1, max(.001, deadline - time.monotonic())))
                except FutureTimeout:
                    continue
            return "deny"
        except Exception:
            return "deny"
        finally:
            if not future.done():
                future.cancel()
    return request


def blocked_toolsets():
    return [name for name, key in (("browser", "KAUS_COMPUTER_USE_BROWSER"),
                                   ("computer_use", "KAUS_COMPUTER_USE_DESKTOP"))
            if os.environ.get(key) == "0"]


def install_tool_policy(model_tools, run_agent, blocked):
    """Apply the child policy to construction, late discovery and dispatch."""
    from toolsets import resolve_toolset
    names = {name for toolset in blocked for name in resolve_toolset(toolset)}
    if "computer_use" in blocked:
        names.add("computer_use")
    original_definitions = model_tools.get_tool_definitions
    original_dispatch = model_tools.handle_function_call

    def with_disabled(function, args, kwargs):
        bound = inspect.signature(function).bind_partial(*args, **kwargs)
        current = bound.arguments.get("disabled_toolsets") or []
        bound.arguments["disabled_toolsets"] = list(dict.fromkeys([*current, *blocked]))
        return bound

    @wraps(original_definitions)
    def definitions(*args, **kwargs):
        bound = with_disabled(original_definitions, args, kwargs)
        return [item for item in original_definitions(*bound.args, **bound.kwargs)
                if item.get("function", {}).get("name") not in names]

    @wraps(original_dispatch)
    def dispatch(*args, **kwargs):
        bound = with_disabled(original_dispatch, args, kwargs)
        if bound.arguments.get("function_name") in names:
            return json.dumps({"error": "Tool disabled for this project", "code": "computer_tool_disabled"})
        return original_dispatch(*bound.args, **bound.kwargs)

    model_tools.get_tool_definitions = definitions
    model_tools.handle_function_call = dispatch
    # Native runtime keeps these module aliases for the actual tool loop.
    run_agent.get_tool_definitions = definitions
    run_agent.handle_function_call = dispatch


def install():
    selected = os.environ.get("KAUS_COMPUTER_USE_DESKTOP") == "1"
    blocked = blocked_toolsets()
    if not selected and not blocked:
        return
    import run_agent

    if getattr(run_agent.AIAgent, "_kaus_computer_use_policy", False):
        return
    if blocked:
        import model_tools
        install_tool_policy(model_tools, run_agent, blocked)
    if selected:
        from acp_adapter.server import HermesACPAgent
        from hermes_cli.config import load_config_readonly
        from tools.computer_use import tool
        if not {"session_id", "prompt"} <= set(inspect.signature(HermesACPAgent.prompt).parameters):
            raise RuntimeError("此 Hermes 版本尚未适配电脑操作")
        if not callable(getattr(tool, "_request_approval", None)):
            raise RuntimeError("此 Hermes 版本未提供电脑操作审批")
        original_prompt = HermesACPAgent.prompt
    base = run_agent.AIAgent

    class ComputerAgent(base):
        _kaus_computer_use_policy = True

        def __init__(self, *args, **kwargs):
            if blocked:
                kwargs["disabled_toolsets"] = list(dict.fromkeys([
                    *(kwargs.get("disabled_toolsets") or []), *blocked]))
            if selected:
                kwargs["enabled_toolsets"] = desktop_toolsets(
                    kwargs.get("enabled_toolsets"), kwargs.get("disabled_toolsets"),
                    load_config_readonly(), available=tool.check_computer_use_requirements())
            super().__init__(*args, **kwargs)

    run_agent.AIAgent = ComputerAgent
    if not selected:
        return

    async def prompt(self, prompt, session_id, **kwargs):
        state = self.session_manager.get_session(session_id)
        initial_native_id = getattr(getattr(state, "agent", None), "session_id", None)
        def cancelled():
            return state is None or bool(state.cancel_event and state.cancel_event.is_set())
        def owns_session(value):
            # ACP retains its public ID when native compression rotates the DB
            # head. Only the agent owned by this prompt may supply that new ID.
            current_native_id = getattr(getattr(state, "agent", None), "session_id", None)
            return bool(value) and value in {session_id, initial_native_id, current_native_id}
        context = {"session_id": session_id, "owns_session": owns_session, "cancelled": cancelled,
                   "request": permission_requester(self._conn, asyncio.get_running_loop(), session_id, cancelled)}
        token = _permission.set(context)
        try:
            return await original_prompt(self, prompt, session_id, **kwargs)
        finally:
            _permission.reset(token)

    prompt._kaus_computer_use = True
    HermesACPAgent.prompt = prompt
    # Native desktop mutation approval used to default-allow when its CLI
    # callback was absent. This ACP child always delegates to its owning host.
    tool._request_approval = request_approval
