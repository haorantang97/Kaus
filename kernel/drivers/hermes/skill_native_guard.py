"""Native skill discovery for a Project home, with Project-owned skill writes.

No selection filtering is applied here. The connected engine's native sources
remain visible while old managed selections never become ambient native skills.
"""
from pathlib import Path
import shutil
import sys


def install(home, source, output):
    home, source, output = Path(home).resolve(), Path(source).resolve(), Path(output)
    if output.is_symlink() or not output.resolve().is_relative_to(home / '.kaus-native-skill-outputs'):
        raise RuntimeError('Native skill output must remain in its Project home')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    from agent import skill_utils
    from tools import skills_tool, skill_manager_tool
    if not all(callable(getattr(skill_utils, key, None)) for key in ('get_all_skills_dirs', 'get_skill_create_dir', 'get_external_skills_dirs')):
        raise RuntimeError('Installed Hermes cannot select native Project skill sources')
    original_all = skill_utils.get_all_skills_dirs
    original_create = skill_utils.get_skill_create_dir
    def native_dirs():
        return list(dict.fromkeys([output, source / 'skills', *skill_utils.get_external_skills_dirs()]))
    for name, original, replacement in (('get_all_skills_dirs', original_all, native_dirs),
                                        ('get_skill_create_dir', original_create, lambda: output)):
        for module_name, module in tuple(sys.modules.items()):
            if module and module_name.startswith(('agent.', 'tools.', 'hermes_cli.')) and getattr(module, name, None) is original:
                setattr(module, name, replacement)
    skills_tool.SKILLS_DIR = source / 'skills'
    skills_tool._skills_dir = lambda: source / 'skills'
    skill_manager_tool.SKILLS_DIR = output
    skill_manager_tool._skills_dir = lambda: output
    find = skill_manager_tool._find_skill
    def writable(name):
        found = find(name)
        if not found:
            return found
        folder = Path(found['path']).resolve()
        if folder.is_relative_to(output):
            return found
        target = output / folder.name
        if target.exists():
            raise RuntimeError('Native skill output name conflicts with another source')
        try:
            from .skill_snapshots import digest
        except ImportError:
            from skill_snapshots import digest
        digest(folder)
        shutil.copytree(folder, target, symlinks=False)
        return {**found, 'path': target}
    skill_manager_tool._find_skill = writable
