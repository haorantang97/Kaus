import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import cursor as adapter
from test_selection import setup, effective


INDEX = '''const fs=require('node:fs'),path=require('node:path');
class Ts {async loadAgentStoreSkills(roots){const rows=roots.flatMap(root=>{const p=path.join(root,'SKILL.md');return fs.existsSync(p)?[{fullPath:p,content:fs.readFileSync(p,'utf8')}]:[]});return {rules:rows,agentSkills:rows,realSkillFilePathsByFullPath:new Map(rows.map(row=>[row.fullPath,row.fullPath]))}};async loadAll(){throw Error('automatic roots')};computeFoldersToWatch(){return ['/native']};reloadAtPath(){throw Error('dynamic native')};reload(){return this.loadAll()}}
class Os {constructor(n){this.pluginsService=n,this.onChangeCallbacks=new Set,this.currentImportSettings={}}}
class Tp {constructor(services){this.services=services}async getAllAgentSkills(){return (await Promise.all(this.services.map(s=>s.getAllAgentSkills()))).flat()}}
class xs {constructor(services){this.services=services}async getAllCursorRules(){return (await Promise.all(this.services.map(s=>s.getAllCursorRules()))).flat()}}
Tp.CHANGE_COALESCE_MS=1500;
module.exports={Ts,Os,Tp,xs};
'''
CHUNK = '''class Custom {loadSkillRoots(e){return e}parseSkillMarkdown(e,t,r,n){return {id:e,body:t,source:r,path:n}}}module.exports=Custom;'''


def installed(tmp_path, monkeypatch):
    package = tmp_path / 'native'; package.mkdir()
    launcher = package / 'cursor-agent'; launcher.write_text('#!/bin/sh\nexit 0\n'); launcher.chmod(0o700)
    for name, text in [('index.js', INDEX), ('1348.index.js', CHUNK)]: (package / name).write_text(text)
    monkeypatch.setattr(adapter, 'LAUNCHER_HASH', hashlib.sha256(launcher.read_bytes()).hexdigest())
    monkeypatch.setattr(adapter, 'MODULE_HASHES', {name:hashlib.sha256((package/name).read_bytes()).hexdigest() for name in ['index.js','1348.index.js']})
    spec,binding=setup(tmp_path,'cursor')
    return spec.model_copy(update={'command':(str(launcher),'acp'),'env':{**spec.env,'HOME':'/identity-stays','NODE_OPTIONS':'--no-warnings'}}),binding,package


async def test_native_noop_unknown_version_preview_and_managed_failure(tmp_path, monkeypatch):
    spec,binding,package=installed(tmp_path,monkeypatch)
    value=effective(tmp_path,binding)
    (package/'index.js').write_text('updated')
    assert (await adapter.project(spec,binding,value,tmp_path,dry_run=True)).report.unsupported
    with pytest.raises(UnsupportedCapabilityError):await adapter.project(spec,binding,value,tmp_path)
    value.entries[0].config['mode']='native'
    native=await adapter.project(spec,binding,value,tmp_path)
    assert native.root is None and native.spec==spec
    assert not (tmp_path/'state').exists()


async def test_snapshots_are_immutable_independent_and_keep_identity(tmp_path, monkeypatch):
    spec,binding,package=installed(tmp_path,monkeypatch)
    wrapper=tmp_path/'wrapper';wrapper.write_text('#!/bin/sh\nexec '+str(package/'cursor-agent')+' "$@"\n');wrapper.chmod(0o700)
    assert adapter.package_path(spec.model_copy(update={'command':(str(wrapper),'acp')}))==package
    first=await adapter.project(spec,binding,effective(tmp_path,binding),tmp_path)
    before=Path(first.spec.env['KAUS_CURSOR_SKILL_SCOPE']).read_bytes()
    second=await adapter.project(spec,binding,effective(tmp_path,binding,()),tmp_path)
    assert first.spec.env['HOME']==spec.env['HOME'] and first.spec.command==spec.command and first.spec.cwd==spec.cwd
    assert first.spec.env['NODE_OPTIONS'].startswith('--no-warnings --require=')
    assert Path(first.spec.env['KAUS_CURSOR_SKILL_SCOPE']).read_bytes()==before
    assert json.loads(Path(second.spec.env['KAUS_CURSOR_SKILL_SCOPE']).read_text())['paths']==[]
    with pytest.raises(UnsupportedCapabilityError):adapter.cli_command(first,('cursor-agent','--resume','same-id'))


async def test_native_service_guard_covers_dynamic_sources_plugins_and_slash_parser(tmp_path,monkeypatch):
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable')
    spec,binding,package=installed(tmp_path,monkeypatch)
    # A non-ASCII/spaced path exercises the actual NODE_OPTIONS parser.
    state=tmp_path/'项目 技能';spec=spec.with_env(KAUS_SKILL_STATE_ROOT=str(state))
    plan=await adapter.project(spec,binding,effective(tmp_path,binding),tmp_path)
    manifest=json.loads(Path(plan.spec.env['KAUS_CURSOR_SKILL_SCOPE']).read_text());chosen=manifest['paths'][0]
    forbidden=tmp_path/'outside/SKILL.md';forbidden.parent.mkdir();forbidden.write_text('FORBIDDEN')
    script='''const assert=require('node:assert'),path=require('node:path');
const [index,chunk,chosen,blocked]=process.argv.slice(1),{Ts,Os,Tp,xs}=require(index),Custom=require(chunk);
(async()=>{const native=new Ts;const loaded=await native.loadAll();assert.deepStrictEqual(loaded.agentSkills.map(x=>x.fullPath),[chosen]);
assert.deepStrictEqual(native.computeFoldersToWatch(),[path.dirname(chosen)]);
let reloads=0;native.reload=()=>{reloads++};native.reloadAtPath(null,blocked);assert.strictEqual(reloads,1);assert.deepStrictEqual((await native.loadAll()).agentSkills.map(x=>x.fullPath),[chosen]);
const originals=[{installPath:path.dirname(blocked),skills:[{path:'SKILL.md'}],rules:[{path:'keep.mdc'}],mcpServers:{keep:{command:'retained'}}}];
const service={getAllEnabledPlugins:async()=>originals,reload:async()=>originals};const plugin=new Os(service);
const rows=await plugin.pluginsService.getAllEnabledPlugins();assert.deepStrictEqual(rows[0].skills,[]);assert.strictEqual(rows[0].rules,originals[0].rules);assert.strictEqual(rows[0].mcpServers,originals[0].mcpServers);assert.strictEqual(originals[0].skills.length,1);
const skills=new Tp([{getAllAgentSkills:async()=>[{fullPath:chosen},{fullPath:blocked}]}]);assert.deepStrictEqual((await skills.getAllAgentSkills()).map(x=>x.fullPath),[chosen]);
const rules=new xs([{getAllCursorRules:async()=>[{fullPath:chosen},{fullPath:blocked},{fullPath:'/normal/keep.mdc'}]}]);assert.deepStrictEqual((await rules.getAllCursorRules()).map(x=>x.fullPath),[chosen,'/normal/keep.mdc']);
const custom=new Custom;assert.strictEqual(custom.parseSkillMarkdown('bad','BODY','workspace',blocked),null);assert.strictEqual(custom.parseSkillMarkdown('ok','BODY','workspace',chosen).body,'BODY');assert.deepStrictEqual(custom.loadSkillRoots([{dirPath:'/unselected'}]),[{dirPath:path.dirname(chosen),source:'workspace'}]);
console.log('verified')})().catch(e=>{console.error(e);process.exitCode=1});'''
    result=subprocess.run((node,'-e',script,str(package/'index.js'),str(package/'1348.index.js'),chosen,str(forbidden)),env=plan.spec.build_env(),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='verified'
    (package/'1348.index.js').write_text('changed')
    result=subprocess.run((node,'-e','0'),env=plan.spec.build_env(),text=True,capture_output=True,timeout=10)
    assert result.returncode!=0 and 'version changed' in result.stderr
