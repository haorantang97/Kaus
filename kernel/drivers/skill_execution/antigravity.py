"""Selected native Skill roots without moving native identity or history."""
from __future__ import annotations

import asyncio
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from drivers.base import FailureHint, UnsupportedCapabilityError
from drivers.acp.client import AcpRpcError
from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from .antigravity_harness import (binary_hash, diagnostic_secrets, mcp_transport_fingerprint,
                                 read_computer_receipt, read_receipt, redact_diagnostic)
from .common import materialize, selection_of, state_root, unsupported
from .inventory import project_workspaces

PAR_HASH = '9d900b93031fc42397f88206e14eba4193729bbef631a70b18e7a19631a6dfac'
HARNESS_HASH = 'e0a8ef9d80a1ffb178f945159dda33f73d4a5be65516642542352584b834fa2a'
OWNER = 'Kaus Antigravity Skill projections v1\n'
COMPUTER_USE_POLICY_SCOPES = frozenset({'browser'})
# Cover the owned harness's EOF/TERM/KILL budget before ACP group escalation.
GRACEFUL_SHUTDOWN_TIMEOUT = 15.0


def _retry_binary_stamp(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=16)
def _verified_retry_binary(path, stamp, expected_hash):
    # A changed file must never inherit a prior build's pre-session contract.
    return binary_hash(path) == expected_hash and _retry_binary_stamp(Path(path)) == stamp


def _session_start_retry_policy(spec):
    try:
        par, _harness, _env = native_paths(spec)
        if par and par.is_file() and _verified_retry_binary(
                str(par), _retry_binary_stamp(par), PAR_HASH):
            return session_start_retry_delay
    except (OSError, ValueError, TypeError):
        pass
    return None


async def session_start_retry_policy(spec):
    """Bind the known pre-allocation retry contract to the binary being launched."""
    return await asyncio.to_thread(_session_start_retry_policy, spec)


def session_start_retry_delay(error, stderr: str, attempt: int) -> float | None:
    """Retry only the observed OAuth TLS failure before native session creation.

    In 1.1.1, new_session awaits authentication before allocating its session or
    harness. TransportError escapes as -32603; it is not an invalid credential.
    Require the current request's stack, not an earlier error in the stderr ring.
    """
    if error.code != -32603:
        return None
    required = ('in new_session', 'await self._assert_authenticated()',
                'in _assert_authenticated', 'in _ensure_oauth_logged_in',
                'google.auth.exceptions.TransportError:', 'oauth2.googleapis.com',
                'SSLEOFError', 'EOF occurred in violation of protocol')
    if not all(marker in stderr for marker in required):
        return None
    if any(marker in stderr for marker in ('invalid_grant', 'RefreshError', 'CERTIFICATE_VERIFY_FAILED')):
        return None
    error.failure = FailureHint(code='connection_interrupted', message='连接暂时中断', hint='请稍后重试')
    if attempt >= 2:
        return None
    return (0.5, 1.0)[attempt]


def native_paths(spec):
    env, args = spec.build_env(), list(spec.command)
    if args and Path(args[0]).name == 'env':
        args.pop(0)
        while args and '=' in args[0] and not args[0].startswith('-'):
            key, value = args.pop(0).split('=', 1)
            env[key] = value
    par = shutil.which(args[0], path=env.get('PATH')) if args else None
    harness = env.get('ANTIGRAVITY_HARNESS_PATH')
    if not harness and par:
        harness = str(Path(par).parent / 'localharness_external')
    return Path(par).resolve() if par else None, Path(harness).expanduser().resolve() if harness else None, env


def immutable(path, text):
    if path.exists():
        if path.is_symlink() or path.read_text() != text:
            raise UnsupportedCapabilityError('项目技能执行目录归属不匹配')
    else:
        atomic_write_text(path, text)


def owned_copy(plan, workspace):
    """The ACP safe_view_file allows cwd; use a private ignored subdirectory."""
    workspace = Path(workspace).resolve(strict=True)
    parent = workspace / '.kaus'
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise UnsupportedCapabilityError('工作区技能目录不可用')
    parent.mkdir(exist_ok=True, mode=0o700)
    owned = parent / 'skill-projections'
    if owned.exists():
        if owned.is_symlink() or (owned / '.kaus-owner').is_symlink() or not (owned / '.kaus-owner').is_file() or (owned / '.kaus-owner').read_text() != OWNER:
            raise UnsupportedCapabilityError('工作区技能目录已有其他内容')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.kaus-skills-', dir=parent))
        try:
            (stage / '.kaus-owner').write_text(OWNER)
            (stage / '.gitignore').write_text('*\n')
            try:
                stage.rename(owned)
            except FileExistsError:
                if (owned / '.kaus-owner').read_text() != OWNER:
                    raise UnsupportedCapabilityError('工作区技能目录归属不匹配')
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    immutable(owned / '.gitignore', '*\n')
    directory = owned / plan.root.parent.name / plan.root.name
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    if directory.parent.is_symlink() or directory.is_symlink():
        raise UnsupportedCapabilityError('工作区技能目录包含链接')
    marker = (plan.root / '.kaus-skill-selection.json').read_text()
    if directory.exists():
        entry = directory / '.kaus-skill-selection.json'
        if entry.is_symlink() or not entry.is_file() or entry.read_text() != marker:
            raise UnsupportedCapabilityError('工作区技能副本归属不匹配')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=directory.parent))
        try:
            for path in plan.paths:
                shutil.copytree(path.parent, stage / path.parent.name)
            (stage / '.kaus-skill-selection.json').write_text(marker)
            try:
                stage.rename(directory)
            except FileExistsError:
                immutable(directory / '.kaus-skill-selection.json', marker)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # A source package containing another SKILL would add an unselected native
    # registration. Reject it rather than dropping arbitrary package resources.
    expected = {str(path.parent.name + '/SKILL.md') for path in plan.paths}
    actual = {str(path.relative_to(directory)) for path in directory.rglob('SKILL.md')}
    if actual != expected:
        raise UnsupportedCapabilityError('技能包包含额外技能入口')
    expected_files = {'.kaus-skill-selection.json': hashlib.sha256(marker.encode()).hexdigest()}
    for source in plan.paths:
        for item in source.parent.rglob('*'):
            if item.is_symlink():
                raise UnsupportedCapabilityError('技能执行目录包含链接')
            if item.is_file():
                expected_files[str(Path(source.parent.name) / item.relative_to(source.parent))] = binary_hash(item)
    files = {}
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise UnsupportedCapabilityError('技能执行目录包含链接')
        if path.is_file():
            files[str(path.relative_to(directory))] = binary_hash(path)
    if files != expected_files:
        raise UnsupportedCapabilityError('工作区技能副本已变化')
    return directory, files


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        return materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    par, harness, _ = native_paths(spec)
    if os.name != 'posix' or not par or not harness or not par.is_file() or not harness.is_file():
        return unsupported(binding, '当前安装缺少可验证的原生技能加载入口', dry_run=dry_run)
    if binary_hash(par) != PAR_HASH or binary_hash(harness) != HARNESS_HASH:
        return unsupported(binding, '当前引擎版本的技能加载接口需要适配', dry_run=dry_run)
    workspace = workspace_root or spec.cwd
    if not workspace or not Path(workspace).is_dir():
        return unsupported(binding, '项目技能需要可写的本地工作区', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if dry_run:
        return plan
    try:
        owned, files = owned_copy(plan, workspace)
    except OSError as exc:
        raise UnsupportedCapabilityError('无法建立工作区技能副本') from exc
    scope = {'harness': str(harness), 'harnessHash': HARNESS_HASH, 'ownedRoot': str(owned), 'files': files,
             'roots': [str(owned / path.parent.name) for path in plan.paths], 'names': list(plan.names)}
    plan.spec = wrap_harness(spec, scope, plan.root)
    plan.metadata['workspaceSkillProjection'] = str(owned)
    return plan


def wrap_harness(spec, scope, root):
    encoded = json.dumps(scope, sort_keys=True)
    manifest = root / ('antigravity-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    immutable(manifest, encoded)
    source = Path(__file__).with_name('antigravity_harness.py').read_text()
    proxy = root / ('harness-proxy-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.py')
    immutable(proxy, source)
    text = '#!/bin/sh\nexec ' + ' '.join(shlex.quote(v) for v in (sys.executable, str(proxy), str(manifest))) + '\n'
    launcher = root / ('harness-launch-' + hashlib.sha256(text.encode()).hexdigest()[:24] + '.sh')
    immutable(launcher, text)
    launcher.chmod(0o700)
    command = tuple('ANTIGRAVITY_HARNESS_PATH=' + str(launcher)
                    if arg.startswith('ANTIGRAVITY_HARNESS_PATH=') else arg for arg in spec.command)
    return spec.model_copy(update={'command': command}).with_env(
        ANTIGRAVITY_HARNESS_PATH=str(launcher), KAUS_AGY_SCOPE_MANIFEST=str(manifest))


async def prepare_runtime_spec(spec, conversation, metadata):
    """Observe native MCP failure for both native and managed Skill modes."""
    par, harness, env = native_paths(spec)
    manifest = env.get('KAUS_AGY_SCOPE_MANIFEST')
    try:
        if manifest:
            path = Path(manifest)
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('Invalid native scope')
            scope = json.loads(path.read_text())
            harness = Path(scope['harness'])
        else:
            scope = {'harness': str(harness), 'harnessHash': HARNESS_HASH, 'nativeSkills': True}
        verified = (os.name == 'posix' and par and harness and par.is_file() and harness.is_file()
                    and binary_hash(par) == PAR_HASH and binary_hash(harness) == HARNESS_HASH
                    and scope.get('harnessHash') == HARNESS_HASH)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UnsupportedCapabilityError('无法验证原生 MCP 运行接口') from exc
    if not verified:
        if manifest or metadata.get('mcpServers') or metadata.get('_kausComputerUse'):
            raise UnsupportedCapabilityError('当前引擎版本的 MCP 运行接口需要适配')
        return spec
    from drivers.computer_use.integration import server_fingerprint
    plan = metadata.setdefault('_kausComputerUse', {})
    browser_enabled = plan.get('controls', {}).get('browser', True) is True
    automatically_injected = []
    for entry in plan.get('autoInjectedServers', []):
        server = next((value for value in metadata.get('mcpServers', [])
                       if value.get('name') == entry.get('name')
                       and server_fingerprint(value) == entry.get('fingerprint')), None)
        if server is not None:
            automatically_injected.append({'name': server['name'],
                'capabilities': list(entry.get('capabilities', [])),
                'transportFingerprint': mcp_transport_fingerprint(server)})
    scope = {**scope, 'computerUse': {'browserEnabled': browser_enabled,
                                    'autoFallbacks': automatically_injected}}
    # Enterprise administration can enable its browser while a prompt is
    # rebuilding the native harness. Reserve before the prompt, not after that
    # first tool has already been exposed. The proxy reapplies policy on every
    # harness initialization, including model and administrator changes.
    resources = set(plan.get('resources', []))
    if browser_enabled:
        resources.update(('browser:shared', 'desktop:local'))
    plan['resources'] = sorted(resources)
    plan.setdefault('browser', {'status': 'unavailable'})
    metadata['_kausAntigravityBaseBrowser'] = dict(plan['browser'])
    parent = state_root(spec) / 'antigravity-runtime'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='runtime-', dir=parent))
    return wrap_harness(spec, scope, root).with_env(
        KAUS_AGY_MCP_RECEIPT=str(root / 'mcp-receipt.json'),
        KAUS_AGY_COMPUTER_RECEIPT=str(root / 'computer-receipt.json'))


def _refresh_computer_selection(spec, metadata, *, required=False):
    path = spec.build_env().get('KAUS_AGY_COMPUTER_RECEIPT') if spec else None
    if not path:
        return
    try:
        receipt = read_computer_receipt(path)
    except (OSError, ValueError) as exc:
        raise AcpRpcError('computer_use.receipt_failed', '无法核验电脑工具配置') from exc
    if receipt is None:
        if required:
            raise AcpRpcError('computer_use.receipt_failed', '无法核验电脑工具配置')
        return
    plan = metadata.get('_kausComputerUse', {})
    value = dict(metadata.get('_kausAntigravityBaseBrowser', {'status': 'unavailable'}))
    if receipt['browserDisabled']:
        value = {'status': 'disabled', 'exposed': False, 'requirements': ['project-disabled']}
    elif receipt['nativeBrowser']:
        # Registration is proven by the native initialization frame. It does
        # not itself prove the optional upstream MCP finished loading.
        value = {'source': 'native', 'provider': 'antigravity-native', 'status': 'unknown',
                 'exposed': True, 'verified': False, 'requirements': ['native_browser_registered'],
                 'resource': 'browser:shared'}
    selection = plan.setdefault('browser', {})
    selection.clear()
    selection.update(value)
    plan['suppressedAutoServers'] = list(receipt['removedAutoServers'])


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    _refresh_computer_selection(spec, metadata, required=True)


def runtime_receipt(spec):
    path = spec.build_env().get('KAUS_AGY_MCP_RECEIPT') if spec else None
    if not path:
        return None
    try:
        return read_receipt(path)
    except (OSError, ValueError) as exc:
        raise AcpRpcError('mcp.receipt_failed', '无法核验 MCP 运行状态', {'reason': str(exc)}) from exc


async def before_runtime_prompt(connection, spec, conversation, metadata, session_id, params):
    _refresh_computer_selection(spec, metadata)
    # The verified ACP server handles this exact control command before invoking
    # the harness. Its successful RPC response has no trajectory state event.
    blocks = params.get('prompt', [])
    if (blocks and all(isinstance(block, dict) and block.get('type') == 'text' for block in blocks)
            and ''.join(str(block.get('text', '')) for block in blocks).strip() == '/logout'):
        return {'control': 'logout'}
    receipt = runtime_receipt(spec)
    return receipt['sequence'] if receipt else None


async def after_runtime_prompt(connection, spec, conversation, metadata, session_id, params, result, token):
    _refresh_computer_selection(spec, metadata)
    if token == {'control': 'logout'}:
        return {'auth_state': 'unknown'}
    if token is None:
        return
    receipt = runtime_receipt(spec)
    fresh = receipt and receipt['sequence'] > token and receipt.get('trajectoryId') == session_id
    if not fresh:
        if isinstance(result, dict) and result.get('stopReason') == 'end_turn':
            raise AcpRpcError('mcp.receipt_failed', '无法核验引擎运行结果')
        return
    error = redact_diagnostic(receipt['error'], diagnostic_secrets(metadata))
    if error.startswith('MCP load failed for '):
        server = error.removeprefix('MCP load failed for ').split(':', 1)[0]
        raise AcpRpcError('mcp.server_failed', 'MCP 服务器初始化失败', {'server': server, 'reason': error})
    if error:
        raise AcpRpcError('agent.execution_failed', '引擎运行失败', {'reason': error})


def inventory_roots(spec, projects, bindings):
    projects = tuple(projects)
    root = next((p.id for p in projects if p.slug == 'default'), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if not root:
        return ()
    _, _, env = native_paths(spec)
    home = Path(env.get('GEMINI_HOME') or Path(env.get('HOME', str(Path.home()))) / '.gemini').expanduser()
    result = [NativeSkillRoot(home / 'config/skills', 'antigravity', 'Antigravity', root),
              NativeSkillRoot(home / 'antigravity-cli/skills', 'antigravity', 'Antigravity', root)]
    for project_id, workspace in project_workspaces(projects, bindings, 'antigravity'):
        result.extend(NativeSkillRoot(workspace / folder, 'antigravity', 'Antigravity', project_id)
                      for folder in ('.gemini/skills', '.agents/skills'))
    return tuple(result)


def cli_command(plan, command):
    raise UnsupportedCapabilityError('当前引擎尚未提供原生终端续接入口')
