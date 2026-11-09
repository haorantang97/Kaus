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
