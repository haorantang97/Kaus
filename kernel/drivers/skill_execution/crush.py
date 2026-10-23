"""Project selection through the original HTTP bridge and native Skill loader."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

from drivers.base import UnsupportedCapabilityError
from drivers.crush_bridge.skills import inspect
from drivers.crush_bridge.protocol import BridgeError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported


async def refresh_inventory(spec):
    from .crush_inventory import refresh
    await refresh(spec)


def inventory_roots(spec, projects, bindings):
    from .crush_inventory import roots
    return roots(spec, projects, bindings)


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        return materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    command = spec.command
    executable = command[command.index('--crush-command') + 1] if '--crush-command' in command else 'crush'
    native = shutil.which(executable, path=spec.build_env().get('PATH'))
    if not native:
        return unsupported(binding, '原生引擎尚未安装', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if dry_run:
        return plan
    try:
        rows = await inspect(native, workspace_root or spec.cwd, spec.build_env(), plan.paths)
    except (OSError, BridgeError) as exc:
        raise UnsupportedCapabilityError('原生技能目录验证失败：' + str(exc)) from None
    if not set(map(str, plan.paths)).issubset({row['id'] for row in rows}):
        raise UnsupportedCapabilityError('原生引擎未加载完整的项目技能')
    encoded = json.dumps({'executable': str(Path(native).resolve()),
        'binaryHash': hashlib.sha256(Path(native).read_bytes()).hexdigest(),
        'paths': list(map(str, plan.paths))}, sort_keys=True)
    manifest = plan.root / ('crush-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    if manifest.exists():
        if manifest.is_symlink() or manifest.read_text() != encoded:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(manifest, encoded)
    # Use the exact executable verified by the projection, including custom paths.
    if '--crush-command' not in command:
        command = (*command, '--crush-command', native)
    plan.spec = spec.model_copy(update={'command': command}).with_env(KAUS_CRUSH_SKILL_SCOPE=str(manifest))
    return plan


def cli_command(plan, command):
    if plan and plan.root and not plan.spec.env.get('KAUS_CRUSH_SKILL_SCOPE'):
        raise UnsupportedCapabilityError('项目技能终端配置不可用')
    return tuple(command)


def remember_session_options(spec, conversation, metadata):
    from .bridge_terminal import remember
    remember(spec, conversation, metadata)


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .bridge_terminal import manifest, record
    from .terminal_settings import approval, choice, environment, selected
    cli_command(plan, command)
    active = plan.spec if plan else spec
    mode = selected(conversation, 'execution_mode', 'mode')
    # Native 0.96.1 TUI always initializes its mode to coder. Do not show a
    # misleading Code label while the server is executing the Plan agent.
    choice(mode, ('coder',), '运行模式')
    value = record(spec, conversation, binding, 'crush-bridge')
    value.update(executable=command[0], model=selected(conversation, 'model_id', 'model'),
                 reasoning=selected(conversation, 'reasoning_mode', 'reasoning'),
                 permission=choice(approval(conversation), {'ask':'ask', 'host:ask':'ask', 'bypass':'bypass'}, '审批方式'))
    path = manifest(spec, conversation, value)
    result = (sys.executable, str(Path(__file__).with_name('crush_terminal.py')), str(path))
    scope = active.build_env().get('KAUS_CRUSH_SKILL_SCOPE')
    return environment(spec, conversation, result, {'KAUS_CRUSH_SKILL_SCOPE': scope} if scope else {})


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    import asyncio
    from .crush_history import read
    return await asyncio.to_thread(read, spec, binding, native_session_id)
