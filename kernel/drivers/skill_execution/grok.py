"""Project skills through a verified, Kaus-owned Grok loader build."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shlex

from .common import materialize, selection_of, unsupported
from .managed_builds import resolve_managed_binary
from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text

SOURCE_VERSION = "1.0.41"
PATCH_VERSION = "kaus-skills-1"


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from urllib.parse import quote
    from drivers.base import NativeHistory, NativeHistoryEntry
    from .history_io import canonical_id, read_bytes
    from datetime import datetime, timezone
    ident = canonical_id(native_session_id)
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    root = Path(env.get('GROK_HOME') or home / '.grok')
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    if not cwd:
        raise UnsupportedCapabilityError('原生会话工作目录缺失')
    folder = Path('sessions') / quote(str(Path(cwd).resolve()), safe='') / ident
    try:
        info = json.loads(read_bytes(root, folder / 'summary.json')).get('info', {})
        if info.get('id') != ident or not isinstance(info.get('cwd'), str) or Path(info['cwd']).resolve() != Path(cwd).resolve():
            raise ValueError('native transcript identity mismatch')
        rows = [json.loads(line) for line in read_bytes(root, folder / 'updates.jsonl').splitlines() if line.strip()]
        live = []; starts = []; in_user = False; last_prompt = None; seen_prompt = False
        for row in rows:
            update = row.get('params', {}).get('update', {}); kind = update.get('sessionUpdate')
            if row.get('method') == '_x.ai/session/update' and kind == 'rewind_marker':
                target = update.get('target_prompt_index')
                if not isinstance(target, int) or target < 0:
                    raise ValueError('invalid native rewind')
                live = live[:starts[target]] if target < len(starts) else live
                starts = starts[:target]; in_user = False; last_prompt = None
                continue
            meta = update.get('_meta', {})
            if row.get('method') == 'session/update' and kind == 'user_message_chunk' and not meta.get('hostTurn'):
                prompt = meta.get('promptIndex')
                seen_prompt |= prompt is not None
                new_prompt = not in_user or (seen_prompt and prompt != last_prompt)
                if new_prompt and (not seen_prompt or prompt is not None):
                    starts.append(len(live))
                in_user = True; last_prompt = prompt
            else:
                in_user = False; last_prompt = None
            live.append(row)
        entries = []; seen = {}; previous = None; omitted = False
        for row in live:
            params = row.get('params', {})
            if params.get('sessionId') != ident:
                raise ValueError('foreign session update')
            update = params.get('update', {}); kind = update.get('sessionUpdate')
            if update.get('_meta', {}).get('hostTurn'):
                previous = None
                continue
            if kind not in ('user_message_chunk', 'agent_message_chunk'):
                if kind in ('turn_completed', 'tool_call', 'tool_call_update', 'agent_thought_chunk'):
                    previous = None
                    omitted |= kind != 'turn_completed'
                continue
            meta = params.get('_meta', {})
            event_id = meta.get('eventId'); timestamp_ms = meta.get('agentTimestampMs')
            content = update.get('content', {})
            if not isinstance(event_id, str) or not event_id or not isinstance(timestamp_ms, int):
                raise ValueError('native event identity missing')
            # Native event counters restart on a cold ACP process. The persisted
            # native millisecond stamp disambiguates these exact records.
            key = event_id + ':' + str(timestamp_ms)
            if key in seen:
                if seen[key] != row:
                    raise ValueError('native event identity conflict')
                continue
            seen[key] = row
            if content.get('type') != 'text' or not isinstance(content.get('text'), str):
                omitted = True; previous = None; continue
            role = 'user' if kind == 'user_message_chunk' else 'assistant'
            if previous == kind and entries:
                entries[-1] = entries[-1].model_copy(update={'text': entries[-1].text + content['text']})
            else:
                stamp = row.get('timestamp')
                entries.append(NativeHistoryEntry(entry_id=key, role=role, text=content['text'],
                    occurred_at=datetime.fromtimestamp(stamp, timezone.utc) if isinstance(stamp, (int, float)) else None))
            previous = kind
        return NativeHistory(native_session_id=ident, entries=tuple(entries), complete=not omitted,
                             missing=('non_text_native_entries',) if omitted else ())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise RuntimeError('原生会话历史暂不可读') from None


async def native_version(spec, cwd):
    from .native_catalog import query
    output = await query(spec, (spec.command[0], '--no-auto-update', '--version'), cwd=cwd)
    parts = output.strip().split()
    return parts[1] if len(parts) > 1 and parts[0] == 'grok' else None


def skill_list_result(response):
    """Grok extensions wrap data in an ExtMethodResult envelope."""
    if not isinstance(response, dict) or response.get('error'):
        raise ValueError('Grok 技能目录读取失败')
    data = response.get('result')
    if not isinstance(data, dict) or not isinstance(data.get('skills'), list):
        raise ValueError('Grok 技能目录格式无效')
    return data['skills']


async def refresh_inventory(spec):
    from drivers.acp.client import AcpConnection
    from .native_catalog import save_paths
    connection = await AcpConnection.spawn(spec)
    try:
        await connection.call('initialize', {'protocolVersion': 1,
            'clientInfo': {'name': 'kaus-inventory', 'version': '1'}, 'clientCapabilities': {}}, timeout=15)
        data = await connection.call('_x.ai/skills/list', {'cwd': spec.cwd or str(Path.home())}, timeout=15)
        save_paths(spec, 'grok', [item['path'] for item in skill_list_result(data)
            if isinstance(item.get('path'), str) and item.get('enabled', True)])
    finally:
        await connection.aclose()


def inventory_roots(spec, projects, bindings):
    from .native_catalog import cached_roots
    return cached_roots(spec, 'grok', 'Grok Build', projects, bindings)


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout="roots", dry_run=dry_run)
        plan.spec = spec
        return plan
    try:
        build = json.loads((Path(__file__).parent.parent / 'grok/build.json').read_text())
        binary = resolve_managed_binary(spec, engine_key="grok", source_version=SOURCE_VERSION,
            patch_version=PATCH_VERSION, expected_source_commit=build['sourceCommit'],
            expected_patch_hash=build['patchHash'])
        if await native_version(spec, workspace_root or spec.cwd) != SOURCE_VERSION:
            return unsupported(binding, '原生版本与项目技能运行组件不匹配，请更新运行组件', dry_run=dry_run)
    except (OSError, ValueError, UnsupportedCapabilityError) as exc:
        return unsupported(binding, str(exc), dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout="roots", dry_run=dry_run)
    plan.spec = spec
    if plan.root is not None and not dry_run:
        plan.spec = spec.model_copy(update={"command": (str(binary), *spec.command[1:])}).with_env(
            KAUS_GROK_SKILL_ALLOWLIST=json.dumps([str(path) for path in plan.paths]))
    return plan


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    if not plan.spec or not plan.spec.env.get("KAUS_GROK_SKILL_ALLOWLIST"):
        raise UnsupportedCapabilityError("项目技能启动配置缺失")
    if not command:
        return ()
    variable = 'KAUS_GROK_SKILL_ALLOWLIST=' + plan.spec.env['KAUS_GROK_SKILL_ALLOWLIST']
    script = '#!/bin/sh\nexec env ' + shlex.quote(variable) + ' ' + shlex.quote(plan.spec.command[0]) + ' "$@"\n'
    launcher = plan.root / ('grok-terminal-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    if launcher.exists() and (launcher.is_symlink() or launcher.read_text() != script):
        raise UnsupportedCapabilityError('终端技能配置不一致')
    if not launcher.exists():
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    arguments = command[1:]
    if '--no-auto-update' not in arguments:
        arguments = ('--no-auto-update', *arguments)
    return (str(launcher), *arguments)


MCP_READY_TIMEOUT = 12.0
MCP_POLL_INTERVAL = 0.2


def _mcp_failure(message, name):
    label = ''.join(char for char in name if char.isprintable())[:48]
    return UnsupportedCapabilityError(f'{message}：{label}')


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    """Check live clients after native client-over-baseline merge by name."""
    import asyncio
    requested = metadata.get('mcpServers', [])
    if not isinstance(requested, list) or any(not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'] for row in requested):
        raise UnsupportedCapabilityError('MCP 配置无效')
    if not requested:
        return
    names = {row['name'] for row in requested}
    if len(names) != len(requested):
        raise UnsupportedCapabilityError('MCP 名称重复')
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_READY_TIMEOUT

    async def read(params):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise UnsupportedCapabilityError('MCP 服务启动超时')
        try:
            response = await connection.call('_x.ai/mcp/list', params, timeout=min(remaining, 5.0))
        except Exception as exc:
            raise UnsupportedCapabilityError('无法核验当前会话的 MCP 状态') from None
        data = response.get('result') if isinstance(response, dict) and not response.get('error') else None
        if not isinstance(data, dict) or not isinstance(data.get('servers'), list):
            raise UnsupportedCapabilityError('无法核验当前会话的 MCP 状态')
        rows = data['servers']
        if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in rows):
            raise UnsupportedCapabilityError('MCP 状态格式无效')
        if len({row['name'] for row in rows}) != len(rows):
            raise UnsupportedCapabilityError('MCP 来源不唯一')
        return data

    # The native merge inserts client definitions last, then establishes this
    # session's clients. Catalog command fields may describe the baseline; only
    # the requested session's client status proves a successful override.
    while True:
        data = await read({'sessionId': session_id, 'cache': True})
        by_name = {row['name']: row for row in data['servers']}
        completed = data.get('sessionMcpResolved') is True
        ready = True
        for name in names:
            row = by_name.get(name)
            if row and (row.get('type') == 'managedGateway' or row.get('source') == 'managed'):
                raise _mcp_failure('MCP 配置来源无法核验', name)
            state = row.get('session') if row else None
            if not isinstance(state, dict):
                if completed:
                    raise _mcp_failure('MCP 服务启动失败', name)
                ready = False
                continue
            if state.get('authRequired'):
                raise _mcp_failure('MCP 服务需要授权', name)
            if state.get('setupRequired'):
                raise _mcp_failure('MCP 服务需要配置', name)
            if state.get('blockedReason'):
                raise _mcp_failure('MCP 服务被策略阻止', name)
            if state.get('enabled') is not True or state.get('status') != 'ready':
                if completed and state.get('status') != 'initializing':
                    raise _mcp_failure('MCP 服务启动失败', name)
                ready = False
        if completed and ready:
            return
        await asyncio.sleep(min(MCP_POLL_INTERVAL, max(0, deadline - loop.time())))


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, selected
    mode = choice(approval(conversation), {'host:ask': 'default', 'ask': 'default', 'auto': 'acceptEdits',
        'bypass': 'bypassPermissions', 'read_only': 'plan', 'plan': 'plan', 'default': 'default',
        'acceptEdits': 'acceptEdits', 'native:auto': 'auto', 'dontAsk': 'dontAsk', 'bypassPermissions': 'bypassPermissions'}, '审批模式')
    if selected(conversation, 'execution_mode'):
        raise UnsupportedCapabilityError('终端不支持当前运行模式')
    effort = choice(selected(conversation, 'reasoning_mode', 'reasoning_effort'),
        {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}, '思考强度')
    command = arguments(command, ('--model', selected(conversation, 'model_id', 'model')),
        ('--reasoning-effort', effort), ('--permission-mode', mode))
    return cli_command(plan, command) if plan is not None else command
