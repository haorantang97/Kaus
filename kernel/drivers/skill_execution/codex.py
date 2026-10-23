"""Codex project skills enforced inside a verified private native loader.

The native TUI connects to the same scoped server; CODEX_HOME and thread IDs
remain unchanged. Unmanaged sessions continue to use the user's native CLI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from drivers.acp.client import AcpConnection
from drivers.base import UnsupportedCapabilityError
from drivers.codex.companions import HOST, resolve_companion, matching_tui
from .common import materialize, selection_of, unsupported
from .managed_builds import resolve_managed_binary, file_hash

COMPUTER_USE_TERMINAL_MCP = False


def _executable(spec):
    env = spec.build_env()
    path = env.get("CODEX_PATH") or shutil.which("codex", path=env.get("PATH"))
    return str(Path(path).expanduser().absolute()) if path else None


async def native_inventory(executable, spec, cwd, root, *, expected=None):
    """Read the actual native loader without starting a thread or inference."""
    process = await asyncio.create_subprocess_exec(executable, "app-server", "--stdio",
        cwd=cwd, env=spec.build_env(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name == 'posix')
    async def request(identity, method, params):
        process.stdin.write((json.dumps({"id": identity, "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            if not line:
                raise UnsupportedCapabilityError("无法读取原生技能加载清单")
            value = json.loads(line)
            if value.get("id") == identity:
                if "error" in value:
                    raise UnsupportedCapabilityError("原生运行时不支持项目技能加载查询")
                return value["result"]
    try:
        await request(1, "initialize", {"clientInfo": {"name": "kaus-skills", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        await process.stdin.drain()
        await request(2, "skills/extraRoots/set", {"extraRoots": [str(root / ".agents/skills")]})
        result = await request(3, "skills/list", {"cwds": [cwd, str(root)], "forceReload": True})
        rows = result.get("data")
        if not isinstance(rows, list) or any(row.get("errors") for row in rows):
            raise UnsupportedCapabilityError("原生技能清单不完整，未应用项目技能范围")
        paths = set()
        for row in rows:
            for skill in row.get("skills", []):
                path = skill.get("path")
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise UnsupportedCapabilityError("原生技能来源无法按路径控制")
                canonical = str(Path(path).resolve())
                if expected is not None and skill.get("enabled") is not (canonical in expected):
                    raise UnsupportedCapabilityError("原生运行时未执行项目技能范围")
                paths.add(canonical)
        return paths
    finally:
        # Inventory probes use the same owned-process cleanup as conversations.
        # Native installers can put a spawning launcher in front of the server.
        connection = AcpConnection(process.stdout, process.stdin, process=process,
                                   owns_process_group=os.name == 'posix')
        await connection.aclose()


def config_argument(entries):
    return "skills.config=[" + ",".join(
        "{path=" + json.dumps(item["path"]) + ",enabled=" + str(item["enabled"]).lower() + "}"
        for item in entries) + "]"


async def managed_executable(spec, native, *, require_matching_tui=False):
    """Resolve the verified server independently of the user's optional TUI.

    ACP runs only this server. Native CLI compatibility matters when opening
    its remote TUI, and is also rechecked by the scoped launcher at handoff.
    """
    build = json.loads((Path(__file__).parent.parent / 'codex/build.json').read_text())
    server = resolve_managed_binary(spec, engine_key='codex', source_version=build['sourceVersion'],
        patch_version=build['patchVersion'], expected_source_commit=build['sourceCommit'],
        expected_patch_hash=build['patchHash'])
    resolve_companion(server, build['sourceVersion'], HOST)
    if require_matching_tui:
        native = matching_tui(server, build['sourceVersion'], native)
        from .native_catalog import query
        version = (await query(spec, (native, '--version'))).strip()
        if version != 'codex-cli ' + build['sourceVersion']:
            raise UnsupportedCapabilityError('Codex 终端与项目技能运行组件不匹配，请更新运行组件')
    return server, build


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout="roots", dry_run=dry_run)
        plan.spec = spec
        return plan
    name, version = runtime_info or (None, None)
    parsed = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if name != "@agentclientprotocol/codex-acp" or not parsed or tuple(map(int, parsed.groups())) < (1, 10, 0):
        return unsupported(binding, "当前适配器未提供已验证的技能加载目录", dry_run=dry_run)
    executable = _executable(spec)
    if not executable or os.name == "nt":
        return unsupported(binding, "项目技能范围需要可用的原生 CLI 加载器", dry_run=dry_run)
    try:
        server, build = await managed_executable(spec, executable)
    except (OSError, ValueError, UnsupportedCapabilityError) as exc:
        return unsupported(binding, str(exc), dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout="roots", dry_run=dry_run)
    if plan.root is None:
        return plan
    # The private loader reads exact selected paths itself. ACP additionalRoots
    # also grants write access, so never use that channel for Skill discovery.
    plan.metadata = {}
    if dry_run:
        plan.spec = spec
        return plan
    expected = {str(path.resolve()) for path in plan.paths}
    entries = [{"path": path, "enabled": True} for path in sorted(expected)]
    helper = Path(__file__).parent.parent / 'codex/scoped_launcher.py'
    command = (sys.executable, str(helper), '--native', executable, '--server', str(server),
               '--allowed', json.dumps(sorted(expected)), '--version', build['sourceVersion'],
               '--server-hash', file_hash(server), '--')
    script = '#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n'
    launcher = plan.root / ("native-launch-" + hashlib.sha256(script.encode()).hexdigest()[:24] + ".sh")
    from drivers.projection_store import atomic_write_text
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_text() != script:
            raise UnsupportedCapabilityError("原生技能启动配置不一致")
    else:
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    verified = await native_inventory(str(launcher), spec, workspace_root or spec.cwd or os.getcwd(),
                                      plan.root, expected=expected)
    if expected != verified:
        raise UnsupportedCapabilityError("原生运行时未加载完整项目技能集合")
    previous = spec.build_env().get("CODEX_CONFIG")
    config = json.loads(previous) if previous else {}
    if not isinstance(config, dict):
        raise ValueError("原生配置覆盖必须为对象")
    config.pop("skills.config", None)
    config["skills"] = {**config.get("skills", {}), "config": entries}
    overrides = {"CODEX_PATH": str(launcher), "CODEX_CONFIG": json.dumps(config),
                 "KAUS_CODEX_SCOPED_LAUNCHER": str(launcher)}
    provider = config.get("model_provider")
    # The adapter reads CODEX_CONFIG for new threads, but its resume provider
    # resolver reads MODEL_PROVIDER/native config. Preserve an explicit choice.
    if not spec.build_env().get("MODEL_PROVIDER") and isinstance(provider, str) and provider.strip():
        overrides["MODEL_PROVIDER"] = provider.strip()
    plan.spec = spec.with_env(**overrides)
    # The native allowlist is deny-by-default and cannot be widened by source
    # discovery, extra roots, config reloads or plugin changes. This small watch
    # only detects damaged materialized copies; it is not the isolation boundary.
    plan.watch_roots = (plan.root / '.agents/skills',)
    plan.watch_snapshot = await asyncio.to_thread(_source_snapshot, plan)
    return plan


def _source_snapshot(plan):
    rows = []
    for root in getattr(plan, 'watch_roots', ()):
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {'.git', 'node_modules', '__pycache__'}]
            if 'SKILL.md' in files:
                path = Path(directory) / 'SKILL.md'
                try:
                    stat = path.stat()
                    rows.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    rows.append((str(path), None, None))
    return tuple(sorted(set(rows)))


async def validate_turn(plan, cwd, *, only_changed=False):
    snapshot = await asyncio.to_thread(_source_snapshot, plan)
    if only_changed and snapshot == getattr(plan, 'watch_snapshot', None):
        return
    try:
        expected = {str(path.resolve()) for path in plan.paths}
        discovered = await native_inventory(plan.spec.env['CODEX_PATH'], plan.spec, cwd, plan.root, expected=expected)
        if not expected.issubset(discovered):
            raise UnsupportedCapabilityError('项目技能副本不完整')
    except UnsupportedCapabilityError as exc:
        raise UnsupportedCapabilityError('技能来源已更新，请重新应用项目配置后继续') from exc
    plan.watch_snapshot = snapshot


async def watch(plan, cwd):
    while True:
        await asyncio.sleep(.5)
        await validate_turn(plan, cwd, only_changed=True)


def cli_command(plan, command):
    import sys
    launcher = plan.spec.env.get("CODEX_PATH") if plan.spec else None
    if not launcher or not Path(launcher).is_file():
        raise UnsupportedCapabilityError("项目技能启动配置缺失")
    if not getattr(plan, 'terminal_verified', False):
        try:
            help_text = subprocess.run([launcher, '--help'], env=plan.spec.build_env(),
                capture_output=True, text=True, timeout=5, check=True).stdout
            server_help = subprocess.run([launcher, 'app-server', '--help'], env=plan.spec.build_env(),
                capture_output=True, text=True, timeout=5, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            raise UnsupportedCapabilityError('当前原生版本未提供可验证的终端续接接口')
        if '--remote' not in help_text or 'unix://' not in server_help:
            raise UnsupportedCapabilityError('当前原生版本未提供项目技能终端续接接口')
        plan.terminal_verified = True
    return (sys.executable, str(Path(__file__).with_name("codex_terminal.py")),
            "--launcher", launcher, "--root", str(plan.root), "--", *command[1:]) if command else ()


def external_cli_command(plan, command, *, conversation, binding, spec):
    """Apply cold conversation edits before handing the same thread to the TUI."""
    settings = {}
    model = conversation.model_id
    effort = conversation.reasoning_mode
    if model:
        match = re.fullmatch(r'(.+)\[([^\[\]]+)\]', model)
        if match:
            model, embedded = match.groups()
            effort = effort or embedded
        settings['model'] = model
    if effort:
        settings['config'] = {'model_reasoning_effort': effort}
    mode = conversation.approval_mode
    policies = {
        'ask': ('on-request', 'user', 'workspace-write'),
        'auto': ('on-request', 'auto_review', 'workspace-write'),
        'bypass': ('never', 'user', 'danger-full-access'),
        'native:read-only': ('on-request', 'user', 'workspace-write'),
        'native:agent': ('on-request', 'auto_review', 'workspace-write'),
        'native:agent-full-access': ('never', 'user', 'danger-full-access'),
    }
    if mode:
        if mode not in policies:
            raise UnsupportedCapabilityError('当前权限模式无法在 Codex 终端中保持')
        settings['approvalPolicy'], settings['approvalsReviewer'], settings['sandbox'] = policies[mode]
    if plan is None or plan.root is None:
        args = []
        if model:
            args += ['-m', model]
        if effort:
            args += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        if mode:
            args += ['-a', settings['approvalPolicy'], '-s', settings['sandbox'],
                     '-c', 'approvals_reviewer=' + json.dumps(settings['approvalsReviewer'])]
        return (*command[:1], *args, *command[1:])
    wrapped = cli_command(plan, command)
    if not settings:
        return wrapped
    if not conversation.native_session_id:
        raise UnsupportedCapabilityError('Codex 会话尚未建立，无法在终端中保持当前设置')
    settings['threadId'] = conversation.native_session_id
    index = wrapped.index('--')
    return (*wrapped[:index], '--thread-settings', json.dumps(settings), *wrapped[index:])


async def load_native_history(spec, binding, native_session_id):
    from drivers.codex.native_history import load_native_history as load
    return await load(spec, binding, native_session_id)


async def prepare_runtime_spec(spec, conversation, metadata):
    """Materialize fresh external sessions without changing native discovery."""
    if not metadata.get('_kausExternalPrecreate') or conversation.native_session_id:
        return spec
    env = spec.build_env()
    if env.get('KAUS_CODEX_SCOPED_LAUNCHER') == env.get('CODEX_PATH') and env.get('CODEX_PATH'):
        return spec
    native = _executable(spec)
    if not native:
        raise UnsupportedCapabilityError('Codex 原生程序不可用')
    server, build = await managed_executable(spec, native, require_matching_tui=True)
    from .common import state_root
    from drivers.projection_store import atomic_write_text
    helper = Path(__file__).parent.parent / 'codex/scoped_launcher.py'
    command = (sys.executable, str(helper), '--native', native, '--server', str(server),
        '--allowed', 'null', '--version', build['sourceVersion'], '--server-hash', file_hash(server), '--')
    script = '#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n'
    directory = state_root(spec) / 'external-precreate' / 'codex'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = directory / (hashlib.sha256(script.encode()).hexdigest() + '.sh')
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_text() != script:
            raise UnsupportedCapabilityError('原生会话启动配置不一致')
    else:
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    return spec.with_env(CODEX_PATH=str(launcher))


async def load_local_native_history(spec, binding, native_session_id):
    """Filesystem-only full-session reader; never starts a model or gateway."""
    return await load_native_history(spec, binding, native_session_id)
