"""Per-process scope for Qwen's native SkillManager and dynamic extension skills."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import shlex
import yaml
from collections import Counter

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported
from .node_package import locate

# Qwen Code 0.24.6. Changes to the private loader require a fresh native probe.


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from drivers.base import NativeHistory, NativeHistoryEntry
    from .history_io import active_branch, canonical_id, read_bytes, occurred
    ident = canonical_id(native_session_id)
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    root = Path(env.get('QWEN_HOME') or home / '.qwen') / 'projects'
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    try:
        candidates = [p / 'chats' / (ident + '.jsonl') for p in root.iterdir()
                      if not p.is_symlink() and p.is_dir() and (p / 'chats' / (ident + '.jsonl')).exists()]
        if len(candidates) != 1 or not cwd:
            raise ValueError('ambiguous native transcript')
        rows = [json.loads(line) for line in read_bytes(root, candidates[0].relative_to(root)).splitlines() if line.strip()]
        if not rows or any(row.get('sessionId') != ident or not isinstance(row.get('cwd'), str) or Path(row['cwd']).resolve() != Path(cwd).resolve() for row in rows):
            raise ValueError('native transcript identity mismatch')
        entries = []; omitted = False
        for row in active_branch(rows, id_key='uuid', parent_key='parentUuid'):
            role = row.get('type'); provenance = row.get('provenance')
            if (role, provenance) not in {('user', 'real_user'), ('assistant', 'assistant_output')}:
                continue
            parts = row.get('message', {}).get('parts', [])
            text = '\n'.join(p['text'] for p in parts if isinstance(p, dict) and isinstance(p.get('text'), str) and not p.get('thought'))
            omitted |= any(not isinstance(p, dict) or 'text' not in p or p.get('thought') for p in parts)
            if text:
                entries.append(NativeHistoryEntry(entry_id=ident + ':' + row['uuid'], role=role, text=text, occurred_at=occurred(row.get('timestamp'))))
        return NativeHistory(native_session_id=ident, entries=tuple(entries), complete=not omitted,
                             missing=('non_text_native_entries',) if omitted else ())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError('原生会话历史暂不可读') from None
VERIFIED_LOADER_HASHES = {'be1e8c2f3df694ae8769d8601f5280117337607c0ccef9d5ba303517b9d5d791'}


def loader_module(package):
    if package:
        for path in (package / 'chunks').glob('*.js'):
            if hashlib.sha256(path.read_bytes()).hexdigest() in VERIFIED_LOADER_HASHES:
                return path.resolve()
    return None


def _write(path, content):
    if path.exists():
        if path.is_symlink() or path.read_text() != content:
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
    else:
        atomic_write_text(path, content)


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    package = locate(spec, '@qwen-code/qwen-code')
    module = loader_module(package)
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if not module or not node:
        return unsupported(binding, '当前 Qwen 技能加载版本尚未验证', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    records = json.loads((plan.root / '.kaus-skill-selection.json').read_text())['skills']
    original_names = []
    for item in records:
        text = Path(item['source']).read_text()
        meta = yaml.safe_load(text[4:].split('\n---', 1)[0]) if text.startswith('---\n') else {}
        original_names.append(meta.get('name') if isinstance(meta, dict) and isinstance(meta.get('name'), str) else '')
    selected_assets = selection_of(effective).config.get('assets', [])
    for i, asset in enumerate(selected_assets):
        if i < len(original_names) and asset.get('capabilityType') == 'qwen:skills' and ':' in str(asset.get('name', '')):
            original_names[i] = asset['name']
    counts = Counter(original_names)
    native_counts = Counter(name for name, record in zip(original_names, records) if record['type'] == 'qwen:skills')
    # Native workflow/delegation tools refer to the bundled skills by name.
    # Preserve unique declared names while exact copied paths remain the fence.
    plan.names = tuple(name if name and (counts[name] == 1 or record['type'] == 'qwen:skills' and native_counts[name] == 1)
                       else fallback for name, fallback, record in zip(original_names, plan.names, records))
    source = Path(__file__).with_name('qwen_guard.mjs').read_text()
    guard = plan.root / ('qwen-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    _write(guard, source)
    data = {'package': str(package.resolve()), 'module': str(module),
            'moduleHash': hashlib.sha256(module.read_bytes()).hexdigest(),
            'root': str(plan.root / '.agents/skills'), 'names': list(plan.names), 'paths': [str(path) for path in plan.paths]}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('qwen-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    _write(manifest, encoded)
    options = spec.build_env().get('NODE_OPTIONS', '')
    plan.spec = spec.with_env(KAUS_QWEN_SKILL_SCOPE=str(manifest),
        NODE_OPTIONS=(options + ' --import=' + guard.as_uri()).strip())
    script = f'''const {{SkillManager}}=await import({json.dumps(module.as_uri())});
const m=new SkillManager({{getBareMode:()=>false,isSafeMode:()=>false,getProjectRoot:()=>process.cwd()}});
const list=await m.listSkills();if(list.length!=={len(plan.paths)})throw Error('incomplete');
for(const skill of list)if(!(await m.loadSkillForRuntime(skill.name)))throw Error('unreadable');'''
    process = await asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
        env={**plan.spec.build_env(), 'KAUS_QWEN_SCOPE_PROBE': '1'}, cwd=workspace_root or spec.cwd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, _error = await asyncio.wait_for(process.communicate(), 20)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生技能加载验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生 Qwen 技能加载验证失败')
    return plan


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    if not plan.spec or not plan.spec.env.get('KAUS_QWEN_SKILL_SCOPE') or not plan.spec.env.get('NODE_OPTIONS'):
        raise UnsupportedCapabilityError('终端技能配置缺失')
    if not command:
        return ()
    variables = [key + '=' + plan.spec.env[key] for key in ('KAUS_QWEN_SKILL_SCOPE', 'NODE_OPTIONS')]
    script = '#!/bin/sh\nexec env ' + shlex.join(variables) + ' ' + shlex.quote(command[0]) + ' "$@"\n'
    launcher = plan.root / ('qwen-terminal-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    _write(launcher, script)
    launcher.chmod(0o700)
    return (str(launcher), *command[1:])


def inventory_roots(spec, projects, bindings):
    from .qwen_inventory import roots
    return roots(spec, projects, bindings)


MCP_READY_TIMEOUT = 12.0
MCP_POLL_INTERVAL = 0.2


def _requested_mcp(metadata):
    requested = metadata.get('mcpServers', [])
    if not isinstance(requested, list) or any(not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'] for row in requested):
        raise UnsupportedCapabilityError('MCP 配置无效')
    if len({row['name'] for row in requested}) != len(requested):
        raise UnsupportedCapabilityError('MCP 名称重复')
    return requested


def _mcp_failure(message, name):
    label = ''.join(char for char in name if char.isprintable())[:48]
    return UnsupportedCapabilityError(f'{message}：{label}')


def _mcp_config_matches(requested, actual):
    config = actual.get('config')
    if not isinstance(config, dict):
        return False
    if requested.get('type', 'stdio') == 'stdio':
        return (actual.get('transport') == 'stdio'
                and config.get('command') == requested.get('command')
                and config.get('args', []) == requested.get('args', []))
    field = 'httpUrl' if requested.get('type') == 'http' else 'url'
    return config.get(field) == requested.get('url')


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    """Validate the live session's injected MCP, without consulting workspace pools."""
    requested = _requested_mcp(metadata)
    if not requested:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_READY_TIMEOUT
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise UnsupportedCapabilityError('MCP 服务启动超时')
        try:
            data = await connection.call('qwen/status/session/resources',
                {'sessionId': session_id}, timeout=min(remaining, 5.0))
        except Exception as exc:
            raise UnsupportedCapabilityError('无法核验当前会话的 MCP 状态') from None
        status = data.get('mcp') if isinstance(data, dict) else None
        if not isinstance(status, dict) or data.get('sessionId') != session_id or not isinstance(status.get('servers'), list):
            raise UnsupportedCapabilityError('无法核验当前会话的 MCP 状态')
        rows = status['servers']
        if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in rows):
            raise UnsupportedCapabilityError('MCP 状态格式无效')
        if len({row['name'] for row in rows}) != len(rows):
            raise UnsupportedCapabilityError('MCP 来源不唯一')
        by_name = {row['name']: row for row in rows}
        completed = status.get('initialized') is True and status.get('discoveryState') == 'completed'
        ready = True
        for wanted in requested:
            row = by_name.get(wanted['name'])
            if row is None:
                if completed:
                    raise _mcp_failure('MCP 配置未应用', wanted['name'])
                ready = False
                continue
            if row.get('configOrigin') != 'runtime' or not _mcp_config_matches(wanted, row):
                raise _mcp_failure('MCP 配置来源不一致', wanted['name'])
            if row.get('requiresAuth') or row.get('approvalState') not in (None, 'approved') or row.get('authenticationState') in ('pending', 'required'):
                raise _mcp_failure('MCP 服务需要授权', wanted['name'])
            connected = row.get('status') == 'ok' and row.get('mcpStatus') == 'connected' and row.get('disabled') is False
            if not connected:
                if completed and row.get('mcpStatus') != 'connecting':
                    raise _mcp_failure('MCP 服务启动失败', wanted['name'])
                ready = False
        if completed and ready:
            return
        await asyncio.sleep(min(MCP_POLL_INTERVAL, max(0, deadline - loop.time())))


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, environment, immutable, selected
    from .copilot import read_jsonc
    import os
    import sys
    mode = choice(approval(conversation), {'host:ask': 'default', 'ask': 'default', 'auto': 'auto-edit',
        'bypass': 'yolo', 'read_only': 'plan', 'plan': 'plan', 'default': 'default',
        'auto-edit': 'auto-edit', 'native:auto': 'auto', 'yolo': 'yolo'}, '审批模式')
    execution = selected(conversation, 'execution_mode')
    if execution:
        raise UnsupportedCapabilityError('终端不支持当前运行模式')
    model = selected(conversation, 'model_id', 'model')
    # A routed ACP ID includes provider identity, not just a CLI model name.
    if model and model.startswith('qwen-route:'):
        raise UnsupportedCapabilityError('终端无法恢复当前模型路由')
    auth = None
    if model:
        import re
        route = re.fullmatch(r'(.+)\((openai|openai-responses|anthropic|qwen-oauth|gemini|vertex-ai)\)', model)
        if route:
            model, auth = route.groups()
    command = arguments(command, ('--model', model), ('--auth-type', auth), ('--approval-mode', mode))
    if plan is not None:
        command = cli_command(plan, command)
    effort = choice(selected(conversation, 'reasoning_mode', 'reasoning_effort'),
        {'default': None, 'none': 'none', 'minimal': 'minimal', 'low': 'low',
         'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh', 'max': 'max'}, '思考强度')
    if selected(conversation, 'reasoning_mode', 'reasoning_effort') is not None:
        env = spec.build_env()
        default = '/Library/Application Support/QwenCode/settings.json' if sys.platform == 'darwin' else (
            r'C:\ProgramData\qwen-code\settings.json' if os.name == 'nt' else '/etc/qwen-code/settings.json')
        settings = read_jsonc(Path(env.get('QWEN_CODE_SYSTEM_SETTINGS_PATH') or default))
        previous = settings.get('model') or {}
        if not isinstance(previous, dict):
            raise UnsupportedCapabilityError('原生模型配置无法读取')
        settings['model'] = {**previous, 'reasoningEffort': effort}
        path = immutable(spec, conversation, json.dumps(settings, sort_keys=True), suffix='.json')
        command = environment(spec, conversation, command, {'QWEN_CODE_SYSTEM_SETTINGS_PATH': str(path)})
    return command
