"""Shared project ownership helpers for native read-only inventories."""
from pathlib import Path
from drivers.skill_inventory import NativeSkillRoot


def root_project(projects):
    return next((p.id for p in projects if p.slug=='default'),None) or next((p.id for p in projects if not p.parent_project_id),None)


def workspace_roots(projects, bindings, *, agent_key, label, folders, backend_keys=()):
    known={p.id for p in projects}
    work={(p.id,str(p.workspace_root)) for p in projects if p.workspace_root}
    for binding in bindings:
        if binding.project_id in known and binding.backend_key in set(backend_keys or (agent_key,)):
            path=binding.runtime_config.get('workspace_root')
            if isinstance(path,str) and path:work.add((binding.project_id,path))
    return tuple(NativeSkillRoot(Path(path).expanduser()/folder,agent_key,label,pid)
                 for pid,path in sorted(work) for folder in folders)
