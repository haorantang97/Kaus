"""Check native Hermes MCP registration without editing its installation.

Hermes 0.21.0 catches discovery errors and can accept a session without its
requested tools. Inspect the actual registry and refreshed agent tool surface,
not a second probe server. Native ambient servers with other names stay native.
"""
import hashlib
import inspect
import json
import os


PINS = {
    '_register_session_mcp_servers': '4cb9f6ec2b972cd1f0f5c1f0bd66fa79efc8482f6b72d13da36875eb83a5e2ef',
    'register_mcp_servers': '7df4661c92027d24f4ce7e434d60fc74711390a3a2a3b8ed2dcbef6fa19899af',
    '_load_mcp_config': '5ec894e03b92d415a0e8ddad0f7c1b14d11596c320105a766373aa6313b5fccd',
}


def _config(server):
    if hasattr(server, 'command'):
        return {'command': server.command, 'args': list(server.args),
                'env': {item.name: item.value for item in server.env}}
    return {'url': server.url, 'headers': {item.name: item.value for item in server.headers}}


def verify_registered(module, state, requested, *, deferred_names=()):
    callable_names = set(state.agent.valid_tool_names) | set(deferred_names)
    with module._lock:
        for item in requested:
            actual = module._servers.get(item.name)
            expected = _config(item)
            # Compare in memory only. Neither env values nor native errors are
            # included in the returned diagnostic.
            if (actual is None or actual.session is None
                    or any(actual._config.get(key) != value for key, value in expected.items())):
                raise RuntimeError(f'MCP 连接失败：{item.name}')
            registered = set(actual._registered_tool_names)
            if not registered <= callable_names:
                raise RuntimeError(f'MCP 工具未加载：{item.name}')


def install():
    from acp_adapter.server import HermesACPAgent
    from tools import mcp_tool
    original = HermesACPAgent._register_session_mcp_servers
    if getattr(original, '_kaus_mcp_guard', False):
        return
    for fn in (original, mcp_tool.register_mcp_servers, mcp_tool._load_mcp_config):
        if hashlib.sha256(inspect.getsource(fn).encode()).hexdigest() != PINS.get(fn.__name__):
            raise RuntimeError('此 Hermes 版本尚未通过 MCP 接口核验')
    names = set(json.loads(os.environ.get('KAUS_HERMES_MCP_NAMES', '[]')))
    project_owned = os.environ.get('HERMES_ACP_SKIP_CONFIGURED_MCP') == '1'
    load_config = mcp_tool._load_mcp_config

    def configured_without_session_overrides():
        # Native discovery starts before session/new. Exclude only names that
        # this process will receive through ACP, avoiding an old same-name
        # connection winning the registry's existing-server shortcut.
        # Native agent construction may restart discovery even if entry.py
        # skipped it. An isolated Project receives its entire set through ACP.
        if project_owned:
            return {}
        return {name: value for name, value in load_config().items() if name not in names}

    async def register(self, state, mcp_servers):
        await original(self, state, mcp_servers)
        deferred = ()
        if {'tool_search', 'tool_describe', 'tool_call'} <= set(state.agent.valid_tool_names):
            from model_tools import get_tool_definitions
            from tools.tool_search import scoped_deferrable_names
            definitions = get_tool_definitions(enabled_toolsets=state.agent.enabled_toolsets,
                disabled_toolsets=getattr(state.agent, 'disabled_toolsets', None),
                quiet_mode=True, skip_tool_search_assembly=True)
            deferred = scoped_deferrable_names(definitions)
        try:
            verify_registered(mcp_tool, state, mcp_servers or (), deferred_names=deferred)
        except RuntimeError as exc:
            from acp.exceptions import RequestError
            raise RequestError(-32000, str(exc)) from None

    register._kaus_mcp_guard = True
    mcp_tool._load_mcp_config = configured_without_session_overrides
    HermesACPAgent._register_session_mcp_servers = register
