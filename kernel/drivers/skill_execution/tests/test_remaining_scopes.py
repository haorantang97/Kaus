from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import hashlib
from unittest.mock import AsyncMock
import pytest
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import copilot, deepseek
from test_selection import setup, effective


def test_jsonc_preserves_urls_and_strings(tmp_path):
    path=tmp_path/'settings.json';path.write_text('//comment\n{"url":"https://example.test/x//y","literal":"/*yes*/",/*block*/"values":[1,],}')
    assert copilot.read_jsonc(path)=={'url':'https://example.test/x//y','literal':'/*yes*/','values':[1]}


FIXTURE_APP = 'const Jre=1; export function create(){return {clientKind:"acp",integrationId:Jre};} export function resume(){return {clientKind:"acp",integrationId:Jre};}\n'
FIXTURE_ENTRY = 'console.log(JSON.stringify(await import("./app.js").then(m=>[m.create(),m.resume()])));\n'


@pytest.fixture(autouse=True)
def verified_fixture_package(monkeypatch):
    monkeypatch.setattr(copilot,'VERIFIED_ACP_HASHES',{hashlib.sha256(FIXTURE_APP.encode()).hexdigest()})
    monkeypatch.setattr(copilot,'VERIFIED_ENTRY_HASHES',{hashlib.sha256(FIXTURE_ENTRY.encode()).hexdigest()})


async def native_fixture(spec,cwd):
    config=copilot.read_jsonc(Path(spec.env['COPILOT_HOME'])/'settings.json')
    rows=[]
    for root in config.get('skillDirectories',[]):
        for skill in Path(root).glob('*/SKILL.md'):
            rows.append({'name':skill.parent.name,'path':str(skill.parent),'enabled':True,'source':'custom'})
    package=Path(spec.env.get('HOME',cwd))/'fixture-package';package.mkdir(parents=True,exist_ok=True)
    (package/'app.js').write_text(FIXTURE_APP);(package/'index.js').write_text(FIXTURE_ENTRY);(package/'package.json').write_text('{"type":"module"}')
    rows.append({'name':'builtin','path':str(package/'builtin-skills/builtin'),'enabled':'builtin' not in config.get('disabledSkills',[]),'source':'builtin'})
    return rows


async def test_private_config_preserves_plugins_without_history_or_source_writes(tmp_path,monkeypatch):
    spec,binding=setup(tmp_path,'copilot');source=tmp_path/'native';source.mkdir()
    (source/'settings.json').write_text('{"model":"fixture","allowedUrls":["https://example.test"]}')
    (source/'session-state').mkdir();(source/'session-state/private.txt').write_text('do-not-copy')
    plugin=source/'installed-plugins/p';plugin.mkdir(parents=True);(plugin/'plugin.json').write_text('{"mcpServers":{"fixture":{"command":"synthetic"}}}')
    before={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    spec=spec.with_env(HOME=str(tmp_path/'home'),COPILOT_HOME=str(source))
    monkeypatch.setattr(copilot,'inventory',native_fixture)
    plan=await copilot.project_session(spec,binding,effective(tmp_path,binding),tmp_path,conversation=SimpleNamespace(id='a'))
    private=Path(plan.spec.env['COPILOT_HOME']);config=copilot.read_jsonc(private/'settings.json')
    assert config['model']=='fixture' and config['allowedUrls']==['https://example.test']
    assert (private/'installed-plugins/p/plugin.json').read_text()==(plugin/'plugin.json').read_text()
    assert not (private/'session-state/private.txt').exists()
    assert before=={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert config['disabledSkills']==['builtin']
    assert private.stat().st_mode & 0o077 == 0
    second=await copilot.project_session(spec,binding,effective(tmp_path,binding,()),tmp_path,conversation=SimpleNamespace(id='b'))
    assert second.paths==() and second.spec.env['COPILOT_HOME']!=plan.spec.env['COPILOT_HOME']
    assert copilot.read_jsonc(private/'settings.json')==config
    await copilot.validate_turn(plan,str(tmp_path))


async def test_changed_policy_and_unexpected_native_source_fail_closed(tmp_path,monkeypatch):
    spec,binding=setup(tmp_path,'copilot');spec=spec.with_env(HOME=str(tmp_path/'home'))
    monkeypatch.setattr(copilot,'inventory',native_fixture)
    plan=await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    monkeypatch.setattr(copilot,'inventory',AsyncMock(return_value=[{'name':'unexpected','path':'/outside','enabled':True}]))
    with pytest.raises(UnsupportedCapabilityError,match='加载范围'):await copilot.validate_turn(plan,str(tmp_path))
    path=Path(plan.spec.env['COPILOT_HOME'])/'settings.json';path.write_text('{}')
    with pytest.raises(UnsupportedCapabilityError,match='配置已变化'):copilot.cli_command(plan,())


async def test_auth_secrets_not_duplicated_and_native_mode_untouched(tmp_path,monkeypatch):
    spec,binding=setup(tmp_path,'copilot');source=tmp_path/'native';source.mkdir();(source/'config.json').write_text('{"oauth_token":"synthetic-secret"}')
    spec=spec.with_env(COPILOT_HOME=str(source));monkeypatch.setattr(copilot,'inventory',native_fixture)
    with pytest.raises(UnsupportedCapabilityError,match='系统凭据'):await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    assert not list((tmp_path/'state').rglob('config.json'))
    value=effective(tmp_path,binding);value.entries[0].config['mode']='native'
    plan=await copilot.project(spec,binding,value,tmp_path)
    assert plan.spec==spec and plan.root is None


async def test_terminal_uses_same_scope_and_original_argument_boundaries(tmp_path,monkeypatch):
    spec,binding=setup(tmp_path,'copilot');spec=spec.with_env(HOME=str(tmp_path/'home'))
    monkeypatch.setattr(copilot,'inventory',native_fixture)
    plan=await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    native=tmp_path/'native cli';native.write_text('#!/bin/sh\nprintf "%s\\n" "$COPILOT_HOME" "$@"\n');native.chmod(0o700)
    command=copilot.cli_command(plan,(str(native),'--resume','a b'))
    result=subprocess.run(command,check=True,capture_output=True,text=True)
    assert result.stdout.splitlines()==[plan.spec.env['COPILOT_HOME'],'--resume','a b']


async def test_deepseek_uses_shared_verified_registry_guard(tmp_path,monkeypatch):
    spec,binding=setup(tmp_path,'deepseek-acp');project=AsyncMock(return_value='plan')
    monkeypatch.setattr(deepseek,'project_for_package',project)
    assert await deepseek.project(spec,binding,effective(tmp_path,binding),str(tmp_path))=='plan'
    assert project.await_args.args[-1]=='deepseek-acp'


async def test_goose_verified_build_matches_source_version_and_native_scope(tmp_path,monkeypatch):
    from drivers.skill_execution import goose
    spec,binding=setup(tmp_path,'goose');spec=spec.model_copy(update={'command':('/native/goose','acp')})
    binary=tmp_path/'managed-goose';binary.write_text('synthetic');binary.chmod(0o700)
    resolved=[]
    monkeypatch.setattr(goose,'resolve_managed_binary',lambda *a,**kw:resolved.append(kw) or binary)
    async def query(command,spec,cwd):
        if '--version' in command:return 'goose 1.52.0'
        if '--kaus-skill-scope-version' in command:return json.dumps({'schema':1,'upstreamVersion':'1.52.0','patchVersion':'kaus-skills-1'})
        data=json.loads(Path(spec.env['KAUS_GOOSE_SKILL_MANIFEST']).read_text())
        return json.dumps([{'name':Path(path).parent.name,'path':str(Path(path).parent)} for path in data['paths']])
    monkeypatch.setattr(goose,'query',query)
    first=await goose.project(spec,binding,effective(tmp_path,binding),tmp_path)
    second=await goose.project(spec,binding,effective(tmp_path,binding,()),tmp_path)
    assert all(call['expected_source_commit']==goose.BUILD_SOURCE['commit'] and call['expected_patch_hash']==goose.BUILD_SOURCE['patchSha256'] for call in resolved)
    assert first.spec.command==(str(binary),'acp') and first.names
    assert second.names==() and first.spec.env['KAUS_GOOSE_SKILL_MANIFEST']!=second.spec.env['KAUS_GOOSE_SKILL_MANIFEST']
    await goose.validate_turn(first,str(tmp_path))
    Path(first.spec.env['KAUS_GOOSE_SKILL_MANIFEST']).write_text('{"schema":1,"paths":[]}')
    with pytest.raises(UnsupportedCapabilityError,match='清单'):await goose.validate_turn(first,str(tmp_path))


async def test_goose_missing_or_wrong_build_fails_closed_only_in_managed_mode(tmp_path,monkeypatch):
    from drivers.skill_execution import goose
    spec,binding=setup(tmp_path,'goose');value=effective(tmp_path,binding)
    preview=await goose.project(spec,binding,value,tmp_path,dry_run=True)
    assert preview.report.unsupported
    with pytest.raises(UnsupportedCapabilityError):await goose.project(spec,binding,value,tmp_path)
    value.entries[0].config['mode']='native'
    assert (await goose.project(spec,binding,value,tmp_path)).spec==spec
