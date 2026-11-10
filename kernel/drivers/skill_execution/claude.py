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
    # 工作目录 .mcp.json（项目级）：严格模式会忽略这个文件，所以把用户已经批准过的那几条
    # 显式带上；没批准过的照旧不加载并提示。批准记录在 ~/.claude.json 的 projects[<cwd>]。
    approved, pending = _approved_project_mcp(cwd, projects, disabled)
    for name, definition in approved.items():
        servers.setdefault(name, definition)
    meta, claude, options = _claude_code_options(metadata)
    options["strictMcpConfig"] = True
    options["mcpServers"] = {**(options.get("mcpServers") or {}), **servers}
    if pending:
        warnings.append("工作目录 .mcp.json 里还没批准的 MCP 本次不会加载：" + "、".join(sorted(pending)))
    return spec, _with_options(metadata, meta, claude, options), tuple(warnings)


def _approved_project_mcp(cwd, projects, disabled):
    """``<cwd>/.mcp.json`` 里用户批准过的项目级 MCP（``enableAllProjectMcpServers`` /
    ``enabledMcpjsonServers``，``disabledMcpjsonServers`` 排除；code.claude.com/docs/en/settings）。

    返回 ``(批准的 名字 → 定义, 没批准的名字)``。读不到文件就两样都空。
    """
    if not cwd:
        return {}, set()
    try:
        data = json.loads((Path(cwd) / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, set()
    declared = data.get("mcpServers") if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) else {}
    entry = {}
    for key, value in projects.items():
        try:
            if isinstance(value, dict) and Path(key).expanduser().resolve() == Path(cwd).expanduser().resolve():
                entry = value
                break
        except OSError:
            continue
    allow_all = entry.get("enableAllProjectMcpServers") is True
    enabled = {n for n in entry.get("enabledMcpjsonServers", []) if isinstance(n, str)}
    refused = {n for n in entry.get("disabledMcpjsonServers", []) if isinstance(n, str)} | set(disabled)
    approved, pending = {}, set()
    for name, definition in declared.items():
        if not isinstance(definition, dict) or name in refused:
            continue
        if allow_all or name in enabled:
            approved[name] = definition
        else:
            pending.add(name)
    return approved, pending


# --------------------------------------------------------------------------- #
# 开对话前关掉自带记忆（2026-11-09，设计稿第 5 节「改向」）
# --------------------------------------------------------------------------- #

#: ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`` 经 claude-agent-acp 这条路还没在真实 Claude Code 上跑过。
NATIVE_MEMORY_DISABLE_VERIFIED = False
AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


def disable_native_memory(spec, metadata):
    """Turn Claude Code's auto memory off for this process only.

    ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``: "When disabled, Claude does not create or
    load auto memory files" (code.claude.com/docs/en/env-vars); it "takes precedence
    over [``autoMemoryEnabled``] for one session, in either direction"
    (code.claude.com/docs/en/settings-reference#automemoryenabled). claude-agent-acp
    (v0.90.0 and main, ``src/acp-agent.ts`` ``createSession``) builds the SDK ``env``
    as ``{...process.env, ...userProvidedOptions.env, ...}``, so the adapter process
    environment is inherited and ``_meta.claudeCode.options.env`` is layered on top;
    both are set here. ``~/.claude/settings.json`` is not modified. A settings-file
    ``env`` block that forces the variable to ``0`` would still win.
    """
    meta, claude, options = _claude_code_options(metadata)
    options["env"] = {**(options.get("env") or {}), AUTO_MEMORY_ENV: "1"}
    return spec.with_env(**{AUTO_MEMORY_ENV: "1"}), _with_options(metadata, meta, claude, options)


# --------------------------------------------------------------------------- #
# 能力库插件原样送达（2026-11-09，设计稿第 6 节）
# --------------------------------------------------------------------------- #

#: Claude Code 插件格式（``.claude-plugin/plugin.json``）由 Claude Code 自己加载。
NATIVE_PLUGIN_FORMATS = ("claude",)
#: 完整插件（含 hooks / 命令 / MCP）经 ``_meta.claudeCode.options.plugins`` 这条路还没在真实
#: Claude Code 上跑过；只有 Kaus 自己生成的技能插件（``kaus-project``）走过同一参数。
NATIVE_PLUGIN_DELIVERY_VERIFIED = False


def deliver_plugins(spec, metadata, plugins):
    """Installed Claude Code plugins ride ``session/new`` ``_meta.claudeCode.options.plugins``.

    Agent SDK ``Options.plugins`` takes ``{type: "local", path}`` entries that are
    loaded for the session (code.claude.com/docs/en/agent-sdk/plugins); claude-agent-acp
    spreads ``_meta.claudeCode.options`` into those options (see ``deliver_instructions``).
    The managed skill projection already uses the same list for ``kaus-project``; the
    library packages are appended after it. When the managed skill allowlist
    (``options.skills``) is active, the plugin's namespaced skills (``<plugin>:<skill>``,
    the SDK's plugin skill naming) are added so the allowlist does not hide them.
    Nothing is written to ``~/.claude``.
    """
    meta, claude, options = _claude_code_options(metadata)
    listed = [dict(row) for row in options.get("plugins") or () if isinstance(row, dict)]
    known = {row.get("path") for row in listed}
    skills = list(options.get("skills")) if isinstance(options.get("skills"), list) else None
    for plugin in plugins:
        if plugin["path"] not in known:
            listed.append({"type": "local", "path": plugin["path"]})
            known.add(plugin["path"])
        if skills is not None:
            for skill in plugin.get("skills", ()):
                qualified = f"{plugin['name']}:{skill}"
                if qualified not in skills:
                    skills.append(qualified)
    options["plugins"] = listed
    if skills is not None:
        options["skills"] = skills
    if options.get("strictMcpConfig"):
        # 严格模式只加载显式传入的 MCP，插件自己带的会被挡掉：把它们也写进清单。
        # 名字加插件前缀，和项目 MCP、其他插件不撞名。
        servers = dict(options.get("mcpServers") or {})
        for plugin in plugins:
            for name, definition in (plugin.get("mcpServers") or {}).items():
                servers.setdefault(f"{plugin['name']}-{name}", dict(definition))
        options["mcpServers"] = servers
    return spec, _with_options(metadata, meta, claude, options)


# --------------------------------------------------------------------------- #
# Agent 自己装的插件：本次不加载没分到这里的（2026-11-09，设计稿第 6 节补充）
# --------------------------------------------------------------------------- #

#: 2026-11-10 用 Claude Code 2.1.309 在隔离配置目录里装合成插件实测：``--settings`` 标志层的
#: ``enabledPlugins: false`` 让 ``plugin list`` 显示「disabled」，用户 ``settings.json`` 不变。
#: 经 claude-agent-acp → SDK ``settings`` 这一段还没跑过。
NATIVE_PLUGIN_EXCLUSION_VERIFIED = False


def exclude_native_plugins(spec, metadata, *, excluded, kept):
    """Installed plugins not assigned here are turned off for this session only.

    Agent SDK ``Options.settings`` is loaded as the flag settings layer
    (code.claude.com/docs/en/agent-sdk/typescript), which sits above user and project
    settings, and ``enabledPlugins: {"<plugin>@<market>": false}`` disables a plugin
    with all its skills, commands, hooks and MCP (code.claude.com/docs/en/settings).
    An inline ``settings`` object already in ``_meta`` is merged, not replaced.
    ``~/.claude/settings.json`` is not modified.

    Under strict MCP config (set by ``exclude_native_mcp``) the CLI ignores plugin
    servers, so the plugins that stay on have their MCP passed explicitly, prefixed
    with the plugin name like the library plugins in ``deliver_plugins``.
    """
    meta, claude, options = _claude_code_options(metadata)
    if excluded:
        settings = options.get("settings")
        if isinstance(settings, str):
            return spec, metadata, ("已有设置文件参数，本次没能关掉未分到这里的自装插件",)
        settings = dict(settings or {})
        enabled = dict(settings.get("enabledPlugins") or {})
        for row in excluded:
            if row.get("pluginRef"):
                enabled[str(row["pluginRef"])] = False
        settings["enabledPlugins"] = enabled
        options["settings"] = settings
    if options.get("strictMcpConfig"):
        servers = dict(options.get("mcpServers") or {})
        for row in kept:
            for name, definition in (row.get("mcpServers") or {}).items():
                servers.setdefault(f"{row['name']}-{name}", dict(definition))
        options["mcpServers"] = servers
    return spec, _with_options(metadata, meta, claude, options), ()
