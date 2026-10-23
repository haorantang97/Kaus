"""Read-only plugin skill metadata, matching Goose's native source scopes."""
import json
from pathlib import Path
import yaml
from drivers.skill_inventory import NativeSkillRoot
from drivers.skill_execution.source_roots import root_project, workspace_roots


def document(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:return {}
        value=yaml.safe_load(path.read_text()) if path.suffix=='.yaml' else json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,yaml.YAMLError):return {}


def _enabled(name, path, native, settings):
    entry=native.get(str(path),{})
    if isinstance(entry,dict) and entry.get('enabled') is False:return False
    for setting in settings:
        if name in setting.get('disabledPlugins',[]):return False
        if name in setting.get('enabledPlugins',[]):return True
    return True


def _paths(plugin):
    manifest=next((document(plugin/name) for name in ('.goose-plugin/plugin.json','.plugin/plugin.json','plugin.json') if (plugin/name).is_file()),{})
    value=manifest.get('skills')
    paths=[]
    if not isinstance(value,dict) or not value.get('exclusive'):paths.append(plugin/'skills')
    custom=value.get('paths',[]) if isinstance(value,dict) else value or []
    if isinstance(custom,str):custom=[custom]
    for item in custom if isinstance(custom,list) else []:
        if not isinstance(item,str) or Path(item).is_absolute() or '..' in Path(item).parts:continue
        paths.append(plugin/item)
    if (plugin/'SKILL.md').is_file() and value is None:paths.append(plugin)
    return paths


def plugin_roots(spec, projects, bindings, config, base):
    pid=root_project(projects)
    user=document(base/'.config/goose/settings.json')
    native=document(config/'config.yaml').get('plugins',{})
    if not isinstance(native,dict):native={}
    work=workspace_roots(projects,bindings,agent_key='goose',label='Goose',folders=('',))
    sources=[(pid,base/'.agents/plugins',[user])]
    sources.extend((w.project_id,w.path/'.agents/plugins',[
        document(w.path/'.config/goose/settings.local.json'),document(w.path/'.config/goose/settings.json'),user]) for w in work)
    result=[]
    for owner,directory,settings in sources:
        if not directory.is_dir():continue
        for plugin in sorted(directory.iterdir()):
            if not plugin.is_dir() or plugin.name.startswith('.'):continue
            blocked=[] if _enabled(plugin.name,plugin,native,settings) else [owner]
            if owner==pid and directory==base/'.agents/plugins':
                for w in work:
                    local=[document(w.path/'.config/goose/settings.local.json'),document(w.path/'.config/goose/settings.json'),user]
                    if not _enabled(plugin.name,plugin,native,local) and w.project_id not in blocked:blocked.append(w.project_id)
            for path in _paths(plugin):
                # Namespace follows the native plugin identifier, not guessed
                # compatibility with another engine.
                result.append(NativeSkillRoot(path,'goose','Goose',owner,
                    name_prefix=plugin.name+':',blocked_project_ids=tuple(blocked)))
    return tuple(result)
