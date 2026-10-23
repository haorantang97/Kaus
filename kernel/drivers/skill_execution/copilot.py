"""Native Copilot skill filtering in a private conversation config directory.

Authentication remains in the OS credential store or existing process env.
Only native settings and plugin resources are copied, never the account/session
store. Managed session data lives under a Kaus-owned conversation directory.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, state_root, unsupported

_CONFIG_FILES = ('config.json', 'settings.json', 'providers.json', 'mcp-config.json', 'lsp-config.json')
VERIFIED_ACP_HASHES = {'7b48282a19b5b0814a0c96ad5e3a125173d792cc55f9525b2effea962f6063c0'}
VERIFIED_ENTRY_HASHES = {'24ec5cfb8f9bcb6559d8c187fbaaae3a9e964a570bf212fa646cff825bc47b78'}
_RESOURCE_DIRS = ('installed-plugins', 'hooks', 'agents', 'extensions')
_SECRET_KEYS = frozenset(('oauth_token', 'oauthToken', 'access_token', 'accessToken', 'refresh_token', 'refreshToken', 'githubToken'))


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from drivers.base import NativeHistory, NativeHistoryEntry
    from .history_io import active_branch, canonical_id, read_bytes, occurred
    ident = canonical_id(native_session_id)
    env = spec.build_env(); home = Path(env.get('HOME') or Path.home())
    native = Path(env.get('COPILOT_HOME') or home / '.copilot')
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    stores = [native / 'session-state']
    owned = state_root(spec)
    for directory in (owned / 'copilot-sessions', owned / 'session-mcp/copilot'):
        if directory.is_dir() and not directory.is_symlink():
            stores.extend(p / 'session-state' for p in directory.iterdir() if p.is_dir() and not p.is_symlink())
    try:
        candidates = {}
        for store in stores:
            file = store / ident / 'events.jsonl'
            if file.exists():
                if file.is_symlink():
                    raise ValueError('linked native transcript')
                resolved = file.resolve()
                # Known Kaus MCP records may link to the exact original session.
                if not resolved.is_relative_to(native.resolve()) and not resolved.is_relative_to(owned.resolve()):
                    raise ValueError('foreign native transcript')
                candidates[resolved] = (resolved.parent, Path('events.jsonl'))
        if len(candidates) != 1 or not cwd:
            raise ValueError('ambiguous native transcript')
        root, relative = next(iter(candidates.values()))
        rows = [json.loads(line) for line in read_bytes(root, relative).splitlines() if line.strip()]
        start = rows[0].get('data', {}) if rows and rows[0].get('type') == 'session.start' else {}
        if start.get('sessionId') != ident or Path(start.get('context', {}).get('cwd', '')).resolve() != Path(cwd).resolve():
            raise ValueError('native transcript identity mismatch')
        entries = []; omitted = False
        for row in active_branch(rows):
            kind = row.get('type'); data = row.get('data', {})
            if kind not in ('user.message', 'assistant.message'):
                omitted |= isinstance(kind, str) and kind.startswith('tool.')
                continue
            text = data.get('content')
            if isinstance(text, str) and text:
                entries.append(NativeHistoryEntry(entry_id=ident + ':' + row['id'], role=kind.split('.')[0], text=text, occurred_at=occurred(row.get('timestamp'))))
        return NativeHistory(native_session_id=ident, entries=tuple(entries), complete=not omitted,
                             missing=('non_text_native_entries',) if omitted else ())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError('原生会话历史暂不可读') from None


def read_jsonc(path):
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise UnsupportedCapabilityError('原生配置文件不可读')
    text = path.read_text(encoding='utf-8')
    # Preserve quoted strings (including URLs); strip comments and trailing
    # commas only outside those strings.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/|,\s*(?=[}\]])')
    text = tokens.sub(lambda match: match[0] if match[0].startswith('"') else '', text)
    try:
        value = json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise UnsupportedCapabilityError('原生配置格式无法读取') from exc
    if not isinstance(value, dict):
        raise UnsupportedCapabilityError('原生配置必须为对象')
    return value


def _contains_plaintext_auth(value):
    if isinstance(value, dict):
        return any(key in _SECRET_KEYS and bool(item) or _contains_plaintext_auth(item) for key, item in value.items())
    return isinstance(value, list) and any(_contains_plaintext_auth(item) for item in value)


def _copy_resources(source, target):
    count = size = 0
    for name in _RESOURCE_DIRS:
        root = source / name
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__'}]
            for child in [*dirs, *files]:
                path = Path(current) / child
                if path.is_symlink():
                    raise UnsupportedCapabilityError('插件资源包含外部链接，无法建立私有副本')
            destination = target / Path(current).relative_to(source)
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            for child in files:
                original = Path(current) / child
                count += 1
                size += original.stat().st_size
                if count > 50000 or size > 512 * 1024 * 1024:
                    raise UnsupportedCapabilityError('插件资源超过私有副本限制')
                shutil.copy2(original, destination / child)


async def inventory(spec, cwd):
    command = tuple(arg for arg in spec.command if arg not in {'--acp', '--stdio'})
    process = await asyncio.create_subprocess_exec(*command, 'skill', 'list', '--json',
        cwd=cwd, env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 25)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    try:
        rows = json.loads(output)
        if process.returncode or not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('name'), str) or not isinstance(r.get('enabled'), bool) for r in rows):
            raise ValueError()
    except ValueError as exc:
        raise UnsupportedCapabilityError('原生运行时未提供可验证的技能加载清单') from exc
    return rows


def _settings_matches(plan):
    for filename, expected in getattr(plan, 'skill_launch_files', {}).items():
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise UnsupportedCapabilityError('项目技能启动配置已变化，请重新应用')
    for name in ('settings.json', 'config.json'):
        values = read_jsonc(Path(plan.spec.env['COPILOT_HOME']) / name)
        for key, expected in plan.skill_policy.items():
            # Native 1.0.88 migrates user settings out of config.json on first
            # session startup. The authoritative settings file must retain the
            # policy; legacy fields, if still present, must not conflict.
            if (name == 'settings.json' or key in values) and values.get(key) != expected:
                raise UnsupportedCapabilityError('项目技能配置已变化，请重新应用')


async def validate_turn(plan, cwd):
    _settings_matches(plan)
    rows = await inventory(plan.spec, cwd)
    expected = {str(path.parent.resolve()) for path in plan.paths}
    actual = {str(Path(row.get('path', '')).resolve()) for row in rows if row['enabled']}
    if actual != expected:
        raise UnsupportedCapabilityError('原生技能加载范围已变化，请重新应用')


async def watch(plan, cwd):
    # Native discovery is constrained by directory ignores and name disables.
    # Configuration writes are additionally watched throughout a model turn.
    while True:
        await asyncio.sleep(.5)
        _settings_matches(plan)



def _acp_skill_scope(spec, plan, rows, policy):
    modules = {Path(row['path']).parent.parent / 'app.js' for row in rows
               if row.get('source') == 'builtin' and isinstance(row.get('path'), str)}
    modules = {path.resolve() for path in modules if path.is_file()}
    if len(modules) != 1:
        raise UnsupportedCapabilityError('原生技能会话接口无法定位')
    module = modules.pop()
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    if digest not in VERIFIED_ACP_HASHES:
        raise UnsupportedCapabilityError('当前 Copilot 技能会话版本尚未验证')
    entry = module.with_name('index.js')
    if not entry.is_file() or hashlib.sha256(entry.read_bytes()).hexdigest() not in VERIFIED_ENTRY_HASHES:
        raise UnsupportedCapabilityError('当前 Copilot 技能会话入口尚未验证')
    source = Path(__file__).with_name('copilot_guard.mjs').read_text()
    guard = plan.root / ('copilot-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    data = json.dumps({'module': str(module), 'moduleHash': digest, 'entry': str(entry), 'entryHash': hashlib.sha256(entry.read_bytes()).hexdigest(), 'policy': policy}, sort_keys=True)
    manifest = plan.root / ('copilot-scope-' + hashlib.sha256(data.encode()).hexdigest()[:24] + '.json')
    for path, content in ((guard, source), (manifest, data)):
        if path.exists() and (path.is_symlink() or path.read_text() != content):
            raise UnsupportedCapabilityError('项目技能启动配置不一致')
        if not path.exists():
            atomic_write_text(path, content)
    # SEA executables do not apply Node's --import preloads. Use the native
    # distribution-entry override to install the hook before its original
    # entrypoint, without copying or editing the installed distribution.
    bootstrap = plan.root / ('copilot-bootstrap-' + hashlib.sha256(data.encode()).hexdigest()[:24])
    bootstrap.mkdir(exist_ok=True, mode=0o700)
    script = '(async()=>{await import(' + json.dumps(guard.as_uri()) + ');process.env.COPILOT_CLI_RESOLVED_DIST_DIR=' + json.dumps(str(module.parent)) + ';await import(' + json.dumps(entry.as_uri()) + ');})().catch(error=>{console.error(error);process.exitCode=1;});\n'
    path = bootstrap / 'index.js'
    if path.exists() and (path.is_symlink() or path.read_text() != script):
        raise UnsupportedCapabilityError('项目技能启动配置不一致')
    if not path.exists():
        atomic_write_text(path, script)
    plan.skill_launch_files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (guard, manifest, path)}
    return spec.with_env(KAUS_COPILOT_SKILL_SCOPE=str(manifest), COPILOT_CLI_DIST_DIR=str(bootstrap))

async def _project(spec, binding, effective, workspace_root, conversation_id, *, dry_run=False, runtime_info=None):
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    if any(arg == '--prefer-version' or arg.startswith('--prefer-version=') for arg in spec.command):
        return unsupported(binding, '固定版本启动参数与项目技能范围冲突')
    if any(arg == '--config-dir' or arg.startswith('--config-dir=') for arg in spec.command):
        return unsupported(binding, '自定义配置目录参数与项目技能范围冲突')
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home()).expanduser()
    source = Path(env.get('COPILOT_HOME') or home / '.copilot').expanduser()
    cwd = str(workspace_root or spec.cwd or os.getcwd())
    configs = {name: read_jsonc(source / name) for name in _CONFIG_FILES}
    if not configs['mcp-config.json']:
        configs['mcp-config.json'] = {'mcpServers': {}}
    if any(_contains_plaintext_auth(value) for value in configs.values()):
        return unsupported(binding, '明文登录配置需要改用系统凭据存储或现有令牌环境变量')
    scope = hashlib.sha256((binding.id + ':' + conversation_id).encode()).hexdigest()[:24]
    private = plan.root / 'copilot' / scope
    state = state_root(spec) / 'copilot-sessions' / scope
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / 'session-state').mkdir(exist_ok=True, mode=0o700)
    if not private.exists():
        private.parent.mkdir(exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=private.parent))
        try:
            _copy_resources(source, stage)
            for name, value in configs.items():
                atomic_write_text(stage / name, json.dumps(value))
                (stage / name).chmod(0o600)
            (stage / 'session-state').symlink_to(state / 'session-state', target_is_directory=True)
            stage.rename(private)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    elif private.is_symlink() or (private / 'session-state').resolve() != state / 'session-state':
        raise UnsupportedCapabilityError('技能会话目录归属不匹配')
    projected = spec.with_env(COPILOT_HOME=str(private), COPILOT_AUTO_UPDATE='false',
                              COPILOT_SKILLS_DIRS=str(plan.root / '.agents/skills'))
    defaults = {home / '.agents/skills', home / '.claude/skills', source / 'skills', source / 'installed-plugins', private / 'installed-plugins'}
    for parent in (Path(cwd).resolve(), *Path(cwd).resolve().parents):
        defaults.update(parent / name for name in ('.github/skills', '.agents/skills', '.claude/skills'))
    for index,arg in enumerate(spec.command):
        if arg=='--plugin-dir' and index+1<len(spec.command):
            defaults.add(Path(spec.command[index+1]).expanduser())
        elif arg.startswith('--plugin-dir='):
            defaults.add(Path(arg.split('=',1)[1]).expanduser())
    for config in configs.values():
        defaults.update(Path(p).expanduser() for p in config.get('skillDirectories', []) if isinstance(p, str))
        defaults.update(Path(p).expanduser() for p in config.get('ignoredSkillsLocations', []) if isinstance(p, str))
    policy = {'skillDirectories': [str(plan.root / '.agents/skills')],
              'ignoredSkillsLocations': sorted(str(p.resolve()) for p in defaults), 'disabledSkills': []}
    for name in ('config.json', 'settings.json'):
        atomic_write_text(private / name, json.dumps({**configs[name], **policy}))
    # Discover every built-in/native source with the same plugin settings, then
    # disable non-selected names. Verify native enablement before any model call.
    rows = await inventory(projected, cwd)
    expected = {str(path.parent.resolve()) for path in plan.paths}
    policy['disabledSkills'] = sorted({row['name'] for row in rows if str(Path(row.get('path', '')).resolve()) not in expected})
    for name in ('config.json', 'settings.json'):
        atomic_write_text(private / name, json.dumps({**configs[name], **policy}))
    plan.spec = _acp_skill_scope(projected, plan, rows, policy)
    plan.skill_policy = policy
    await validate_turn(plan, cwd)
    return plan


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    return await _project(spec, binding, effective, workspace_root, 'preview', dry_run=dry_run, runtime_info=runtime_info)


async def project_session(spec, binding, effective, workspace_root, *, conversation, dry_run=False, runtime_info=None):
    return await _project(spec, binding, effective, workspace_root, conversation.id, dry_run=dry_run, runtime_info=runtime_info)


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    _settings_matches(plan)
    if not command:
        return ()
    variables = [key + '=' + plan.spec.env[key] for key in ('COPILOT_HOME', 'COPILOT_SKILLS_DIRS', 'COPILOT_AUTO_UPDATE', 'KAUS_COPILOT_SKILL_SCOPE', 'COPILOT_CLI_DIST_DIR')]
    script = '#!/bin/sh\nexec env ' + shlex.join(variables) + ' ' + shlex.quote(command[0]) + ' \"$@\"\n'
    launcher = plan.root / ('copilot-terminal-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    if launcher.exists() and (launcher.is_symlink() or launcher.read_text() != script):
        raise UnsupportedCapabilityError('终端技能配置不一致')
    if not launcher.exists():
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    return (str(launcher), *command[1:])


def _inventory_cache(spec):
    env=spec.build_env()
    identity=hashlib.sha256(json.dumps({'command':spec.command,'home':env.get('HOME'),'config':env.get('COPILOT_HOME')},sort_keys=True).encode()).hexdigest()[:24]
    return state_root(spec)/'native-inventory/copilot'/f'{identity}.json'


async def refresh_inventory(spec):
    # A neutral directory prevents a workspace source being promoted globally.
    with tempfile.TemporaryDirectory(prefix='kaus-inventory-') as directory:
        rows=await inventory(spec.with_env(COPILOT_AUTO_UPDATE='false'),directory)
    values=[{'path':row['path'],'enabled':row['enabled']} for row in rows
            if isinstance(row.get('path'),str) and Path(row['path']).is_absolute() and (Path(row['path'])/'SKILL.md').is_file()]
    cache=_inventory_cache(spec);cache.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_write_text(cache,json.dumps(values))


def inventory_roots(spec,projects,bindings):
    from drivers.skill_inventory import NativeSkillRoot
    from .source_roots import root_project,workspace_roots
    projects=tuple(projects);pid=root_project(projects)
    if not pid:return ()
    env=spec.build_env();home=Path(env.get('HOME') or Path.home());native=Path(env.get('COPILOT_HOME') or home/'.copilot')
    roots=[NativeSkillRoot(path,'copilot','GitHub Copilot',pid) for path in (native/'skills',home/'.agents/skills')]
    try:
        rows=json.loads(_inventory_cache(spec).read_text())
        roots.extend(NativeSkillRoot(Path(row['path']),'copilot','GitHub Copilot',pid,
                    blocked_project_ids=(pid,) if row.get('enabled') is False else ()) for row in rows)
    except (OSError,ValueError,TypeError,KeyError):pass
    roots.extend(workspace_roots(projects,bindings,agent_key='copilot',label='GitHub Copilot',folders=('.github/skills','.agents/skills','.claude/skills')))
    return tuple(roots)


async def prepare_runtime_spec(spec, conversation, metadata):
    """Copilot ACP accepts remote MCP only; stdio uses a private native config.

    Preserve the already projected Skill settings. A resumed native history is
    linked by its exact session ID, never copied as an entire user history store.
    """
    servers=[row for row in metadata.get('mcpServers',[]) if row.get('type') not in {'http','sse'}]
    private=state_root(spec)/'session-mcp/copilot'/hashlib.sha256(conversation.id.encode()).hexdigest()
    if not servers and not private.exists():return spec
    env=spec.build_env();source=Path(env.get('COPILOT_HOME') or Path(env.get('HOME') or Path.home())/'.copilot').expanduser()
    if source==private:
        marker=read_jsonc(private/'kaus-source.json')
        if not isinstance(marker.get('source'),str):raise UnsupportedCapabilityError('原生 MCP 配置来源不可验证')
        source=Path(marker['source'])
    configs={name:read_jsonc(source/name) for name in _CONFIG_FILES}
    if any(_contains_plaintext_auth(value) for value in configs.values()):
        raise UnsupportedCapabilityError('明文登录配置需要改用系统凭据存储或现有令牌环境变量')
    if private.is_symlink():raise UnsupportedCapabilityError('会话 MCP 配置目录归属不匹配')
    private.mkdir(parents=True,exist_ok=True,mode=0o700)
    sessions=private/'session-state'
    if sessions.is_symlink():raise UnsupportedCapabilityError('会话 MCP 记录目录归属不匹配')
    sessions.mkdir(exist_ok=True,mode=0o700)
    session_id=conversation.native_session_id
    if session_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',session_id):
        raise UnsupportedCapabilityError('原生会话标识无效')
    if session_id and not (sessions/session_id).exists():
        original=(source/'session-state'/session_id).resolve()
        if not original.is_relative_to((source/'session-state').resolve()) or not original.is_dir() or not (original/'events.jsonl').is_file():
            raise UnsupportedCapabilityError('原生会话记录不在当前配置目录，无法应用会话 MCP')
        (sessions/session_id).symlink_to(original,target_is_directory=True)
    elif session_id and (sessions/session_id).is_symlink():
        expected=(source/'session-state'/session_id).resolve()
        if (sessions/session_id).resolve()!=expected:
            raise UnsupportedCapabilityError('原生会话记录链接已变化')
    _copy_resources(source,private)
    for name,value in configs.items():
        if name=='mcp-config.json':
            inherited=value.get('mcpServers',{})
            if not isinstance(inherited,dict):raise UnsupportedCapabilityError('原生 MCP 配置格式无效')
            additions={row['name']:{'command':row['command'],'args':list(row.get('args',[])),
                'env':{item['name']:item['value'] for item in row.get('env',[])},'tools':['*']} for row in servers}
            value={**value,'mcpServers':{**inherited,**additions}}
        atomic_write_text(private/name,json.dumps(value));(private/name).chmod(0o600)
    atomic_write_text(private/'kaus-source.json',json.dumps({'source':str(source)}));(private/'kaus-source.json').chmod(0o600)
    return spec.with_env(COPILOT_HOME=str(private))


async def before_runtime_session(connection, spec, conversation, metadata):
    # Stdio entries were installed in the private native config above. Sending
    # them to ACP would produce an ignored-entry warning despite that success.
    return {**metadata, 'mcpServers': [row for row in metadata.get('mcpServers', [])
                                     if row.get('type') in {'http', 'sse'}]}


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, environment, selected
    permission = approval(conversation)
    # ACP's allow_all option controls tools, paths and URLs together.
    raw = (getattr(conversation, 'config_options', {}) or {}).get('allow_all')
    if permission in ('host:ask', 'ask') and raw not in (None, False, 'off'):
        raise UnsupportedCapabilityError('终端审批配置冲突')
    if permission not in (None, 'host:ask', 'ask', 'bypass'):
        raise UnsupportedCapabilityError('终端不支持当前审批模式')
    if raw not in (None, False, True, 'off', 'on'):
        raise UnsupportedCapabilityError('终端不支持当前审批模式')
    values = {}
    if permission is not None or raw is not None:
        allow = permission == 'bypass' or raw in (True, 'on')
        values['COPILOT_ALLOW_ALL'] = 'true' if allow else 'false'
        values['COPILOT_ASSISTED_APPROVAL'] = 'false'
    mode = selected(conversation, 'execution_mode', 'mode')
    if mode:
        mode = mode.removeprefix('https://agentclientprotocol.com/protocol/session-modes#')
    mode = choice(mode, {'agent': 'interactive', 'interactive': 'interactive', 'plan': 'plan', 'autopilot': 'autopilot'}, '运行模式')
    effort = choice(selected(conversation, 'reasoning_mode', 'reasoning_effort'),
        {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}, '思考强度')
    command = arguments(command, ('--model', selected(conversation, 'model_id', 'model')),
        ('--reasoning-effort', effort), ('--mode', mode))
    if plan is not None:
        command = cli_command(plan, command)
    return environment(spec, conversation, command, values)
