import json
from pathlib import Path
from types import SimpleNamespace

from drivers.acp.client import AcpAgentSpec
from drivers.skill_execution import commandcode, gemini, pi
from drivers.skill_execution.native_catalog import cache_path, cached_roots, save_paths


def skill(path):
    path.mkdir(parents=True)
    result = path / 'SKILL.md'
    result.write_text('---\nname: fixture\ndescription: synthetic\n---\nfixture')
    return result


def test_catalogue_assigns_registered_workspace_not_global_parent(tmp_path):
    spec = AcpAgentSpec(command=('fixture',), env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'state')})
    local = skill(tmp_path / 'project/child/.gemini/skills/one')
    shared = skill(tmp_path / 'home/.gemini/skills/two')
    save_paths(spec, 'gemini', [str(local), str(shared), str(shared), str(tmp_path / 'absent')])
    projects = [SimpleNamespace(id='root', slug='default', parent_project_id=None, workspace_root=None),
        SimpleNamespace(id='parent', slug='parent', parent_project_id='root', workspace_root=str(tmp_path / 'project')),
        SimpleNamespace(id='child', slug='child', parent_project_id='parent', workspace_root=str(tmp_path / 'project/child'))]
    roots = cached_roots(spec, 'gemini', 'Gemini', projects, ())
    assert {(r.path, r.project_id) for r in roots} == {(local.parent, 'child'), (shared.parent, 'root')}
    assert 'env' not in json.loads(cache_path(spec, 'gemini').read_text())


async def test_gemini_inventory_excludes_disabled_and_preserves_native_sources(tmp_path, monkeypatch):
    from drivers.skill_execution import native_catalog
    enabled = skill(tmp_path / 'enabled')
    disabled = skill(tmp_path / 'disabled')
    spec = AcpAgentSpec(command=('gemini',), env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'state')})
    monkeypatch.setattr(gemini, 'locate', lambda *_: tmp_path / 'package')
    monkeypatch.setattr(gemini.shutil, 'which', lambda *_args, **_kwargs: '/node')
    async def query(*_args, **_kwargs):
        return f'Discovered Agent Skills:\n\nfirst [Enabled] [Built-in]\n  Location:    {enabled}\n\nsecond [Disabled]\n  Location:    {disabled}\n'
    monkeypatch.setattr(native_catalog, 'query', query)
    await gemini.refresh_inventory(spec)
    assert json.loads(cache_path(spec, 'gemini').read_text())['paths'] == [str(enabled)]
    assert enabled.is_file() and disabled.is_file()


async def test_pi_installed_catalogue_reads_native_packages_without_installing(tmp_path):
    import os
    import shutil
    import pytest
    from drivers.skill_execution.node_package import locate
    executable = shutil.which('pi')
    if not executable:
        pytest.skip('Pi CLI is not installed')
    spec = AcpAgentSpec(command=(executable,), cwd=str(tmp_path), env={
        'HOME': str(tmp_path / 'home'), 'PATH': os.environ.get('PATH', ''),
        'PI_CODING_AGENT_DIR': str(tmp_path / 'home/.pi/agent'),
        'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'state'),
        'npm_config_registry': 'http://127.0.0.1:9'}, inherit_env=False)
    if not locate(spec, '@mariozechner/pi-coding-agent'):
        pytest.skip('Pi package is not available')
    base = skill(tmp_path / 'home/.pi/agent/skills/base')
    plugin = skill(tmp_path / 'plugin/skills/extension')
    plugin.write_text('---\nname: extension\ndescription: synthetic package\n---\nfixture')
    (tmp_path / 'plugin/package.json').write_text(json.dumps({'name': 'fixture', 'pi': {'skills': ['./skills']}}))
    settings = tmp_path / 'home/.pi/agent/settings.json'
    settings.write_text(json.dumps({'packages': [str(tmp_path / 'plugin'), 'npm:@kaus-never-install/inventory-fixture']}))
    before = settings.read_bytes()
    await pi.refresh_inventory(spec)
    assert set(json.loads(cache_path(spec, 'pi').read_text())['paths']) == {str(base), str(plugin)}
    assert settings.read_bytes() == before
    assert not (tmp_path / 'home/.pi/agent/npm/node_modules/@kaus-never-install').exists()


def test_commandcode_bundled_and_project_skills_keep_disabled_scope(tmp_path, monkeypatch):
    from drivers.skill_execution import node_package
    home, package, workspace = tmp_path / 'home', tmp_path / 'package', tmp_path / 'project'
    bundled = skill(package / 'dist/bundled/builtin')
    local = skill(workspace / '.commandcode/skills/local')
    local.write_text('---\nname: local\ndescription: synthetic\n---\nfixture')
    (home / '.commandcode').mkdir(parents=True)
    (home / '.commandcode/settings.json').write_text(json.dumps({'disabledSkills': ['fixture']}))
    (workspace / '.commandcode/settings.local.json').write_text(json.dumps({'disabledSkills': ['local']}))
    spec = AcpAgentSpec(command=('bridge', '--command', '/native/command-code'),
        env={'HOME': str(home)}, inherit_env=False)
    monkeypatch.setattr(node_package, 'locate', lambda *_: package)
    projects = [SimpleNamespace(id='root', slug='default', parent_project_id=None, workspace_root=None),
        SimpleNamespace(id='child', slug='child', parent_project_id='root', workspace_root=str(workspace))]
    roots = commandcode.inventory_roots(spec, projects, ())
    actual = {r.path: r for r in roots}
    assert actual[bundled.parent].project_id == 'root'
    assert set(actual[bundled.parent].blocked_project_ids) == {'root', 'child'}
    assert actual[local.parent].project_id == 'child'
    assert actual[local.parent].blocked_project_ids == ('child',)
