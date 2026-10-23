"""Verified native service and command-parser hooks; original identity is retained."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .common import materialize, selection_of, unsupported

LAUNCHER_HASH = '2ccc9a8e167797641448b5e5c936f006ba137a2555f117f38c5eb76a5238a233'

MODULE_HASHES = {
    'index.js': '19af19274b07dcb8ad9bd86915320b7b49a62de763af10c6857ab45257c8a14b',
    '1348.index.js': 'e540ae1241c89c86f303594fa9cd84f21fd10db725e238e53bd5de6e8d0d3ccb',
}


def immutable(path, text):
    if path.exists():
        if path.is_symlink() or path.read_text() != text:
            raise UnsupportedCapabilityError('项目技能执行目录归属不匹配')
    else:
        atomic_write_text(path, text)


def package_path(spec):
    env = spec.build_env()
    executable = shutil.which(spec.command[0], path=env.get('PATH')) if spec.command else None
    if not executable:
        return None
    try:
        current = Path(executable).resolve()
        for _ in range(3):
            if all((current.parent / name).is_file() for name in MODULE_HASHES):
                return current.parent if hashlib.sha256(current.read_bytes()).hexdigest() == LAUNCHER_HASH else None
            if current.stat().st_size > 32 * 1024:
                return None
            # Kaus' explicit executable wrapper. Do not evaluate a shell file.
            for line in current.read_text().splitlines():
                if line.startswith('exec '):
                    parts = shlex.split(line)
                    if len(parts) == 3 and parts[-1] == '$@' and Path(parts[1]).is_absolute() and Path(parts[1]).is_file():
                        current = Path(parts[1]).resolve()
                        break
            else:
                return None
        return None
    except (OSError, UnicodeError, ValueError):
        return None


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if selection_of(effective) is None:
        plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
        plan.spec = spec
        return plan
    package = package_path(spec)
    if os.name != 'posix' or not package or any(hashlib.sha256((package / name).read_bytes()).hexdigest() != digest for name, digest in MODULE_HASHES.items()):
        return unsupported(binding, '当前 Cursor 技能加载版本尚未验证', dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout='roots', dry_run=dry_run)
    plan.spec = spec
    if dry_run:
        return plan
    source = Path(__file__).with_name('cursor_guard.cjs').read_text()
    guard = plan.root / ('cursor-guard-' + hashlib.sha256(source.encode()).hexdigest()[:24] + '.cjs')
    immutable(guard, source)
    data = {'modules': {str(package / name): digest for name, digest in MODULE_HASHES.items()},
            'paths': [str(path) for path in plan.paths]}
    encoded = json.dumps(data, sort_keys=True)
    manifest = plan.root / ('cursor-' + hashlib.sha256(encoded.encode()).hexdigest()[:24] + '.json')
    immutable(manifest, encoded)
    options = spec.build_env().get('NODE_OPTIONS', '')
    # JSON quoting is understood by NODE_OPTIONS and preserves paths with spaces.
    plan.spec = spec.with_env(KAUS_CURSOR_SKILL_SCOPE=str(manifest), NODE_OPTIONS=(options + ' --require=' + json.dumps(str(guard), ensure_ascii=False)).strip())
    return plan


def cli_command(plan, command):
    # No native terminal session mapping is asserted by this adapter.
    raise UnsupportedCapabilityError('当前引擎尚未验证受管终端续接')


async def refresh_inventory(spec):
    from .native_catalog import query, save_paths
    package = package_path(spec)
    if not package or any(hashlib.sha256((package / name).read_bytes()).hexdigest() != digest for name, digest in MODULE_HASHES.items()):
        raise ValueError('当前 Cursor 技能来源版本尚未验证')
    env = spec.build_env()
    explicit = []
    for index, argument in enumerate(spec.command):
        if argument == '--plugin-dir' and index + 1 < len(spec.command):
            explicit.append(spec.command[index + 1])
        elif argument.startswith('--plugin-dir='):
            explicit.append(argument.partition('=')[2])
    payload = {'package': str(package), 'modules': MODULE_HASHES,
               'home': env.get('HOME') or str(Path.home()), 'cwd': spec.cwd,
               'plugins': [str((Path(spec.cwd or Path.cwd()) / value).resolve()) for value in explicit]}
    output = await query(spec, (str(package / 'node'), str(Path(__file__).with_name('cursor_inventory.cjs')), json.dumps(payload)))
    save_paths(spec, 'cursor', json.loads(output.splitlines()[-1])['paths'])


def inventory_roots(spec, projects, bindings):
    from drivers.skill_inventory import NativeSkillRoot
    from .inventory import project_workspaces
    from .native_catalog import cached_roots
    projects = tuple(projects)
    root = next((p.id for p in projects if p.slug == 'default'), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if not root:
        return ()
    home = Path(spec.build_env().get('HOME') or Path.home()).expanduser()
    result = [NativeSkillRoot(home / folder, 'cursor', 'Cursor', root)
              for folder in ('.cursor/skills', '.cursor/skills-cursor', '.agents/skills')]
    for project, workspace in project_workspaces(projects, bindings, 'cursor'):
        result.extend(NativeSkillRoot(workspace / folder, 'cursor', 'Cursor', project)
                      for folder in ('.cursor/skills', '.agents/skills'))
    return (*result, *cached_roots(spec, 'cursor', 'Cursor', projects, bindings))
