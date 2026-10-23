"""Command Code's explicit native Skill paths with discovery disabled.

The host bridge keeps its own conversation id. Only the native subprocess is
wrapped; authentication, project configuration and bridge state keep their homes.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import shlex
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, unsupported
from drivers.session_mcp import projection as _mcp

remember_session_options = _mcp.remember


async def prepare_runtime_spec(spec, conversation, metadata):
    return await _mcp.prepare(spec, conversation, metadata, kind="commandcode")


def inventory_roots(spec, projects, bindings):
    from drivers.skill_inventory import NativeSkillRoot, discover_skills
    from .inventory import _read, project_workspaces
    from .node_package import locate
    projects = tuple(projects)
    root_id = next((p.id for p in projects if p.slug == 'default'), None)
    root_id = root_id or next((p.id for p in projects if not p.parent_project_id), None)
    if not root_id:
        return ()
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home()).expanduser()
    command = list(spec.command)
    executable = command[command.index('--command') + 1] if '--command' in command and command.index('--command') + 1 < len(command) else shutil.which('command-code', path=env.get('PATH'))
    package = locate(spec.model_copy(update={'command': (executable,)}), 'command-code') if executable else None
    user = _read(home / '.commandcode/settings.json')
    workspaces = project_workspaces(projects, bindings, 'commandcode')
    settings = {pid: [user, _read(cwd / '.commandcode/settings.json'), _read(cwd / '.commandcode/settings.local.json')]
                for pid, cwd in workspaces}
    settings[root_id] = [user]
    roots = [(root_id, home / '.commandcode/skills'), (root_id, home / '.agents/skills')]
    if package:
        roots.append((root_id, package / 'dist/bundled'))
    for pid, cwd in workspaces:
        roots.extend(((pid, cwd / '.commandcode/skills'), (pid, cwd / '.agents/skills')))
    for pid, cwd in ((root_id, home), *workspaces):
        extra = []
        for config in settings.get(pid, [user]):
            if isinstance(config.get('skills'), list):
                extra = config['skills']
        for raw in extra:
            if isinstance(raw, str) and raw.strip():
                path = home / raw[2:] if raw.startswith('~/') else Path(raw)
                roots.append((pid, path if path.is_absolute() else cwd / path))
    disabled = {pid: {name for config in layers for name in (config.get('disabledSkills') if isinstance(config.get('disabledSkills'), list) else []) if isinstance(name, str)}
                for pid, layers in settings.items()}
    result = []
    for pid, folder in roots:
        entries = discover_skills((NativeSkillRoot(folder, 'commandcode', 'Command Code', pid),))
        for item in entries:
            blocked = tuple(project.id for project in projects
                if item['name'] in disabled.get(project.id, disabled.get(root_id, set())))
            result.append(NativeSkillRoot(Path(item['sourcePath']).parent, 'commandcode', 'Command Code', pid,
                                          blocked_project_ids=blocked))
    return tuple(result)


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    command = list(spec.command)
    try:
        index = command.index('--command')
        executable = command[index + 1]
    except ValueError:
        index = None
        executable = shutil.which('command-code', path=spec.build_env().get('PATH'))
    except IndexError:
        return unsupported(binding, '原生 CLI 启动参数不完整', dry_run=dry_run)
    if not executable or os.name == 'nt':
        return unsupported(binding, '项目技能范围需要可用的原生 CLI', dry_run=dry_run)
    try:
        process = await asyncio.create_subprocess_exec(executable, '--help', env=spec.build_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError('原生 CLI 检测超时')
        if process.returncode or b'--no-skills' not in output or b'--skill <path>' not in output:
            raise ValueError('当前原生版本未提供技能加载范围参数')
    except (OSError, ValueError) as exc:
        return unsupported(binding, str(exc), dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    args = [str(Path(executable).absolute()), '--no-skills']
    for path in plan.paths:
        # The native extra-path loader accepts Skill directories.
        args.extend(('--skill', str(path.parent)))
    script = '#!/bin/sh\nexec ' + shlex.join(args) + ' "$@"\n'
    launcher = plan.root / ('native-launch-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_text() != script:
            raise UnsupportedCapabilityError('原生技能启动配置不一致')
    else:
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    if index is None:
        command.extend(('--command', str(launcher)))
    else:
        command[index + 1] = str(launcher)
    plan.spec = spec.model_copy(update={'command': tuple(command)})
    return plan


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    spec = _mcp.restore_native_spec(plan.spec, kind='commandcode') if plan.spec else None
    argv = list(spec.command) if spec else []
    try:
        path = Path(argv[argv.index('--command') + 1])
    except (ValueError, IndexError):
        raise UnsupportedCapabilityError('项目技能启动配置缺失') from None
    if not path.is_file() or path.is_symlink() or path.name != 'native-launch-' + hashlib.sha256(path.read_bytes()).hexdigest()[:24] + '.sh':
        raise UnsupportedCapabilityError('项目技能启动配置已变化')
    return (str(path), *command[1:]) if command else ()


def _saved_session(spec, conversation, binding):
    import json
    import stat
    import uuid
    ident = conversation.native_session_id
    try:
        if str(uuid.UUID(ident)) != ident:
            raise ValueError
        argv = list(spec.command)
        if '--state-dir' in argv:
            directory = Path(argv[argv.index('--state-dir') + 1]).expanduser()
        else:
            directory = Path(spec.build_env().get('HOME') or Path.home()) / '.local/share/kaus/commandcode-bridge'
        file = directory / (ident + '.json')
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024 * 1024:
                raise ValueError
            data = json.load(stream)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError
        workspace = (binding.runtime_config.get('workspace_root') if binding else None) or spec.cwd
        if data.get('version') != 1 or data.get('id') != ident or not workspace or Path(data['cwd']).resolve() != Path(workspace).resolve():
            raise ValueError
        if str(uuid.UUID(data['nativeId'])) != data['nativeId']:
            raise ValueError
        if any(not isinstance(data.get(key), str) for key in ('model', 'effort', 'mode')):
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise UnsupportedCapabilityError('原生会话映射不可用，请先在对话中完成一轮') from None


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, selected
    saved = _saved_session(spec, conversation, binding)
    mode = approval(conversation) or saved.get('mode')
    mode = choice(mode, {'plan': 'plan', 'read_only': 'plan', 'bypass': 'yolo', 'yolo': 'yolo'}, '审批模式')
    if selected(conversation, 'execution_mode'):
        raise UnsupportedCapabilityError('原生终端未提供当前运行模式')
    model = selected(conversation, 'model_id', 'model') or saved.get('model')
    if model == '__native_default__':
        model = None
    effort = selected(conversation, 'reasoning_mode', 'effort') or saved.get('effort')
    effort = choice(effort, {'default': None, 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh', 'max': 'max'}, '思考强度')
    if model is not None and (not isinstance(model, str) or not model or '\x00' in model or '\n' in model):
        raise UnsupportedCapabilityError('终端模型配置无效')
    command = (command[0], '--resume', saved['nativeId'], '--no-auto-update', '--skip-onboarding')
    command = arguments(command, ('--model', model), ('--effort', effort), ('--permission-mode', mode))
    command = cli_command(plan, command) if plan is not None else command
    return _mcp.terminal(spec, conversation, command, kind="commandcode")


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from types import SimpleNamespace
    from .history_io import canonical_id, read_bytes, tree_history
    ident = canonical_id(native_session_id)
    root = Path(spec.build_env().get('HOME') or Path.home()) / '.commandcode/projects'
    try:
        saved = _saved_session(spec, SimpleNamespace(native_session_id=ident), binding)
        native = canonical_id(saved['nativeId'])
        # The CLI's Unicode slugifier is not a stable API. Find only this exact
        # UUID one directory below its transcript root, then verify header/cwd.
        candidates = [p / (native + '.jsonl') for p in root.iterdir()
                      if not p.is_symlink() and p.is_dir() and (p / (native + '.jsonl')).exists()]
        if len(candidates) != 1:
            raise ValueError('ambiguous native transcript')
        return tree_history(read_bytes(root, candidates[0].relative_to(root)), ident=native,
                            cwd=saved['cwd'], public_id=ident)
    except (OSError, ValueError, TypeError, UnsupportedCapabilityError):
        raise RuntimeError('原生会话历史暂不可读') from None
