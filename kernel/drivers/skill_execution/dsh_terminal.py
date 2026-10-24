"""Native DSH TUI profile and session overlays; history stays in DSH_HOME."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys

from drivers.base import NativeHistory, NativeHistoryEntry, UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .dsh import dependency, VERIFIED_MODULES, _write
from .node_package import locate
from .terminal_settings import approval, selected

PROFILE = 'kaus-tui'
PLUGIN = '@dsh-tui/dsh-tui'
PLUGIN_VERSION = '0.1.2'
PAYLOAD_ENV = 'KAUS_DSH_TERMINAL_PATCH'
OWNER = 'Kaus DSH terminal profile v1\n'
_locks: dict[str, asyncio.Lock] = {}


def home(spec):
    env = spec.build_env()
    return Path(env.get('DSH_HOME') or Path(env.get('HOME') or Path.home()) / '.dsh').expanduser()


def profile_launch_args(spec):
    """Keep the original profile, including custom bundles and native settings."""
    args=list(spec.command)
    profile, patches = 'acp', []
    for i, arg in enumerate(args):
        for key in ('--profile','--patch'):
            value = (args[i+1] if i+1<len(args) else None) if arg == key else (
                arg[len(key)+1:] if arg.startswith(key+'=') else None)
            if value is None: continue
            if key == '--profile': profile = value
            else: patches.append(value)
    if profile in ('.','..','node_modules') or '/' in profile or '\\' in profile:
        raise UnsupportedCapabilityError('DSH 配置名称无效')
    result=['--profile',profile]
    for value in patches:
        path=Path(value).expanduser()
        result.extend(('--patch',str(path if path.is_absolute() else Path(spec.cwd or os.getcwd())/path)))
    return result


def terminal_surface_patch(spec):
    """Add only native TUI components over the selected ACP profile."""
    folder=home(spec)/'profiles'/PROFILE
    installed=folder/'node_modules'/PLUGIN
    source=compatibility_patch((installed/'cordis.patch.yml').read_text())
    source=source[source.index('- insert:'):]
    for name,filename in (('@dsh-tui/dsh-tui/startup','startup.js'),
                          ('@dsh-tui/dsh-tui/prompt','prompt.js'),
                          ('@dsh-tui/dsh-tui','index.js')):
        source=source.replace("'"+name+"'",json.dumps(str(installed/'lib'/filename),ensure_ascii=False))
    source='- id: acp\n  disabled: true\n- id: acp-app-startup\n  disabled: true\n'+source
    path=folder/('surface-'+hashlib.sha256(source.encode()).hexdigest()[:24]+'.yml')
    _write(path,source)
    return path


async def read_session(spec, sid, cwd, *, package_name='@deepseek-ai/dsh'):
    from .native_catalog import query
    package = locate(spec, package_name)
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    names = {'cordis':'@deepseek-ai/cordis', 'session':'@deepseek-ai/dsh-session',
             'persistence':'@deepseek-ai/dsh-session-persistence-jsonl'}
    if package_name == '@deepseek-ai/dsh':
        names['boot'] = '@deepseek-ai/dsh-app-boot'
    modules = {key:dependency(package, name) for key,name in names.items()} if package else {}
    if not node or not modules or not all(modules.values()):
        raise UnsupportedCapabilityError('当前 DSH 安装缺少原生历史读取组件')
    root = spec.build_env().get('DEEPSEEK_ACP_SESSIONS_ROOT') if package_name == 'deepseek-acp' else None
    payload = {'modules':{key:str(value) for key,value in modules.items()},
               'root':str(root or home(spec)/'sessions'), 'id':sid, 'cwd':cwd}
    if package_name == '@deepseek-ai/dsh':
        args = profile_launch_args(spec)
        payload['profile'] = {'directory':str(home(spec)/'profiles'/args[1]),
            'anchor':str(package/'package.json'), 'home':str(home(spec)),
            'patches':[args[i+1] for i,arg in enumerate(args) if arg == '--patch']}
    output = await query(spec, (node, str(Path(__file__).with_name('dsh_history.mjs')), json.dumps(payload)))
    return json.loads(output)


async def load_native_history(spec, binding, sid, *, package_name='@deepseek-ai/dsh'):
    try:
        data = await read_session(spec, sid, binding.runtime_config.get('workspace_root') or spec.cwd, package_name=package_name)
        return NativeHistory(native_session_id=sid,
            entries=tuple(NativeHistoryEntry(**row) for row in data['entries']),
            complete=not data['missing'], missing=tuple(data['missing']))
    except (ValueError, OSError, TypeError, KeyError):
        raise RuntimeError('无法读取 DSH 原生记录，请重试') from None


def compatibility_patch(source):
    # 0.1.5-rc.3 already supplies these services. Keep upstream plugin source
    # untouched; compose its TUI-only layer in our own native profile.
    for name in ('storage', 'storage-json', 'storage-domain', 'session-projection-cache'):
        source, count = re.subn(r'(?m)^    - id: '+re.escape(name)+r'\n(?:(?!    - id:)[\s\S])*?(?=    - id:|\Z)', '', source, count=1)
        if count != 1:
            raise UnsupportedCapabilityError('DSH 终端组件版本需要更新适配')
    return source


async def ensure_profile(spec, executable):
    async with _locks.setdefault(str(home(spec)), asyncio.Lock()):
        return await _ensure_profile(spec, executable)


async def _ensure_profile(spec, executable):
    package = locate(spec, '@deepseek-ai/dsh')
    version = json.loads((package/'package.json').read_text())['version'] if package else None
    if version != '0.1.5-rc.3':
        raise UnsupportedCapabilityError('当前 DSH 终端组件版本需要适配')
    directory = home(spec)/'profiles'/PROFILE
    marker = directory/'.kaus-owner'
    if directory.is_symlink() or (directory.exists() and (marker.is_symlink() or not marker.is_file() or marker.read_text() != OWNER)):
        raise UnsupportedCapabilityError('DSH 终端配置名称已被占用')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not marker.exists(): atomic_write_text(marker, OWNER)
    installed = directory/'node_modules'/PLUGIN
    if not (installed/'package.json').is_file():
        # Install only a separate profile. No CLI update and no user-profile edit.
        prefix = []
        if not shutil.which('pnpm', path=spec.build_env().get('PATH')):
            npm = shutil.which('npm', path=spec.build_env().get('PATH'))
            if not npm:
                raise UnsupportedCapabilityError('当前 DSH 安装缺少 Node.js 包管理器')
            # npm caches this tool locally; never replace a global package manager.
            prefix = [npm, 'exec', '--yes', '--package=pnpm@11.1.3', '--']
        process = await asyncio.create_subprocess_exec(*prefix, executable, 'plugin', '--profile', PROFILE,
            'add', PLUGIN+'@'+PLUGIN_VERSION, cwd=spec.cwd, env=spec.build_env(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.wait_for(process.wait(), 180)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError): raise
            raise UnsupportedCapabilityError('终端组件安装超时，请重试') from None
        if process.returncode:
            raise UnsupportedCapabilityError('终端组件安装未完成，请重试')
    if json.loads((installed/'package.json').read_text())['version'] != PLUGIN_VERSION:
        raise UnsupportedCapabilityError('DSH 终端组件版本需要适配')
    if hashlib.sha256((installed/'lib/index.js').read_bytes()).hexdigest() != 'bcc48349fb002a227b4349ac686babb580b6b4abc4c42460eabc08d0b43417d5':
        raise UnsupportedCapabilityError('DSH 终端组件需要重新验证')
    manifest = directory/'package.json'
    data = json.loads(manifest.read_text())
    data['dsh'] = {'profile':{'bundles':['@deepseek-ai/dsh-base']}}
    atomic_write_text(manifest, json.dumps(data, indent=2)+'\n')
    atomic_write_text(directory/'cordis.patch.yml', compatibility_patch((installed/'cordis.patch.yml').read_text()))
    return PROFILE


def session_patch(conversation, metadata, previous, cwd):
    mode = approval(conversation)
    if mode not in (None, 'host:ask'):
        raise UnsupportedCapabilityError('DSH 终端尚未适配当前审批模式')
    if selected(conversation, 'execution_mode'):
        raise UnsupportedCapabilityError('DSH 终端尚未适配当前运行模式')
    route = {key:previous[key] for key in ('provider','model','reasoningEffort') if previous.get(key) is not None}
    model = selected(conversation, 'model_id', 'model')
    if model:
        try:
            value = json.loads(model)
            if not isinstance(value, list) or len(value) != 2: raise ValueError
            provider, model_id = value
        except (ValueError, TypeError): raise UnsupportedCapabilityError('DSH 模型路由无效') from None
        if not all(isinstance(v,str) and v for v in (provider,model_id)):
            raise UnsupportedCapabilityError('DSH 模型路由无效')
        route.update(provider=provider,model=model_id)
    effort = selected(conversation, 'reasoning_mode', 'reasoning_effort')
    if effort: route['reasoningEffort'] = effort
    if not route.get('provider') or not route.get('model'):
        raise UnsupportedCapabilityError('请先在 Kaus 完成一轮对话，再打开 DSH 终端')
    patches = [{'id':'agent-loop','inject':['tuiStartup'], 'config':{'agents':[{'id':'main','cwd':cwd,**route}]}}]
    clients = []
    for server in metadata.get('mcpServers', []):
        config = {'serverName':server['name'],'failOnStartupError':True}
        if not server.get('type'):
            config.update(transport='stdio',command=server['command'],args=server.get('args',[]),
                env={row['name']:row['value'] for row in server.get('env',[])},cwd=cwd)
        elif server['type']=='http':
            config.update(transport='streamable-http',url=server['url'],
                headers={row['name']:row['value'] for row in server.get('headers',[])})
        else: raise UnsupportedCapabilityError('DSH 终端尚未支持此 MCP 连接类型')
        clients.append({'id':'kaus-mcp-'+hashlib.sha256(server['name'].encode()).hexdigest()[:20],
            'name':'@deepseek-ai/dsh-mcp-client','config':config})
    if clients: patches.append({'insert':clients})
    return patches


async def prepare_terminal_launch(command, spec, metadata, *, conversation, binding, history_package='@deepseek-ai/dsh'):
    # An editor-facing DSH bridge can use another entry point while sharing the
    # native session store. Resolve the terminal runtime from its own executable.
    terminal_runtime = spec.model_copy(update={'command':(command[0],)})
    await ensure_profile(terminal_runtime, command[0])
    sid = command[command.index('--resume')+1]
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    previous = (await read_session(spec, sid, cwd, package_name=history_package)).get('selection') or {}
    if history_package == 'deepseek-acp':
        updates = {}
        model = selected(conversation, 'model_id', 'model')
        if model:
            updates['model_id'] = json.dumps([previous.get('provider') or 'deepseek-official',model])
        if selected(conversation, 'execution_mode') in ('default',None):
            updates['execution_mode'] = None
        conversation = conversation.model_copy(update=updates)
    patches = session_patch(conversation, metadata, previous, cwd)
    if history_package == '@deepseek-ai/dsh':
        command=(command[0],*profile_launch_args(spec),'--patch',str(terminal_surface_patch(spec)), '--resume',sid)
    if history_package == 'deepseek-acp' and spec.build_env().get('DEEPSEEK_ACP_SESSIONS_ROOT'):
        patches.append({'id':'session-persistence-jsonl','config':{'root':spec.build_env()['DEEPSEEK_ACP_SESSIONS_ROOT']}})
    scope_file = spec.env.get('KAUS_DSH_SKILL_SCOPE')
    if scope_file:
        # The editor bridge and native terminal have different Node entry
        # packages. Keep the exact selected paths but bind the guard to the
        # verified terminal package, otherwise its entry check would skip it.
        package = locate(terminal_runtime, '@deepseek-ai/dsh')
        if not package or any(not (path := dependency(package, '@deepseek-ai/'+name)) or
            hashlib.sha256(path.read_bytes()).hexdigest()!=digest for name,digest in VERIFIED_MODULES.items()):
            raise UnsupportedCapabilityError('当前 DSH 终端技能加载版本需要适配')
        data = json.loads(Path(scope_file).read_text())
        data['package'] = str(package.resolve())
        encoded = json.dumps(data,sort_keys=True)
        target = Path(scope_file).parent/('terminal-'+hashlib.sha256(encoded.encode()).hexdigest()[:24]+'.json')
        _write(target,encoded)
        spec = spec.with_env(KAUS_DSH_SKILL_SCOPE=str(target))
    options = spec.build_env().get('NODE_OPTIONS', '')
    options += ' --import='+Path(__file__).with_name('dsh_tui_compat.mjs').as_uri()
    return (sys.executable, str(Path(__file__).with_name('dsh_terminal_exec.py')), *command), spec.with_env(**{PAYLOAD_ENV:json.dumps(patches), 'NODE_OPTIONS':options.strip()})
