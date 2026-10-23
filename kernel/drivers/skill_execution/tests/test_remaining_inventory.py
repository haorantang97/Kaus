import json
from pathlib import Path
from types import SimpleNamespace as S
from drivers.skill_inventory import discover_skills
from drivers.skill_execution import copilot,goose,deepseek
from drivers.openclaw import skill_inventory as openclaw
from drivers.acp.client import AcpAgentSpec


def skill(path,name):
    path.mkdir(parents=True,exist_ok=True);(path/'SKILL.md').write_text(f'---\nname: {name}\ndescription: fixture\n---\nSYNTHETIC\n')


def fixture(tmp_path,env=None):
    home=tmp_path/'home';home.mkdir()
    spec=AcpAgentSpec(command=('unused',),inherit_env=False,env={'HOME':str(home),'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state'),**(env or {})})
    work=tmp_path/'work';work.mkdir()
    projects=[S(id='root',slug='default',parent_project_id=None,workspace_root=None),S(id='child',slug='child',parent_project_id='root',workspace_root=str(work))]
    return spec,home,work,projects


def test_goose_config_override_builtins_and_plugin_local_disable(tmp_path,monkeypatch):
    override=tmp_path/'native';spec,home,work,projects=fixture(tmp_path,{'GOOSE_PATH_ROOT':str(override)})
    skill(override/'config/skills/native','native')
    skill(override/'.agents/plugins/plugin/custom/plugin-skill','plugin-skill')
    manifest=override/'.agents/plugins/plugin/.goose-plugin/plugin.json';manifest.parent.mkdir()
    manifest.write_text(json.dumps({'name':'plugin','skills':{'paths':['custom'],'exclusive':True}}))
    settings=work/'.config/goose/settings.json';settings.parent.mkdir(parents=True);settings.write_text('{"disabledPlugins":["plugin"]}')
    binary=tmp_path/'build/goose';binary.parent.mkdir();binary.write_text('fixture');skill(binary.parent/'builtin-skills/builtin','builtin')
    monkeypatch.setattr(goose,'resolve_managed_binary',lambda *a,**k:binary)
    rows={r['name']:r for r in discover_skills(goose.inventory_roots(spec,projects,[]))}
    assert set(rows)=={'native','plugin:plugin-skill','builtin'}
    assert rows['plugin:plugin-skill']['defaultProjectIds']==['root']
    assert rows['plugin:plugin-skill']['defaultBlockedProjectIds']==['child']


def test_copilot_cached_builtins_disabled_rows_and_workspace_not_global(tmp_path):
    spec,home,work,projects=fixture(tmp_path)
    skill(tmp_path/'builtin','builtin');skill(work/'.github/skills/local','local')
    cache=copilot._inventory_cache(spec);cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([{'path':str(tmp_path/'builtin'),'enabled':False}]))
    rows={r['name']:r for r in discover_skills(copilot.inventory_roots(spec,projects,[]))}
    assert rows['builtin']['defaultBlockedProjectIds']==['root']
    assert rows['local']['defaultProjectIds']==['child']


def test_openclaw_native_plugin_sources_and_workspace_ownership(tmp_path,monkeypatch):
    spec,home,work,projects=fixture(tmp_path);package=tmp_path/'package'
    skill(package/'skills/builtin','builtin');skill(package/'dist/extensions/tool/skills/plugin','plugin')
    manifest=package/'dist/extensions/tool/openclaw.plugin.json';manifest.write_text('{"id":"tool","enabledByDefault":true,"skills":["skills"]}')
    skill(work/'skills/local','local');skill(home/'.openclaw/agents/custom/workshop-skills/owned','owned')
    config=home/'.openclaw/openclaw.json';config.write_text(json.dumps({'plugins':{'entries':{'tool':{'enabled':False}}},'skills':{'allowBundled':[]},'agents':{'entries':{'custom':{'workspace':str(work),'agentDir':str(home/'.openclaw/agents/custom')}}}}))
    monkeypatch.setattr(openclaw,'locate',lambda *a:package)
    rows={r['name']:r for r in discover_skills(openclaw.inventory_roots(spec,projects,[]))}
    assert rows['builtin']['defaultBlockedProjectIds']==['root']
    assert rows['plugin']['defaultBlockedProjectIds']==['root']
    assert rows['local']['defaultProjectIds']==rows['owned']['defaultProjectIds']==['child']


def test_deepseek_keeps_native_engine_scope(tmp_path,monkeypatch):
    spec,_,_,projects=fixture(tmp_path);calls=[]
    monkeypatch.setattr('drivers.skill_execution.dsh.inventory_roots_for_package',lambda *a,**k:calls.append((a,k)) or ())
    assert deepseek.inventory_roots(spec,projects,[])==()
    assert calls[0][0][-1]=='deepseek-acp' and calls[0][1]['agent_key']=='deepseek-acp'
