"""Pi's native no-discovery switch plus explicit Skill paths.

The ACP bridge accepts a single executable path. Its immutable launcher is used
for both new sessions and session/load, without replacing the user's agent home.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from drivers.session_mcp.display import filter_tool_title
from .common import materialize, unsupported
from .node_package import locate


async def refresh_inventory(spec):
    from .native_catalog import query, save_paths
    env = spec.build_env()
    executable = env.get('PI_ACP_PI_COMMAND') or shutil.which('pi', path=env.get('PATH'))
    package = locate(spec.model_copy(update={'command': (executable,)}), '@mariozechner/pi-coding-agent') if executable else None
    node = shutil.which('node', path=env.get('PATH'))
    if not package or not node:
        raise ValueError('原生技能来源不可用')
    base = package / 'dist/core'
    # Package resolution reads manifests only. Missing packages are skipped;
    # extension code and settings writes are never used for inventory.
    script = "\n".join([
        "import fs from 'node:fs'; import path from 'node:path';",
        f"import {{DefaultPackageManager}} from {json.dumps((base / 'package-manager.js').as_uri())};",
        f"import {{loadSkills}} from {json.dumps((base / 'skills.js').as_uri())};",
        f"import {{SettingsManager}} from {json.dumps((base / 'settings-manager.js').as_uri())};",
        f"import {{getAgentDir}} from {json.dumps((package / 'dist/config.js').as_uri())};",
        "const cwd=process.cwd(), agentDir=getAgentDir();",
        "const read=p=>{try{return fs.readFileSync(p,'utf8')}catch{return undefined}};",
        "const settingsManager=SettingsManager.fromStorage({withLock:(scope,fn)=>{fn(read(scope==='global'?path.join(agentDir,'settings.json'):path.join(cwd,'.pi/settings.json')))}});",
        "const manager=new DefaultPackageManager({cwd,agentDir,settingsManager});",
        "const resolved=await manager.resolve(async()=> 'skip');",
        "const skills=loadSkills({cwd,agentDir,includeDefaults:false,skillPaths:resolved.skills.filter(r=>r.enabled).map(r=>r.path)}).skills;",
        "console.log('KAUS_INVENTORY='+JSON.stringify(skills.map(s=>s.filePath)));",
    ])
    raw = await query(spec, (node, '--input-type=module', '-e', script))
    line = next((line for line in reversed(raw.splitlines()) if line.startswith('KAUS_INVENTORY=')), None)
    if not line:
        raise ValueError('原生技能目录格式不兼容')
    save_paths(spec, 'pi', json.loads(line.partition('=')[2]))


def inventory_roots(spec, projects, bindings):
    from .native_catalog import cached_roots
    return cached_roots(spec, 'pi', 'Pi', projects, bindings)


async def guard_spec(plan, spec, executable, cwd):
    native = spec.model_copy(update={"command": (str(executable),)})
    package = locate(native, '@mariozechner/pi-coding-agent')
    module = package / 'dist/core/resource-loader.js' if package else None
    node = shutil.which('node', path=spec.build_env().get('PATH'))
    if module is None or not module.is_file() or not node:
        raise UnsupportedCapabilityError('当前原生安装未提供可验证的扩展技能加载接口')
    source = Path(__file__).with_name('pi_guard.mjs').read_text()
    guard = plan.root / ('pi-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    data = {'package': str(package.resolve()), 'module': str(module.resolve()),
            'moduleHash': hashlib.sha256(module.read_bytes()).hexdigest(), 'paths': [str(p) for p in plan.paths]}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('pi-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    for path, content in ((guard, source), (manifest, encoded)):
        if path.exists():
            if path.is_symlink() or path.read_text() != content:
                raise UnsupportedCapabilityError('项目技能加载配置不一致')
        else:
            atomic_write_text(path, content)
    result = spec.with_env(KAUS_PI_SKILL_SCOPE=str(manifest),
        NODE_OPTIONS=(spec.build_env().get('NODE_OPTIONS', '') + ' --import=' + guard.as_uri()).strip())
    script = "const {DefaultResourceLoader}=await import(" + json.dumps(module.as_uri()) + ");const loader=new DefaultResourceLoader({cwd:process.cwd(),agentDir:" + json.dumps(str(plan.root)) + ",noSkills:true,noExtensions:true});loader.updateSkillsFromPaths([]);if(loader.getSkills().skills.length!==" + str(len(plan.paths)) + ")throw Error('incomplete');"
    process = await asyncio.create_subprocess_exec(node, '--input-type=module', '-e', script,
        env={**result.build_env(), 'KAUS_PI_SCOPE_PROBE': '1'}, cwd=cwd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), 20)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedCapabilityError('原生技能加载验证超时')
    if process.returncode:
        raise UnsupportedCapabilityError('原生技能加载验证失败')
    return result


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    env = spec.build_env()
    executable = env.get("PI_ACP_PI_COMMAND") or shutil.which("pi", path=env.get("PATH"))
    if not executable or os.name == "nt":
        return unsupported(binding, "项目技能范围需要支持 --no-skills 的原生 CLI", dry_run=dry_run)
    try:
        process = await asyncio.create_subprocess_exec(executable, "--help", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("原生 CLI 检测超时")
        if process.returncode or b"--no-skills" not in output or b"--skill" not in output:
            raise ValueError("原生 CLI 未提供技能加载范围参数")
    except (OSError, ValueError) as exc:
        return unsupported(binding, str(exc), dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout="roots", dry_run=dry_run)
    plan.spec = spec
    if plan.root is None or dry_run:
        return plan
    args = [str(Path(executable).absolute()), "--no-skills"]
    for path in plan.paths:
        args.extend(("--skill", str(path)))
    script = "#!/bin/sh\nexec " + shlex.join(args) + ' "$@"\n'
    launcher = plan.root / ("native-launch-" + hashlib.sha256(script.encode()).hexdigest()[:24] + ".sh")
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_text() != script:
            raise UnsupportedCapabilityError("原生技能启动配置不一致")
    else:
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    plan.spec = await guard_spec(plan, spec.with_env(PI_ACP_PI_COMMAND=str(launcher)), executable, workspace_root or spec.cwd)
    return plan


def cli_command(plan, command):
    if plan.root is None:
        return tuple(command)
    spec = _mcp.restore_native_spec(plan.spec, kind='pi') if plan.spec else None
    launcher = spec.env.get("PI_ACP_PI_COMMAND") if spec else None
    manifest = spec.env.get('KAUS_PI_SKILL_SCOPE') if spec else None
    source = Path(__file__).with_name('pi_guard.mjs').read_text()
    guard = plan.root / ('pi-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.mjs')
    if not launcher or not manifest or not guard.is_file() or guard.is_symlink() or guard.read_text() != source:
        raise UnsupportedCapabilityError("项目技能启动配置缺失")
    path = Path(launcher)
    if not path.is_file() or path.is_symlink() or path.name != 'native-launch-' + hashlib.sha256(path.read_bytes()).hexdigest()[:24] + '.sh':
        raise UnsupportedCapabilityError('项目技能启动配置已变化')
    if not command:
        return ()
    # The connection's NODE_OPTIONS stays in the private relay, never in a file.
    script = '#!/bin/sh\nexport KAUS_PI_SKILL_SCOPE=' + shlex.quote(manifest) + '\n'
    script += 'export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }"' + shlex.quote('--import=' + guard.as_uri()) + '\n'
    script += 'exec ' + shlex.quote(launcher) + ' "$@"\n'
    target = plan.root / ('pi-terminal-' + hashlib.sha256(script.encode()).hexdigest()[:24] + '.sh')
    if target.exists():
        if target.is_symlink() or target.read_text() != script:
            raise UnsupportedCapabilityError('终端技能配置已变化')
    else:
        atomic_write_text(target, script)
    target.chmod(0o700)
    return (str(target), *command[1:])


from .pi_runtime import prepare_runtime_spec as _prepare_rpc_spec
from drivers.session_mcp import projection as _mcp

remember_session_options = _mcp.remember


async def prepare_runtime_spec(spec, conversation, metadata):
    spec = _mcp.restore_native_spec(spec, kind='pi')
    spec = await _prepare_rpc_spec(spec, conversation, metadata)
    return await _mcp.prepare(spec, conversation, metadata, kind="pi")


async def before_runtime_session(connection, spec, conversation, metadata):
    return await _mcp.before_session(connection, spec, conversation, metadata, kind="pi")


async def after_runtime_session(connection, spec, conversation, metadata, session_id):
    await _mcp.verify(spec)


async def load_local_native_history(spec, binding, native_session_id):
    """Read existing local session files only; never start or contact an agent."""
    return await load_native_history(spec, binding, native_session_id)


async def load_native_history(spec, binding, native_session_id):
    from .history_io import canonical_id, read_bytes, tree_history
    import re
    ident = canonical_id(native_session_id)
    env = spec.build_env()
    home = Path(env.get('HOME') or Path.home())
    root = Path(env.get('PI_CODING_AGENT_DIR') or home / '.pi/agent')
    cwd = binding.runtime_config.get('workspace_root') or spec.cwd
    if not cwd:
        raise UnsupportedCapabilityError('原生会话工作目录缺失')
    folder = Path('sessions') / ('--' + re.sub(r'[/\\:]', '-', re.sub(r'^[/\\]', '', cwd)) + '--')
    try:
        candidates = [p for p in (root / folder).iterdir() if p.name.endswith('_' + ident + '.jsonl')]
        if len(candidates) != 1:
            raise ValueError('ambiguous native transcript')
        return tree_history(read_bytes(root, folder / candidates[0].name), ident=ident, cwd=cwd)
    except (OSError, ValueError, TypeError):
        raise RuntimeError('原生会话历史暂不可读') from None


def external_cli_command(plan, command, *, conversation, binding, spec):
    from .terminal_settings import approval, arguments, choice, selected
    if approval(conversation) is not None:
        raise UnsupportedCapabilityError('原生终端未提供当前审批模式')
    if selected(conversation, 'execution_mode'):
        raise UnsupportedCapabilityError('原生终端未提供当前运行模式')
    effort = choice(selected(conversation, 'reasoning_mode', 'thought_level'),
        {'default': None, 'off': 'off', 'minimal': 'minimal', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh'}, '思考强度')
    command = arguments(command, ('--model', selected(conversation, 'model_id', 'model')), ('--thinking', effort))
    command = cli_command(plan, command) if plan is not None else command
    return _mcp.terminal(spec, conversation, command, kind="pi")
