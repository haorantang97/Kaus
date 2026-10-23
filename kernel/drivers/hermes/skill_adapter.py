"""Direct ACP uses the same private home and conversation skill snapshots."""
from pathlib import Path
import json
import os

from drivers.hermes import project_scope, skill_projection
from drivers.hermes.skill_launch import command_for
from drivers.hermes.skill_snapshots import pin_session, session_mode
from drivers.skill_execution.common import SkillPlan, unsupported

COMPUTER_USE_POLICY_SCOPES = frozenset({"browser", "desktop"})


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    native_env = spec.build_env()
    native_root = Path(native_env.get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
    source = native_root
    if binding.native_scope_ref and binding.native_scope_ref != 'default':
        source = source / 'profiles' / binding.native_scope_ref
    home = project_scope.home_for(source, binding)
    if not dry_run:
        project_scope.prepare(source, source, binding, refresh=True)
    report = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=dry_run)
    if report.unsupported and not dry_run:
        return unsupported(binding, report.unsupported[0].detail or '项目技能无法应用')
    names = tuple(str(entry.after) for entry in report.applied if entry.capability_type != 'skill-selection')
    command = spec.command if dry_run else command_for(spec.command[0], home, ('acp',))
    projected = spec.model_copy(update={
        'command': command, 'env': {**dict(spec.env), 'HERMES_HOME': str(home),
                                 'PATH': native_binary_path(native_env.get('PATH', ''), source, native_root),
                                 'HERMES_ACP_SKIP_CONFIGURED_MCP': '1'}})
    return SkillPlan(report, root=home, names=names,
                     spec=guarded_spec(projected, []) if not dry_run else projected)


def native_binary_path(existing, source, root):
    """Reuse installed native executables after HERMES_HOME becomes private."""
    return os.pathsep.join(dict.fromkeys([*native_binary_dirs(source, root),
                                        *(part for part in existing.split(os.pathsep) if part)]))


def native_binary_dirs(source, root):
    return tuple(dict.fromkeys(str(home / 'bin') for home in (source, root) if (home / 'bin').is_dir()))


async def project_session(spec, binding, effective, workspace_root, *, conversation, dry_run=False, runtime_info=None):
    plan = await project(spec, binding, effective, workspace_root, dry_run=dry_run, runtime_info=runtime_info)
    if dry_run:
        return plan
    previous = session_mode(plan.root, conversation.id)
    scope = pin_session(plan.root, conversation.id,
        resume=bool(conversation.native_session_id) and previous != 'native')
    session_mode(plan.root, conversation.id, mode='managed')
    plan.spec = plan.spec.with_env(KAUS_HERMES_SKILL_SCOPE=str(scope))
    return plan


def cli_command(plan, command):
    return tuple(command)


def remember_session_options(spec, conversation, metadata):
    from .terminal_settings import remember_mcp
    home = Path(spec.build_env().get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
    remember_mcp(home, conversation, metadata.get('mcpServers', ()))


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import command_for as with_settings
    from drivers.skill_execution.terminal_settings import environment
    launch_spec = plan.spec if plan is not None and plan.spec is not None else spec
    home = Path(launch_spec.build_env().get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
    scope = launch_spec.env.get('KAUS_HERMES_SKILL_SCOPE')
    native = command[0]
    command = command_for(native, home, tuple(command[1:]), scope=scope)
    command = with_settings(home, conversation, binding, command, native)
    values = {'HERMES_HOME': str(home)}
    if scope:
        values['KAUS_HERMES_SKILL_SCOPE'] = scope
    return environment(spec, conversation, command, values)


async def load_native_history(spec, binding, native_session_id):
    import sqlite3
    try:
        return await _load_native_history(spec, binding, native_session_id)
    except (OSError, ValueError, sqlite3.Error):
        raise RuntimeError('无法读取 Hermes 原生记录，请重试') from None


async def _load_native_history(spec, binding, native_session_id):
    import asyncio
    from .history import read_full_history_from_db
    source = Path(spec.build_env().get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
    if binding.native_scope_ref and binding.native_scope_ref != 'default':
        source = source / 'profiles' / binding.native_scope_ref
    private = project_scope.home_for(source, binding)
    # A direct managed session and an original native-mode session may both
    # belong to one binding. Resolve the exact ID, never the newest DB/session.
    for home in (private, source):
        path = home / 'state.db'
        if not path.is_file():
            continue
        history = await asyncio.to_thread(read_full_history_from_db, path, native_session_id)
        if "history" not in history.missing:
            entries = tuple(row.model_copy(update={'metadata': {}}) for row in history.entries
                if row.role in ('user', 'assistant') and row.text)
            missing = set(history.missing)
            if len(entries) != len(history.entries):
                missing.add('tool_records')
            return history.model_copy(update={'entries': entries, 'missing': tuple(sorted(missing)),
                                             'complete': history.complete and not missing})
    raise FileNotFoundError('native_history_unavailable')


async def prepare_runtime_spec(spec, conversation, metadata):
    """Validate the actual native MCP connections before accepting a session."""
    from .computer_use import prepare_spec
    spec = prepare_spec(spec, metadata)
    names = [item['name'] for item in metadata.get('mcpServers', ()) if item.get('name')]
    return guarded_spec(spec, names)


async def probe_computer_use(spec, binding=None):
    from .computer_use import probe_native
    return await probe_native(spec)


def guarded_spec(spec, names):
    """Apply to metadata launches too: native construction retries discovery."""
    from drivers.base import UnsupportedCapabilityError
    from drivers.hermes.group_execution import hermes_python
    if not names and spec.env.get('HERMES_ACP_SKIP_CONFIGURED_MCP') != '1':
        return spec
    command = spec.command
    launcher = str(Path(__file__).with_name('mcp_launcher.py'))
    if len(command) > 2 and command[1:3] == ('-B', launcher):
        return spec.with_env(KAUS_HERMES_MCP_NAMES=json.dumps(names))
    # Only our two Python launchers or the native Hermes CLI can be composed
    # in-process; an arbitrary shell launcher cannot inherit this guarantee.
    owned = {str(Path(__file__).with_name(name)) for name in ('skill_launcher.py', 'group_launcher.py')}
    if len(command) > 2 and command[1] == '-B' and command[2] in owned:
        python, target, args = command[0], command[2], command[3:]
    elif command and Path(command[0]).name in {'hermes', 'hermes.exe'}:
        python, target, args = hermes_python(spec), 'native', command[1:]
    else:
        raise UnsupportedCapabilityError('此 Hermes 启动器尚未提供 MCP 状态核验')
    if not python:
        raise UnsupportedCapabilityError('无法找到 Hermes 运行环境')
    return spec.model_copy(update={
        'command': (python, '-B', launcher, target, *args),
        'env': {**dict(spec.env), 'KAUS_HERMES_MCP_NAMES': json.dumps(names)},
    })


async def load_local_native_history(spec, binding, native_session_id):
    """Filesystem-only full-session reader; never starts a model or gateway."""
    return await load_native_history(spec, binding, native_session_id)
