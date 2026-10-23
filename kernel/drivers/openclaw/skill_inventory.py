"""Read-only local source metadata for OpenClaw's configured skill providers."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from drivers.skill_execution.common import state_root
from drivers.skill_execution.node_package import locate
from drivers.skill_execution.source_roots import root_project,workspace_roots
from drivers.skill_execution.copilot import read_jsonc


def _locations(spec):
    env=spec.build_env();home=Path(env.get('HOME') or Path.home()).expanduser()
    state=Path(env.get('OPENCLAW_STATE_DIR') or home/'.openclaw').expanduser()
    return env,home,state,Path(env.get('OPENCLAW_CONFIG_PATH') or state/'openclaw.json').expanduser()


def _cache(spec,path):
    return state_root(spec)/'native-inventory/openclaw'/(hashlib.sha256(str(path).encode()).hexdigest()[:24]+'.json')


def _stamp(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'absent'


async def refresh_inventory(spec):
    # Use the installed package's JSON5 parser, not its CLI bootstrap, so
    # inventory refresh cannot auto-migrate native settings or initialize plugins.
    _,_,_,path=_locations(spec)
    if not path.is_file():return
    if path.stat().st_size>4*1024*1024:raise UnsupportedCapabilityError('原生配置文件过大')
    package=locate(spec,'openclaw');node=shutil.which('node',path=spec.build_env().get('PATH'))
    if not package or not node:raise UnsupportedCapabilityError('原生配置解析器不可用')
    script="""const fs=require('fs');const {createRequire}=require('module');
const req=createRequire(process.argv[1]);const v=req('json5').parse(fs.readFileSync(process.argv[2],'utf8'));
const agents=v.agents||{};const entries=agents.entries||Object.fromEntries((agents.list||[]).map(a=>[a.id,a]));
console.log(JSON.stringify({skills:{load:v.skills?.load,allowBundled:v.skills?.allowBundled,entries:Object.fromEntries(Object.entries(v.skills?.entries||{}).map(([k,x])=>[k,{enabled:x.enabled}]))},plugins:{enabled:v.plugins?.enabled,allow:v.plugins?.allow,deny:v.plugins?.deny,load:v.plugins?.load,entries:Object.fromEntries(Object.entries(v.plugins?.entries||{}).map(([k,x])=>[k,{enabled:x.enabled}]))},agents:{entries:Object.fromEntries(Object.entries(entries).map(([k,x])=>[k,{workspace:x.workspace,agentDir:x.agentDir}]))}}));"""
    before=_stamp(path)
    process=await asyncio.create_subprocess_exec(node,'-e',script,str(package/'package.json'),str(path),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=spec.build_env())
    try:output,_=await asyncio.wait_for(process.communicate(),10)
    except BaseException:
        if process.returncode is None:process.kill()
        await process.wait();raise
    if process.returncode or _stamp(path)!=before:raise UnsupportedCapabilityError('原生配置无法稳定读取')
    value=json.loads(output);cache=_cache(spec,path);cache.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_write_text(cache,json.dumps({'stamp':before,'config':value}));cache.chmod(0o600)


def _config(spec,path):
    try:
        cached=json.loads(_cache(spec,path).read_text())
        if cached.get('stamp')==_stamp(path):return cached['config']
    except (OSError,ValueError,KeyError):pass
    try:return read_jsonc(path)
    except UnsupportedCapabilityError:return None


def inventory_roots(spec,projects,bindings):
    projects=tuple(projects);pid=root_project(projects)
    if not pid:return ()
    env,home,state,path=_locations(spec);config=_config(spec,path)
    if config is None:return () # Probe refresh parses native JSON5 before seeding.
    package=locate(spec,'openclaw');roots=[];skill_config=config.get('skills',{})
    def add(root,owner=pid,blocked=False):
        roots.append(NativeSkillRoot(Path(root),'openclaw','OpenClaw',owner,blocked_project_ids=(owner,) if blocked else ()))
    add(state/'skills')
    if state.resolve()==(home/'.openclaw').resolve():add(home/'.agents/skills')
    for value in skill_config.get('load',{}).get('extraDirs',[]):
        if isinstance(value,str):add(Path(value).expanduser())
    bundled=Path(env['OPENCLAW_BUNDLED_SKILLS_DIR']).expanduser() if env.get('OPENCLAW_BUNDLED_SKILLS_DIR') else package/'skills' if package else None
    if bundled and bundled.is_dir():
        allow=skill_config.get('allowBundled')
        for child in bundled.iterdir():
            if child.is_dir():add(child,blocked=isinstance(allow,list) and child.name not in allow)
    work=workspace_roots(projects,bindings,agent_key='openclaw',label='OpenClaw',folders=('',))
    for source in work:
        add(source.path/'skills',source.project_id);add(source.path/'.agents/skills',source.project_id)
    agents=config.get('agents',{}).get('entries',{})
    for agent,value in agents.items() if isinstance(agents,dict) else ():
        if not isinstance(value,dict) or not isinstance(value.get('workspace'),str):continue
        owners={r.project_id for r in work if r.path.resolve()==Path(value['workspace']).expanduser().resolve()}
        for owner in owners:add(Path(value.get('agentDir') or state/'agents'/agent/'agent')/'workshop-skills',owner)
    plugins=config.get('plugins',{});entries=plugins.get('entries',{})
    candidates=[]
    if package and (package/'dist/extensions').is_dir():candidates.extend((p,pid,True) for p in (package/'dist/extensions').iterdir() if p.is_dir())
    if (state/'extensions').is_dir():candidates.extend((p,pid,False) for p in (state/'extensions').iterdir() if p.is_dir())
    candidates.extend((Path(p).expanduser(),pid,False) for p in plugins.get('load',{}).get('paths',[]) if isinstance(p,str))
    for source in work:
        directory=source.path/'.openclaw/extensions'
        if directory.is_dir():candidates.extend((p,source.project_id,False) for p in directory.iterdir() if p.is_dir())
    for folder,owner,builtin in candidates:
        try:manifest=json.loads((folder/'openclaw.plugin.json').read_text())
        except (OSError,ValueError):continue
        key=manifest.get('id');entry=entries.get(key,{})
        enabled=entry.get('enabled',manifest.get('enabledByDefault',False) if builtin else True)
        enabled=enabled and plugins.get('enabled') is not False and key not in plugins.get('deny',[])
        allow=plugins.get('allow');enabled=enabled and (not isinstance(allow,list) or key in allow)
        for value in manifest.get('skills',[]):
            if isinstance(value,str) and not Path(value).is_absolute() and '..' not in Path(value).parts:add(folder/value,owner,not enabled)
    # Retain native per-skill disablement without losing discoverability.
    from drivers.skill_inventory import discover_skills
    disabled={k for k,v in skill_config.get('entries',{}).items() if isinstance(v,dict) and v.get('enabled') is False}
    if disabled:
        for row in discover_skills(roots):
            if row['name'] in disabled:
                roots.append(NativeSkillRoot(Path(row['sourcePath']).parent,'openclaw','OpenClaw',row['defaultProjectId'],blocked_project_ids=tuple(row['defaultProjectIds'])))
    return tuple(roots)
