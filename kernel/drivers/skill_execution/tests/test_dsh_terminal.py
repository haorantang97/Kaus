import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

from app.conversations.models import Conversation
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import dsh_terminal as adapter
from test_selection import setup


def context(tmp_path, **updates):
    spec, binding = setup(tmp_path, 'dsh')
    spec = spec.with_env(DSH_HOME=str(tmp_path/'native'))
    conv = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id,
                               title='Synthetic').model_copy(update=updates)
    return spec, binding, conv


def test_patch_preserves_route_and_scopes_mcp_without_changing_native_files(tmp_path):
    spec, binding, conv = context(tmp_path, reasoning_mode='high', approval_mode='host:ask')
    metadata = {'mcpServers': [{'name':'project','command':'fixture','args':['--one'],
                              'env':[{'name':'TOKEN','value':'synthetic-secret'}]},
                             {'name':'remote','type':'http','url':'https://example.invalid/mcp',
                              'headers':[{'name':'Authorization','value':'synthetic-secret'}]}]}
    patch = adapter.session_patch(conv, metadata, {'provider':'original','model':'native','reasoningEffort':'low'}, str(tmp_path))
    assert patch[0]['config']['agents'] == [{'id':'main','cwd':str(tmp_path),'provider':'original','model':'native','reasoningEffort':'high'}]
    servers = patch[1]['insert']
    assert servers[0]['config']['env'] == {'TOKEN':'synthetic-secret'}
    assert servers[1]['config']['transport'] == 'streamable-http'
    assert not list(tmp_path.iterdir())
    conv = conv.model_copy(update={'model_id':'["another", "selected"]'})
    assert adapter.session_patch(conv, {}, {}, str(tmp_path))[0]['config']['agents'][0]['provider'] == 'another'


@pytest.mark.parametrize('model', ['null','{}','[1,2]','["only"]','"ab"','broken'])
def test_invalid_model_route_is_not_silently_changed(tmp_path, model):
    _, _, conv = context(tmp_path, model_id=model)
    with pytest.raises(UnsupportedCapabilityError, match='模型路由'):
        adapter.session_patch(conv, {}, {}, str(tmp_path))


def test_patch_does_not_turn_host_approval_into_native_bypass(tmp_path):
    _, _, conv = context(tmp_path, approval_mode='host:auto')
    with pytest.raises(UnsupportedCapabilityError, match='自动审批'):
        adapter.session_patch(conv, {}, {'provider':'a','model':'b'}, str(tmp_path))


def test_anonymous_patch_carries_secrets_without_persistent_file(tmp_path):
    native = tmp_path/'native.py'
    native.write_text('import os,sys,json\nassert "KAUS_DSH_TERMINAL_PATCH" not in os.environ\n'
                      'assert sys.argv[1:3] == ["--profile", "kaus-tui"]\n'
                      'assert sys.argv[-2:] == ["--resume", "same-session"]\n'
                      'assert json.load(open(sys.argv[4])) == [{"token":"synthetic-secret"}]\nprint("verified")\n')
    command = [sys.executable, str(Path(adapter.__file__).with_name('dsh_terminal_exec.py')),
               sys.executable, str(native), '--profile','kaus-tui','--resume','same-session']
    result = subprocess.run(command, env={**os.environ, adapter.PAYLOAD_ENV:json.dumps([{'token':'synthetic-secret'}])},
                            capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'verified'
    assert not list(tmp_path.glob('*.json'))


async def test_installer_will_not_adopt_user_profile_or_redirected_directory(tmp_path, monkeypatch):
    spec, _, _ = context(tmp_path)
    package = tmp_path/'package';package.mkdir();(package/'package.json').write_text('{"version":"0.1.5-rc.3"}')
    monkeypatch.setattr(adapter, 'locate', lambda *args: package)
    profile = tmp_path/'native/profiles/kaus-tui';profile.mkdir(parents=True)
    (profile/'personal.txt').write_text('preserve')
    with pytest.raises(UnsupportedCapabilityError, match='占用'):
        await adapter.ensure_profile(spec, '/never-executed')
    assert (profile/'personal.txt').read_text() == 'preserve'


def test_profile_removes_only_duplicate_services():
    source = 'patches:\n  - insert:\n' + ''.join('    - id: '+name+'\n      name: package\n' for name in
        ('storage','storage-json','storage-domain','session-projection-cache','tui','questions'))
    result = adapter.compatibility_patch(source)
    assert 'id: tui' in result and 'id: questions' in result
    assert 'id: storage' not in result and 'id: session-projection-cache' not in result
    with pytest.raises(UnsupportedCapabilityError): adapter.compatibility_patch('unknown version')


async def test_editor_bridge_retargets_only_terminal_skill_scope(tmp_path, monkeypatch):
    """A DeepSeek editor entry must not make the DSH TUI skip the skill guard."""
    import hashlib
    spec, binding, conv = context(tmp_path, model_id='deepseek-flash', execution_mode='default')
    package = tmp_path/'terminal-package'; package.mkdir()
    module = package/'skill.mjs'; module.write_text('synthetic verified module')
    monkeypatch.setattr(adapter, 'VERIFIED_MODULES', {'skill':hashlib.sha256(module.read_bytes()).hexdigest()})
    monkeypatch.setattr(adapter, 'dependency', lambda *args: module)
    monkeypatch.setattr(adapter, 'locate', lambda *args: package)
    async def installed(*args): return adapter.PROFILE
    async def previous(*args, **kwargs): return {'selection':{'provider':'original','model':'previous'}}
    monkeypatch.setattr(adapter, 'ensure_profile', installed)
    monkeypatch.setattr(adapter, 'read_session', previous)
    scope = tmp_path/'editor-scope.json'
    content = {'package':str(tmp_path/'editor-package'), 'paths':['selected/SKILL.md'],
               'root':'selected', 'hashes':{'skill':'verified'}}
    scope.write_text(json.dumps(content))
    spec = spec.with_env(KAUS_DSH_SKILL_SCOPE=str(scope), DEEPSEEK_ACP_SESSIONS_ROOT=str(tmp_path/'custom-sessions'))
    _, terminal = await adapter.prepare_terminal_launch(('dsh','--profile','kaus-tui','--resume','same-session'),
        spec, {}, conversation=conv, binding=binding, history_package='deepseek-acp')
    assert json.loads(scope.read_text()) == content
    terminal_scope = json.loads(Path(terminal.env['KAUS_DSH_SKILL_SCOPE']).read_text())
    assert terminal_scope == {**content,'package':str(package.resolve())}
    assert spec.env['KAUS_DSH_SKILL_SCOPE'] == str(scope)
    patches = json.loads(terminal.env[adapter.PAYLOAD_ENV])
    assert patches[0]['config']['agents'][0]['model'] == 'deepseek-flash'
    assert patches[-1] == {'id':'session-persistence-jsonl','config':{'root':str(tmp_path/'custom-sessions')}}


def test_history_matches_workspace_alias_but_rejects_another_directory(tmp_path):
    node=shutil.which('node')
    if not node: pytest.skip('Node.js is required for the native decoder contract')
    real=tmp_path/'real';real.mkdir();alias=tmp_path/'alias';alias.symlink_to(real,target_is_directory=True)
    modules={name:tmp_path/(name+'.mjs') for name in ('cordis','persistence','session')}
    modules['cordis'].write_text('export class Context {root={fiber:{async dispose(){}}};provide(){}}')
    modules['persistence'].write_text('export default class Persistence {async open(id){return {header:{id,cwd:'+json.dumps(str(real))+'},async read(){return {events:[]}},async close(){}}}}')
    modules['session'].write_text('export const foldSurface=()=>({nodes:[],replacements:[]}), foldRequestHeader=()=>({}), deriveEventMessage=()=>null;')
    payload={'modules':{k:str(v) for k,v in modules.items()},'id':'same-session','root':str(tmp_path),'cwd':str(alias)}
    script=Path(adapter.__file__).with_name('dsh_history.mjs')
    result=subprocess.run([node,str(script),json.dumps(payload)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['entries']==[]
    payload['cwd']=str(tmp_path)
    result=subprocess.run([node,str(script),json.dumps(payload)],capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'workspace mismatch' in result.stderr


def test_terminal_retains_profile_and_command_overlays_in_native_order(tmp_path):
    spec, _, _ = context(tmp_path)
    profile=tmp_path/'native/profiles/acp/cordis.patch.yml';profile.parent.mkdir(parents=True)
    profile.write_text('[]');global_patch=tmp_path/'native/cordis.patch.yml';global_patch.write_text('[]')
    spec=spec.model_copy(update={'command':('dsh','--profile','acp','--patch','custom.yml'),'cwd':str(tmp_path)})
    assert adapter.profile_launch_args(spec)==['--profile','acp','--patch',str(tmp_path/'custom.yml')]
    assert profile.read_text()=='[]' and global_patch.read_text()=='[]'
    spec=spec.model_copy(update={'command':('dsh','--profile=custom','--patch=first.yml','--patch','second.yml')})
    assert adapter.profile_launch_args(spec)==['--profile','custom','--patch',str(tmp_path/'first.yml'),'--patch',str(tmp_path/'second.yml')]


@pytest.mark.parametrize('configured_root', ['custom', 'default_expression', 'unsupported_expression'])
def test_history_uses_effective_native_root_without_evaluating_custom_code(tmp_path, configured_root):
    node = shutil.which('node')
    if not node: pytest.skip('Node.js is required for the native decoder contract')
    work = tmp_path/'work'; work.mkdir()
    home = tmp_path/'home'; home.mkdir()
    expected = str(tmp_path/'custom') if configured_root == 'custom' else str(home/'sessions')
    root = expected if configured_root == 'custom' else {'__jsExpr':
        "dshHomePath('sessions')" if configured_root == 'default_expression' else 'process.exit(0)'}
    modules = {name:tmp_path/(name+'.mjs') for name in ('cordis','persistence','session','boot')}
    modules['cordis'].write_text('export class Context {root={fiber:{async dispose(){}}};provide(){}}')
    modules['persistence'].write_text('export default class Persistence {constructor(ctx,config){if(config.root!=='+json.dumps(expected)+')throw Error("wrong root")};async open(id){return {header:{id,cwd:'+json.dumps(str(work))+'},async read(){return {events:[]}},async close(){}}}}')
    modules['session'].write_text('export const foldSurface=()=>({nodes:[],replacements:[]}), foldRequestHeader=()=>({}), deriveEventMessage=()=>null;')
    modules['boot'].write_text('export const loadProfileDirectory=()=>({layers:[],patches:[]}), loadOptionalPatches=()=>[], loadOverlayPatches=()=>[], composeEntries=()=>'+json.dumps([{'id':'session-persistence-jsonl','config':{'root':root}}])+';')
    payload = {'modules':{k:str(v) for k,v in modules.items()},'id':'same-session',
        'root':'must-not-fall-back','cwd':str(work),
        'profile':{'directory':str(home/'profiles/acp'),'anchor':'unused','home':str(home),'patches':[]}}
    result = subprocess.run([node,str(Path(adapter.__file__).with_name('dsh_history.mjs')),json.dumps(payload)],
        capture_output=True,text=True,timeout=10)
    if configured_root == 'unsupported_expression':
        assert result.returncode != 0 and 'storage expression' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['entries'] == []
