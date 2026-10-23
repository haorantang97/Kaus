"""Goose's verified Kaus build; the installed native binary is never replaced."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported
from .managed_builds import resolve_managed_binary

SOURCE_VERSION = '1.52.0'
PATCH_VERSION = 'kaus-skills-1'
BUILD_SOURCE = json.loads((Path(__file__).parents[1]/'goose/skill-scope-source.json').read_text())


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    import re
    import sqlite3
    import tempfile
    import sys
    from datetime import datetime, timezone
    from drivers.base import NativeHistory, NativeHistoryEntry
    from .history_io import read_bytes
    ident = native_session_id
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident):
        raise UnsupportedCapabilityError('原生会话标识无效')
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    override = Path(env['GOOSE_PATH_ROOT']) if env.get('GOOSE_PATH_ROOT') else None
    if override and override.is_absolute():
        root = override / 'data'
    elif sys.platform == 'win32':
        root = Path(env.get('APPDATA') or home / 'AppData/Roaming') / 'Block/goose/data'
    else:
        root = Path(env.get('XDG_DATA_HOME') or home / '.local/share') / 'goose'
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    try:
        relative = Path('sessions/sessions.db')
        # Read a private snapshot, including the WAL. SQLite never opens the
        # user's database, so handoff cannot migrate it or create a sidecar.
        data = read_bytes(root, relative)
        try:
            wal = read_bytes(root, Path(str(relative) + '-wal'))
        except FileNotFoundError:
            wal = None
        if read_bytes(root, relative) != data:
            raise ValueError('native history changed while reading')
        with tempfile.TemporaryDirectory(prefix='kaus-history-') as temp:
            db = Path(temp) / 'sessions.db'; db.write_bytes(data); db.chmod(0o600)
            if wal is not None:
                sidecar = Path(str(db) + '-wal'); sidecar.write_bytes(wal); sidecar.chmod(0o600)
            connection = sqlite3.connect(db)
            try:
                connection.execute('PRAGMA query_only=ON')
                session = connection.execute('SELECT working_dir FROM sessions WHERE id=?', (ident,)).fetchone()
                if not session or not cwd or Path(session[0]).resolve() != Path(cwd).resolve():
                    raise ValueError('native transcript identity mismatch')
                rows = connection.execute('SELECT id,message_id,role,content_json,created_timestamp,metadata_json FROM messages WHERE session_id=? ORDER BY id', (ident,)).fetchall()
            finally:
                connection.close()
        entries = []; omitted = False
        for row_id, message_id, role, content, stamp, metadata in rows:
            meta = json.loads(metadata or '{}')
            if not isinstance(meta, dict) or meta.get('userVisible') is not True or role not in ('user', 'assistant'):
                continue
            parts = json.loads(content)
            text = '\n'.join(p['text'] for p in parts if isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str))
            omitted |= any(not isinstance(p, dict) or p.get('type') != 'text' for p in parts)
            if text:
                # Model response IDs need not be unique; the native table's
                # immutable primary key is the authoritative message identity.
                entries.append(NativeHistoryEntry(entry_id=ident + ':row:' + str(row_id), role=role, text=text,
                    occurred_at=datetime.fromtimestamp(stamp, timezone.utc) if isinstance(stamp, (int, float)) else None))
        return NativeHistory(native_session_id=ident, entries=tuple(entries), complete=not omitted,
                             missing=('non_text_native_entries',) if omitted else ())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError):
        raise RuntimeError('原生会话历史暂不可读') from None


def managed_binary(spec):
    return resolve_managed_binary(spec,engine_key='goose',source_version=SOURCE_VERSION,
        patch_version=PATCH_VERSION,expected_source_commit=BUILD_SOURCE['commit'],
        expected_patch_hash=BUILD_SOURCE['patchSha256'])


async def query(command,spec,cwd):
    process=await asyncio.create_subprocess_exec(*command,env=spec.build_env(),cwd=cwd,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        output,_=await asyncio.wait_for(process.communicate(),20)
    except BaseException:
        if process.returncode is None:process.kill()
        await process.wait()
        raise
    if process.returncode:raise UnsupportedCapabilityError('受管技能加载器未通过验证')
    return output.decode().strip()


def _manifest_valid(plan):
    manifest=Path(plan.spec.env['KAUS_GOOSE_SKILL_MANIFEST'])
    expected=plan.manifest_data
    if manifest.is_symlink() or json.loads(manifest.read_text())!=expected:
        raise UnsupportedCapabilityError('技能范围清单已变化')


async def watch(plan,cwd):
    while True:
        await asyncio.sleep(.5)
        _manifest_valid(plan)


async def validate_turn(plan,cwd):
    _manifest_valid(plan)
    binary=plan.spec.command[0]
    rows=json.loads(await query((binary,'--kaus-skill-scope-list'),plan.spec,cwd))
    expected={(name,str(path.parent.resolve())) for name,path in zip(plan.names,plan.paths)}
    actual={(row['name'],str(Path(row['path']).resolve())) for row in rows}
    if expected!=actual:raise UnsupportedCapabilityError('受管技能加载范围已变化')


async def project(spec,binding,effective,workspace_root,*,dry_run=False,runtime_info=None):
    if selection_of(effective) is None:
        plan=materialize(spec,binding,effective,layout='roots',dry_run=dry_run);plan.spec=spec;return plan
    try:
        binary=managed_binary(spec)
    except (ValueError,OSError,UnsupportedCapabilityError) as exc:
        return unsupported(binding,str(exc),dry_run=dry_run)
    cwd=workspace_root or spec.cwd
    native=await query((spec.command[0],'--version'),spec,cwd)
    if native.split()[-1]!=SOURCE_VERSION:
        return unsupported(binding,'原生版本与受管技能运行组件不匹配，请更新运行组件',dry_run=dry_run)
    capability=json.loads(await query((str(binary),'--kaus-skill-scope-version'),spec,cwd))
    if capability!={'schema':1,'upstreamVersion':SOURCE_VERSION,'patchVersion':PATCH_VERSION}:
        return unsupported(binding,'受管技能运行组件协议不匹配',dry_run=dry_run)
    plan=materialize(spec,binding,effective,layout='roots',dry_run=dry_run);plan.spec=spec
    if plan.root is None or dry_run:return plan
    plan.manifest_data={'schema':1,'paths':[str(path) for path in plan.paths]}
    assets=selection_of(effective).config.get('assets',[])
    docs_source=(binary.parent/'builtin-skills/goose_doc_guide/SKILL.md').resolve()
    docs_ids={a.get('capabilityId') for a in assets if isinstance(a.get('sourcePath'),str) and Path(a['sourcePath']).resolve()==docs_source}
    if docs_ids:
        plan.manifest_data['builtinDocsPaths']=[str(path) for path,item in zip(plan.paths,plan.report.applied) if item.capability_id in docs_ids]
    content=json.dumps(plan.manifest_data,sort_keys=True)
    manifest=plan.root/('goose-'+hashlib.sha256(content.encode()).hexdigest()[:24]+'.json')
    if manifest.exists() and (manifest.is_symlink() or manifest.read_text()!=content):
        raise UnsupportedCapabilityError('技能范围清单不一致')
    if not manifest.exists():atomic_write_text(manifest,content)
    plan.spec=spec.model_copy(update={'command':(str(binary),*spec.command[1:])}).with_env(KAUS_GOOSE_SKILL_MANIFEST=str(manifest))
    await validate_turn(plan,cwd)
    return plan


def cli_command(plan,command):
    if plan.root is None:
        return tuple(command)
    if not command:return ()
    manifest=plan.spec.env['KAUS_GOOSE_SKILL_MANIFEST']
    script='#!/bin/sh\nexec env '+shlex.quote('KAUS_GOOSE_SKILL_MANIFEST='+manifest)+' '+shlex.quote(plan.spec.command[0])+' "$@"\n'
    launcher=plan.root/('goose-terminal-'+hashlib.sha256(script.encode()).hexdigest()[:24]+'.sh')
    if launcher.exists() and (launcher.is_symlink() or launcher.read_text()!=script):
        raise UnsupportedCapabilityError('终端技能配置不一致')
    if not launcher.exists():atomic_write_text(launcher,script)
    launcher.chmod(0o700)
    return (str(launcher),*command[1:])


def inventory_roots(spec,projects,bindings):
    from drivers.skill_inventory import NativeSkillRoot
    from .source_roots import root_project,workspace_roots
    projects=tuple(projects);pid=root_project(projects)
    if not pid:return ()
    env=spec.build_env();home=Path(env.get('HOME') or Path.home())
    override=Path(env['GOOSE_PATH_ROOT']) if env.get('GOOSE_PATH_ROOT') else None
    base=override if override and override.is_absolute() else home
    config=base/'config' if override and override.is_absolute() else Path(env.get('XDG_CONFIG_HOME') or home/'.config')/'goose'
    roots=[NativeSkillRoot(path,'goose','Goose',pid) for path in (home/'.agents/skills',home/'.claude/skills',home/'.config/agents/skills',config/'skills')]
    try:
        binary=managed_binary(spec)
        roots.append(NativeSkillRoot(binary.parent/'builtin-skills','goose','Goose',pid))
    except UnsupportedCapabilityError:pass
    roots.extend(workspace_roots(projects,bindings,agent_key='goose',label='Goose',folders=('.agents/skills','.goose/skills','.claude/skills')))
    from drivers.goose.inventory import plugin_roots
    roots.extend(plugin_roots(spec,projects,bindings,config,base))
    return tuple(roots)


from drivers.goose.mcp_scope import before_runtime_session, after_runtime_session


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, environment, selected
    mode = approval(conversation)
    if mode is None:
        mode = selected(conversation, 'execution_mode', 'mode')
        if mode == 'auto':
            mode = 'native:auto'
    mode = choice(mode, {'host:ask': 'approve', 'ask': 'approve', 'bypass': 'auto',
        'approve': 'approve', 'native:auto': 'auto', 'smart_approve': 'smart_approve', 'chat': 'chat'}, '审批模式')
    effort = choice(selected(conversation, 'reasoning_mode', 'thinking_effort'),
        {'off', 'low', 'medium', 'high', 'max'}, '思考强度')
    command = arguments(command, ('--model', selected(conversation, 'model_id', 'model')),
        ('--provider', selected(conversation, 'provider_id', 'provider')))
    if plan is not None:
        command = cli_command(plan, command)
    values = {}
    if mode is not None:
        values['GOOSE_MODE'] = mode
    if effort is not None:
        # The native resume restores its persisted per-session model effort;
        # this supplies the same selection when a new model config is built.
        values['GOOSE_THINKING_EFFORT'] = effort
    return environment(spec, conversation, command, values)
