"""Claude Agent SDK's native Skill allowlist, including namespaced plugin Skills.

The allowlist is a loader/tool boundary, not a filesystem sandbox. Native homes,
provider routing, login, hooks and non-Skill plugin functionality remain intact.
"""
import asyncio
import json
import re
from pathlib import Path

from drivers.base import UnsupportedCapabilityError
from .common import materialize, unsupported

COMPUTER_USE_TERMINAL_MCP = True


def _version(value):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


async def supported(spec, runtime_info):
    name, version = runtime_info or (None, None)
    if name != "@agentclientprotocol/claude-agent-acp" or _version(version) < (0, 90, 0):
        return False
    override = spec.build_env().get("CLAUDE_CODE_EXECUTABLE")
    if override:
        # Older CLI binaries silently ignore initialize.skills even when a new
        # SDK sends it. An explicit native binary must meet the verified floor.
        try:
            process = await asyncio.create_subprocess_exec(override, "--version", env=spec.build_env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return False
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            return process.returncode == 0 and _version(stdout.decode()) >= (2, 1, 296)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False
    return True


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if not await supported(spec, runtime_info):
        return unsupported(binding, "当前适配器或原生版本未提供已验证的技能白名单", dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout="plugin", dry_run=dry_run)
    if plan.root is not None:
        plan.metadata = {"claudeCode": {"options": {
            "plugins": [{"type": "local", "path": str(plan.root)}],
            "skills": ["kaus-project:" + name for name in plan.names],
        }}}
    plan.spec = spec
    return plan


def cli_command(plan, command):
    # The interactive CLI currently has no equivalent of SDK initialize.skills.
    # --disable-slash-commands would also break the allowed set.
    raise UnsupportedCapabilityError("当前原生终端不支持项目技能白名单，请在对话页面继续")


def external_cli_command(plan, command, *, conversation, binding, spec):
    if plan is not None:
        return cli_command(plan, command)
    from .terminal_settings import approval, arguments, choice, selected
    mode = choice(approval(conversation), {
        'ask': 'default', 'host:ask': 'default', 'default': 'default',
        'auto': 'acceptEdits', 'acceptEdits': 'acceptEdits',
        'native:auto': 'auto', 'bypass': 'bypassPermissions',
        'bypassPermissions': 'bypassPermissions', 'read_only': 'plan',
        'plan': 'plan', 'dontAsk': 'dontAsk',
    }, '审批方式')
    return arguments(command,
        ('--model', selected(conversation, 'model_id', 'model')),
        ('--effort', selected(conversation, 'reasoning_mode', 'effort', 'reasoning_effort')),
        ('--permission-mode', mode))


def external_cli_context(command, spec, metadata):
    """Carry session MCP into the native CLI without changing global files."""
    import json
    from pathlib import Path
    import sys
    from .claude_terminal import MCP_ENV
    servers = {}
    for server in metadata.get('mcpServers', []):
        row = {key: value for key, value in server.items() if key != 'name'}
        if isinstance(row.get('env'), list):
            row['env'] = {item['name']: item['value'] for item in row['env']}
        if isinstance(row.get('headers'), list):
            row['headers'] = {item['name']: item['value'] for item in row['headers']}
        servers[server['name']] = row
    if not servers:
        return tuple(command), spec
    return ((sys.executable, str(Path(__file__).with_name('claude_terminal.py')), *command),
            spec.with_env(**{MCP_ENV: json.dumps({'mcpServers': servers})}))


async def load_native_history(spec, binding, native_session_id):
    from drivers.claude_code.native_history import load_native_history as load
    return await load(spec, binding, native_session_id)


async def load_local_native_history(spec, binding, native_session_id):
    return await load_native_history(spec, binding, native_session_id)


# --------------------------------------------------------------------------- #
# 开对话前的自动送达（2026-11-09，设计稿第 3 / 3a 节）
# --------------------------------------------------------------------------- #

#: 严格模式 + 内存传定义这条路还没在真实 Claude Code 上跑过。
NATIVE_MCP_EXCLUSION_VERIFIED = False


def _claude_code_options(metadata):
    meta = dict(metadata.get("_meta") or {})
    claude = dict(meta.get("claudeCode") or {})
    options = dict(claude.get("options") or {})
    return meta, claude, options


def _with_options(metadata, meta, claude, options):
    return {**metadata, "_meta": {**meta, "claudeCode": {**claude, "options": options}}}


def deliver_instructions(spec, metadata, body):
    """Project instructions ride ``session/new`` ``_meta.claudeCode.options.systemPrompt``.

    claude-agent-acp (checked at v0.90.0 and main, ``src/acp-agent.ts`` ``createSession``)
    spreads ``_meta.claudeCode.options`` into the Agent SDK ``query()`` options after
    its own ``systemPrompt`` default, so ``{type: "preset", preset: "claude_code",
    append}`` keeps Claude Code's prompt and appends ours (SDK ``Options.systemPrompt``,
    code.claude.com/docs/en/agent-sdk/typescript). The same ``_meta`` is applied on
    ``session/load`` / ``session/resume``. No file is written.
    """
    meta, claude, options = _claude_code_options(metadata)
    options["systemPrompt"] = {"type": "preset", "preset": "claude_code", "append": body}
    return spec, _with_options(metadata, meta, claude, options)


def _claude_json(spec):
    env = spec.build_env()
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    path = (Path(config_dir) / ".claude.json") if config_dir else Path(env.get("HOME") or Path.home()) / ".claude.json"
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def exclude_native_mcp(spec, metadata, *, excluded, assigned, cwd):
    """Claude Code has no per-name disable flag: use strict MCP config instead.

    ``strictMcpConfig`` makes the CLI load only the ``mcpServers`` passed in
    (``--strict-mcp-config``; SDK option documented as ignoring ``.mcp.json``, user
    settings and plugin servers). The assigned native definitions are therefore
    read from ``~/.claude.json`` (user scope: top-level ``mcpServers``) at launch and
    passed **in memory** through ``_meta``; the ACP ``mcpServers`` (Kaus project MCP)
    are merged on top by the adapter. Servers the user disabled for this directory
    (``projects[<cwd>].disabledMcpServers``, code.claude.com/docs/en/mcp) stay off,
    and this directory's local-scope servers (``projects[<cwd>].mcpServers``) keep
    loading because they were never part of the inventory.
    """
    warnings = []
    config = _claude_json(spec)
    user_scope = config.get("mcpServers") if isinstance(config.get("mcpServers"), dict) else {}
    projects = config.get("projects") if isinstance(config.get("projects"), dict) else {}
    local = {}
    disabled = set()
    if cwd:
        for key, value in projects.items():
            if not isinstance(value, dict):
                continue
            try:
                same = Path(key).expanduser().resolve() == Path(cwd).expanduser().resolve()
            except OSError:
                same = key == cwd
            if same:
                if isinstance(value.get("mcpServers"), dict):
                    local.update(value["mcpServers"])
                disabled.update(name for name in value.get("disabledMcpServers", []) if isinstance(name, str))
    servers = {}
    for row in assigned:
        name = row.get("name")
        if name in disabled:
            continue
        definition = user_scope.get(name)
        if not isinstance(definition, dict):
            warnings.append(f"自带 MCP {name} 的定义已不在 ~/.claude.json 里，本次不会加载")
            continue
        servers[name] = definition
    for name, definition in local.items():
        if isinstance(definition, dict) and name not in disabled and name not in servers:
            servers[name] = definition
    meta, claude, options = _claude_code_options(metadata)
    options["strictMcpConfig"] = True
    options["mcpServers"] = {**(options.get("mcpServers") or {}), **servers}
    if cwd and (Path(cwd) / ".mcp.json").is_file():
        warnings.append("严格 MCP 模式下不会加载工作目录 .mcp.json 里的 MCP")
    return spec, _with_options(metadata, meta, claude, options), tuple(warnings)
