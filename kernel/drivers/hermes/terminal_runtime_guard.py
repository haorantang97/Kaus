"""Process-local MCP and edit policy shared with the installed native CLI."""
from copy import deepcopy
import hashlib
import inspect
import os

PINS = {
    '_load_mcp_config': '5ec894e03b92d415a0e8ddad0f7c1b14d11596c320105a766373aa6313b5fccd',
    'register_mcp_servers': '7df4661c92027d24f4ce7e434d60fc74711390a3a2a3b8ed2dcbef6fa19899af',
    'get_edit_approval_requester': '328a77673d984d19a548cae6652830fffbce081928bb10f8f12c586e8f8fe141',
    'maybe_require_edit_approval': '270b65efc408e89cfaf217c192b08de1c64833b73b47081c6276d67d51f5e002',
    'should_auto_approve_edit': 'b9360b6acb30c68abe607f689878c39969c7173da3a07cbd8a2c74c77ac877d4',
    '_restore_session_yolo': 'e1ba2e3469e3e8c50b14c129b7b1e49340ebb95b8ea084f75af417d2a084c06a',
}


def _check(function):
    if hashlib.sha256(inspect.getsource(function).encode()).hexdigest() != PINS[function.__name__]:
        raise RuntimeError('此 Hermes 版本尚未通过终端配置核验')


def mcp_config(servers):
    result = {}
    for item in servers:
        name = item['name']
        if name in result:
            raise RuntimeError('终端 MCP 名称重复')
        if 'command' in item:
            result[name] = {'command': item['command'], 'args': list(item.get('args', [])),
                            'env': {entry['name']: entry['value'] for entry in item.get('env', [])}}
        else:
            result[name] = {'url': item['url'], 'headers': {
                entry['name']: entry['value'] for entry in item.get('headers', [])}}
    return result


def verify_mcp(module, servers):
    with module._lock:
        for name, config in servers.items():
            actual = module._servers.get(name)
            if (actual is None or actual.session is None
                    or any(actual._config.get(key) != value for key, value in config.items())):
                raise RuntimeError('MCP 连接失败：' + name)
        if set(module._servers) - set(servers):
            raise RuntimeError('终端 MCP 范围与项目不一致')


def install(value):
    # Native ACP edit modes govern edits, not arbitrary shell approval bypass.
    # Reuse its exact path/sensitive-file policy and the CLI's own human prompt.
    os.environ['HERMES_YOLO_MODE'] = '0'
    os.environ.pop('HERMES_TUI', None)
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    os.environ['HERMES_YOLO_MODE'] = '0'
    from tools import mcp_tool
    from acp_adapter import edit_approval
    for fn in (mcp_tool._load_mcp_config, mcp_tool.register_mcp_servers,
               edit_approval.get_edit_approval_requester, edit_approval.maybe_require_edit_approval,
               edit_approval.should_auto_approve_edit):
        _check(fn)
    servers = mcp_config(value['mcpServers'])
    mcp_tool._load_mcp_config = lambda: deepcopy(servers)
    mcp_tool.register_mcp_servers(servers)
    verify_mcp(mcp_tool, servers)

    policy = value['editPolicy']
    def requester(proposal):
        if edit_approval.should_auto_approve_edit(proposal, policy, value.get('workspace')):
            return True
        from tools.terminal_tool import _get_approval_callback
        from tools.approval import prompt_dangerous_approval
        callback = _get_approval_callback()
        if callback is None:
            return False
        choice = prompt_dangerous_approval(proposal.path, 'Approve file edit',
            allow_permanent=False, allow_session=False, approval_callback=callback)
        return choice == 'once'
    edit_approval.get_edit_approval_requester = lambda: requester
    # An old /yolo choice must not supersede the currently selected Kaus mode.
    # This only suppresses startup restoration; explicit later CLI actions keep
    # their native behavior and protected instruction paths still require consent.
    import cli
    # The CLI resolves enabled MCP toolsets from its in-memory config separately
    # from the connection registry. Both must see this conversation's servers.
    cli.CLI_CONFIG['mcp_servers'] = deepcopy(servers)
    _check(cli.HermesCLI._restore_session_yolo)
    cli.HermesCLI._restore_session_yolo = lambda *args, **kwargs: None
    args = []
    model, provider = value.get('model'), value.get('provider')
    if model:
        from hermes_cli.models import parse_model_input
        from hermes_cli.config import load_config_readonly
        baseline = load_config_readonly().get('model') or {}
        current = provider or (baseline.get('provider') if isinstance(baseline, dict) else None) or 'auto'
        provider, model = parse_model_input(model, current)
        args += ['--model', model]
    if provider:
        args += ['--provider', provider]
    if value.get('reasoning'):
        args += ['--reasoning', value['reasoning']]
    return args
