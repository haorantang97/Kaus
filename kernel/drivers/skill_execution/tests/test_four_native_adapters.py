"""Real installed parsers; isolated HOME, synthetic files, no model calls."""
import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from app.capabilities.models import EffectiveCapabilities
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import dsh, qwen
from test_selection import setup, effective


def native_spec(tmp_path, backend):
    executable = os.environ.get('KAUS_TEST_' + backend.upper()) or shutil.which(backend)
    if not executable:
        pytest.skip(backend + ' native CLI not installed')
    initial, binding = setup(tmp_path, backend)
    home = tmp_path / 'home'
    home.mkdir()
    return AcpAgentSpec(command=(executable,), cwd=str(tmp_path), inherit_env=False, env={
        **initial.env, 'PATH': os.environ['PATH'], 'HOME': str(home),
        'DSH_HOME': str(home / '.dsh'), 'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_CACHE_HOME': str(home / '.cache'),
    }), binding


async def node(plan, source, *, probe):
    proc = await asyncio.create_subprocess_exec(shutil.which('node', path=plan.spec.build_env()['PATH']),
        '--input-type=module', '-e', source, cwd=plan.spec.cwd,
        env={**plan.spec.build_env(), probe: '1'}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, error = await asyncio.wait_for(proc.communicate(), 20)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    assert proc.returncode == 0, error.decode()[-3000:]
    return out.decode()


@pytest.mark.parametrize('adapter,backend', [(dsh, 'dsh'), (qwen, 'qwen')])
async def test_native_mode_does_not_require_installation(tmp_path, adapter, backend):
    spec, binding = setup(tmp_path, backend)
    plan = await adapter.project(spec, binding, EffectiveCapabilities(project_id=binding.project_id), str(tmp_path))
    assert plan.spec == spec and plan.root is None


@pytest.mark.parametrize('names', [('common',), ()])
async def test_installed_dsh_restricts_runtime_and_provider_additions(tmp_path, names):
    spec, binding = native_spec(tmp_path, 'dsh')
    plan = await dsh.project(spec, binding, effective(tmp_path, binding, names), str(tmp_path))
    package = dsh.locate(spec, '@deepseek-ai/dsh')
    imports = {name: dsh.dependency(package, '@deepseek-ai/' + name).as_uri()
               for name in ['cordis', 'dsh-skill', 'dsh-skill-filesystem']}
    blocked = tmp_path / '.agents/skills/blocked/SKILL.md'
    blocked.parent.mkdir(parents=True)
    blocked.write_text('---\nname: blocked\ndescription: synthetic\n---\nBLOCKED')
    source = f'''
import assert from 'node:assert/strict';
const {{Context}}=await import({json.dumps(imports['cordis'])});
const {{SkillRegistry}}=await import({json.dumps(imports['dsh-skill'])});
const {{FileSystemSkillProvider}}=await import({json.dumps(imports['dsh-skill-filesystem'])});
const ctx=new Context();ctx.provide('logger',{{warn(){{}}}});const r=new SkillRegistry(ctx);
let provider;r.registerProvider(control=>provider=new FileSystemSkillProvider(ctx,control,{{watch:false}}));
const expected={json.dumps(list(plan.names))};
assert.deepEqual((await r.list()).map(x=>x.name),expected);
const invocation={{modelInvocable:true,userInvocable:true}};
r.register({{name:'runtime-blocked',description:'synthetic',invocation,content:'BLOCKED'}});
if(expected.length)r.register({{name:expected[0],description:'shadow',invocation,content:'BLOCKED'}});
r.registerProvider(()=>({{name:'late-badge',async list(){{return [{{name:'badge-blocked',description:'synthetic',source:'badge',provider:'late-badge',rank:0,invocation,locator:{{path:{json.dumps(str(blocked))}}}}}]}},async get(){{throw Error('outside provider read')}}}}));
assert.deepEqual((await r.list()).map(x=>x.name),expected);
assert.equal(await r.get('runtime-blocked'),undefined);assert.equal(await r.get('badge-blocked'),undefined);
for(const name of expected)assert.match((await r.get(name)).content,/BODY_common/);
await provider.dispose();console.log('native registry dynamic scope passed');'''
    assert 'passed' in await node(plan, source, probe='KAUS_DSH_SCOPE_PROBE')
    assert plan.spec.env['DSH_HOME'] == spec.env['DSH_HOME']


@pytest.mark.parametrize('names', [('common',), ()])
async def test_installed_qwen_refresh_extension_and_cache_are_scoped(tmp_path, names):
    spec, binding = native_spec(tmp_path, 'qwen')
    plan = await qwen.project(spec, binding, effective(tmp_path, binding, names), str(tmp_path))
    scope = json.loads(Path(plan.spec.env['KAUS_QWEN_SKILL_SCOPE']).read_text())
    blocked = tmp_path / 'extension/SKILL.md'
    blocked.parent.mkdir()
    blocked.write_text('---\nname: extension-blocked\ndescription: synthetic\n---\nBLOCKED')
    source = f'''
import assert from 'node:assert/strict';
const {{SkillManager}}=await import({json.dumps(Path(scope['module']).as_uri())});
const extension={{name:'extension',skills:[{{name:'blocked',filePath:{json.dumps(str(blocked))},description:'synthetic'}}]}};
const config={{getBareMode:()=>false,isSafeMode:()=>false,getProjectRoot:()=>process.cwd(),getActiveExtensions:()=>[extension]}};
const m=new SkillManager(config);const expected={json.dumps(list(plan.names))};
assert.deepEqual((await m.listSkills()).map(x=>x.name),expected);
assert.equal(await m.loadSkill('extension:blocked'),null);
extension.skills.push({{name:'late',description:'synthetic',filePath:{json.dumps(str(blocked))}}});
await m.refreshCache();assert.deepEqual(m.getCachedSkills().map(x=>x.name),expected);
m.skillsCache.set('extension',extension.skills);
assert.deepEqual(m.getCachedSkills().map(x=>x.name),expected);assert.equal(await m.loadSkill('late'),null);
for(const name of expected)assert.match((await m.loadSkillForRuntime(name)).body,/BODY_common/);
console.log('native qwen dynamic scope passed');'''
    assert 'passed' in await node(plan, source, probe='KAUS_QWEN_SCOPE_PROBE')


@pytest.mark.parametrize('adapter,backend', [(dsh, 'dsh'), (qwen, 'qwen')])
async def test_unknown_loader_fails_managed_before_launch(tmp_path, monkeypatch, adapter, backend):
    spec, binding = setup(tmp_path, backend)
    package = tmp_path / 'unverified'
    package.mkdir()
    monkeypatch.setattr(adapter, 'locate', lambda *_: package)
    with pytest.raises(UnsupportedCapabilityError, match='尚未验证'):
        await adapter.project(spec, binding, effective(tmp_path, binding), str(tmp_path))


@pytest.mark.parametrize('selected', [True, False])
async def test_qwen_hardcoded_references_use_selected_copy_or_are_withheld(tmp_path, selected):
    spec, binding = native_spec(tmp_path, 'qwen')
    names = ('workflow-authoring',) if selected else ()
    plan = await qwen.project(spec, binding, effective(tmp_path, binding, names), str(tmp_path))
    scope = json.loads(Path(plan.spec.env['KAUS_QWEN_SKILL_SCOPE']).read_text())
    source = f'''
import assert from 'node:assert/strict';
const {{readBundledReference,resolveBundledReferenceSurface,SkillManager}}=await import({json.dumps(Path(scope['module']).as_uri())});
assert.equal(readBundledReference('agent-delegation'),null);
assert.equal(resolveBundledReferenceSurface({{}},'agent-delegation'),'withheld');
const actual=readBundledReference('workflow-authoring');
if({str(selected).lower()}){{
 assert.match(actual.body,/BODY_workflow-authoring/);
 assert.equal(resolveBundledReferenceSurface({{}},'workflow-authoring'),'inline');
 const m=new SkillManager({{getBareMode:()=>false,isSafeMode:()=>false,getProjectRoot:()=>process.cwd()}});
 assert.deepEqual((await m.listSkills()).map(x=>x.name),['workflow-authoring']);
 assert.match((await m.loadSkillForRuntime('workflow-authoring')).body,/BODY_workflow-authoring/);
}}else{{assert.equal(actual,null);assert.equal(resolveBundledReferenceSurface({{}},'workflow-authoring'),'withheld');}}
console.log('native hardcoded reference scope passed');'''
    assert 'passed' in await node(plan, source, probe='KAUS_QWEN_SCOPE_PROBE')


@pytest.mark.parametrize('adapter_name,backend', [('kilo', 'kilo'), ('omp', 'omp')])
async def test_additional_native_modes_do_not_require_installation(tmp_path, adapter_name, backend):
    from importlib import import_module
    adapter = import_module('drivers.skill_execution.' + adapter_name)
    spec, binding = setup(tmp_path, backend)
    plan = await adapter.project(spec, binding, EffectiveCapabilities(project_id=binding.project_id), str(tmp_path))
    assert plan.spec == spec and plan.root is None


def test_omp_skill_overlay_is_last_without_dropping_unrelated_flags():
    from drivers.skill_execution.omp import scoped_command
    command, config = scoped_command(('omp', 'acp', '--config', 'one.yml', '--skills=old-*', '--model', 'fixture', '--config=two.yml', '--no-skills', '--extension', 'plugin.js'))
    assert config == ['one.yml', 'two.yml']
    assert command == ('omp', 'acp', '--model', 'fixture', '--extension', 'plugin.js')
