"""Per-process guard for the installed Gemini SkillManager interface."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, unsupported
from .node_package import locate


async def refresh_inventory(spec):
    from .native_catalog import query, save_paths
    package = locate(spec, '@google/gemini-cli')
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if not package or not node:
        raise ValueError('原生技能来源不可用')
    raw = await query(spec, (node, str(package / 'bundle/gemini.js'), 'skills', 'list', '--all'))
    raw = re.sub(r'\x1b\[[0-9;]*m', '', raw)
    enabled, paths = False, []
    for line in raw.splitlines():
        if line and not line[0].isspace() and re.search(r'\[(?:Enabled|Disabled)\]', line):
            enabled = '[Enabled]' in line
        elif enabled and re.match(r'^\s+Location:\s+', line):
            paths.append(re.sub(r'^\s+Location:\s+', '', line).strip())
    if 'Discovered Agent Skills:' not in raw and 'No skills' not in raw:
        raise ValueError('原生技能目录格式不兼容')
    save_paths(spec, 'gemini', paths)


def inventory_roots(spec, projects, bindings):
    from .native_catalog import cached_roots
    return cached_roots(spec, 'gemini', 'Gemini CLI', projects, bindings)


def loader_module(package):
    entry = package / 'bundle/gemini.js'
    try:
        source = entry.read_text()
        for name in re.findall(r'(?:from\s+|import\s+)["\'](\./chunk-[^"\']+\.js)["\']', source):
            path = entry.parent / name
            code = path.read_text()
            if 'var SkillManager = class' in code and 'async function loadSkillsFromDir(' in code:
                return path.resolve()
    except OSError:
        pass
    return None


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    package = locate(spec, '@google/gemini-cli')
    module = loader_module(package) if package else None
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if not module or not node:
        return unsupported(binding, '当前原生安装未提供可验证的技能加载接口', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    guard_source = Path(__file__).with_name('gemini_guard.mjs').read_text()
    guard = plan.root / ('gemini-guard-' + hashlib.sha256(guard_source.encode()).hexdigest()[:24] + '.mjs')
    if guard.exists():
        if guard.is_symlink() or guard.read_text() != guard_source:
            raise UnsupportedCapabilityError('项目技能加载器不一致')
    else:
        atomic_write_text(guard, guard_source)
    data = {'package': str(package.resolve()), 'module': str(module), 'moduleHash': hashlib.sha256(module.read_bytes()).hexdigest(),
            'root': str(plan.root / '.agents/skills'), 'paths': [str(p) for p in plan.paths]}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('gemini-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    if manifest.exists():
        if manifest.is_symlink() or manifest.read_text() != encoded:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(manifest, encoded)
    # Use a file URL so spaces remain a single NODE_OPTIONS argument.
    options = spec.build_env().get('NODE_OPTIONS', '')
    plan.spec = spec.with_env(KAUS_GEMINI_SKILL_SCOPE=str(manifest),
        NODE_OPTIONS=(options + ' --import=' + guard.as_uri()).strip())
    script = "const {SkillManager}=await import(" + json.dumps(module.as_uri()) + ");const m=new SkillManager();await m.discoverSkills();if(m.getSkills().length!==" + str(len(plan.paths)) + ")throw Error('incomplete');"
    process = await asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
        env={**plan.spec.build_env(), "KAUS_GEMINI_SCOPE_PROBE": "1"}, cwd=workspace_root or spec.cwd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), 20)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生技能加载验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生技能加载验证失败')
    return plan


def cli_command(plan, command):
    from .gemini_terminal import cli_command as command_for
    return command_for(plan, command)


def remember_session_options(spec, conversation, metadata):
    from .gemini_terminal import remember_session_options as remember
    remember(spec, conversation, metadata)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .gemini_terminal import external_cli_command as command_for
    return command_for(plan, command, conversation=conversation, binding=binding, spec=spec)


async def prepare_runtime_spec(spec, conversation, metadata):
    from .gemini_runtime import prepare_runtime_spec as prepare
    return await prepare(spec, conversation, metadata)


async def load_native_history(spec, binding, native_session_id):
    from .gemini_history import load_native_history as load
    return await load(spec, binding, native_session_id)


async def load_local_native_history(spec, binding, native_session_id):
    """Filesystem-only full-session reader; never starts a model or gateway."""
    return await load_native_history(spec, binding, native_session_id)


# 开对话前的原生 MCP 排除（2026-11-09，设计稿第 3a 节）。
#: 白名单语义对 ACP ``session/new`` 传入的 MCP 是否也生效，未实测。
NATIVE_MCP_EXCLUSION_VERIFIED = False

#: 空白名单在 Gemini 里等于「不过滤」（``allowedNames.length > 0`` 才生效），所以
#: 「一个自带的都不留」要用一个不会命中任何服务器的名字表达。
ALLOW_NOTHING_SENTINEL = "kaus:none"


def exclude_native_mcp(spec, metadata, *, excluded, assigned, cwd):
    """Gemini CLI only has an allow-list: ``--allowed-mcp-server-names``.

    ``packages/cli/src/config/config.ts`` defines the option (``type: 'array'``,
    comma-separated, same yargs builder as ``--acp`` / ``--experimental-acp``);
    ``packages/core/src/tools/mcp-client-manager.ts`` blocks every server not in a
    non-empty list. The list carries the assigned native names plus the project MCP
    names Kaus sends over ACP (in case the filter applies to them too); an empty
    result uses a sentinel because an empty list disables the filter.
    ``~/.gemini/settings.json`` is not modified.
    """
    allowed = [row.get("name") for row in assigned if isinstance(row.get("name"), str) and row.get("name")]
    for server in metadata.get("mcpServers", []) or ():
        name = server.get("name") if isinstance(server, dict) else None
        if isinstance(name, str) and name and name not in allowed:
            allowed.append(name)
    warnings = ()
    if any("," in name for name in allowed):
        return spec, metadata, ("含逗号的 MCP 名称无法通过 --allowed-mcp-server-names 表达，本次未排除自带 MCP",)
    if not allowed:
        allowed = [ALLOW_NOTHING_SENTINEL]
    command = (*spec.command, "--allowed-mcp-server-names", ",".join(allowed))
    return spec.model_copy(update={"command": command}), metadata, warnings


# Agent 自己装的扩展：本次只加载分到这里的（2026-11-09，设计稿第 6 节补充）。
#: ``--extensions`` 白名单经 ``--acp`` 这条路没在真实 Gemini CLI 上跑过。
NATIVE_PLUGIN_EXCLUSION_VERIFIED = False
#: 只写这一个名字表示一个扩展都不加载（``packages/cli/src/config/extensions/extensionEnablement.ts``
#: 对 ``none`` 的特判）；空白名单等于不过滤。
NO_EXTENSIONS = "none"


def exclude_native_plugins(spec, metadata, *, excluded, kept):
    """Gemini CLI has only an allow-list: ``--extensions`` / ``-e`` (``config.ts``).

    The names are the extension names from ``gemini-extension.json``, matched without
    case. Every extension left on is listed; with none left, the special ``none``
    value is used. ``~/.gemini/extensions`` is not modified.
    """
    if not excluded:
        return spec, metadata, ()
    names = [str(row.get("name")) for row in kept if row.get("name")]
    if any("," in name for name in names):
        return spec, metadata, ("含逗号的扩展名无法通过 --extensions 表达，本次没有关掉未分到这里的扩展",)
    command = (*spec.command, "--extensions", ",".join(names) or NO_EXTENSIONS)
    return spec.model_copy(update={"command": command}), metadata, ()


# 开对话前关掉自带记忆（2026-11-09，设计稿第 5 节「改向」）。
#: 系统设置覆盖文件这条路（``GEMINI_CLI_SYSTEM_SETTINGS_PATH``）没在真实 Gemini CLI 上跑过。
NATIVE_MEMORY_DISABLE_VERIFIED = False
#: v0.43 及更早的 ``save_memory``（``packages/core/src/tools/definitions/base-declarations.ts``
#: ``MEMORY_TOOL_NAME = 'save_memory'``）；v0.44.0 起该工具已删除（PR #26941），排除它是空操作。
MEMORY_TOOL_NAMES = ("save_memory",)


def system_settings_path(spec):
    """Gemini CLI 系统设置文件：``packages/cli/src/config/settings.ts``（环境变量优先）。"""
    import os
    import sys
    env = spec.build_env()
    if env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH"):
        return Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).expanduser()
    if sys.platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    if os.name == "nt":
        return Path(r"C:\ProgramData\gemini-cli\settings.json")
    return Path("/etc/gemini-cli/settings.json")


def disable_native_memory(spec, metadata):
    """One-launch system-settings overlay: ``tools.exclude += save_memory``, ``experimental.autoMemory = false``.

    Gemini CLI has no per-launch tool-exclusion flag (``packages/cli/src/config/
    config.ts`` yargs options), but the system settings file is merged last
    ("5. System Settings (as overrides)", ``settings.ts`` ``mergeSettings``) and its
    path comes from ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``. The current system file is
    read, the two memory keys merged in, and the result written as an immutable
    Kaus-owned file; the user's ``~/.gemini/settings.json`` and the real system file
    are not modified. ``experimental.autoMemory`` (``docs/cli/auto-memory.md``, default
    false) is the live memory feature; ``tools.exclude`` is deprecated upstream and
    ``save_memory`` no longer exists on >= 0.44, where the model edits
    ``~/.gemini/GEMINI.md`` with ordinary file tools — that path is not blocked.
    """
    from .copilot import read_jsonc
    from .settings_overlay import union, write_overlay
    settings = read_jsonc(system_settings_path(spec))
    tools = dict(settings["tools"]) if isinstance(settings.get("tools"), dict) else {}
    tools["exclude"] = union(tools.get("exclude"), MEMORY_TOOL_NAMES)
    experimental = dict(settings["experimental"]) if isinstance(settings.get("experimental"), dict) else {}
    experimental["autoMemory"] = False
    settings["tools"], settings["experimental"] = tools, experimental
    path = write_overlay(spec, "gemini", json.dumps(settings, sort_keys=True, ensure_ascii=False))
    return spec.with_env(GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(path)), metadata


#: ``save_memory`` 追加的段落（``packages/core/src/tools/memoryTool.ts`` ``MEMORY_SECTION_HEADER``）。
MEMORY_SECTION_HEADER = "## Gemini Added Memories"


def read_native_memory(*, home=None):
    """``~/.gemini/GEMINI.md`` 里 ``save_memory`` 记下的条目（每行一条），只读。

    Auto Memory（``experimental.autoMemory``）的产物是待用户批准的补丁，批准后同样写进记忆
    文件，文档没有给出单独的存放位置，这里不读。注意：关闭自带记忆挡不住 Gemini 加载
    ``GEMINI.md`` 本身（见 :func:`disable_native_memory`）。
    """
    root = Path(home) if home is not None else Path.home() / ".gemini"
    path = root / "GEMINI.md"
    if not path.is_file() or path.is_symlink():
        return []
    rows, inside = [], False
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("## "):
            inside = line.strip() == MEMORY_SECTION_HEADER
            continue
        if inside and line.lstrip().startswith("- ") and line.strip()[2:].strip():
            rows.append({"body": line.strip()[2:].strip(), "source": str(path)})
    return rows
