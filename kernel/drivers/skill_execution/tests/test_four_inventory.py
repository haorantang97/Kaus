"""Native source declarations, enabled registries, and installed bundled assets."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivers.skill_inventory import discover_skills
from drivers.skill_execution import dsh, qwen, kilo, omp, opencode
from test_four_native_adapters import native_spec
from test_selection import setup


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def skill(path, name):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: synthetic\n---\nBODY_{name}\n')


def projects(tmp_path):
    workspace = tmp_path / 'project'; workspace.mkdir(exist_ok=True)
    return (SimpleNamespace(id='project:default', slug='default', parent_project_id=None, workspace_root=None),
            SimpleNamespace(id='project:child', slug='child', parent_project_id='project:default', workspace_root=str(workspace)))


def test_qwen_uses_installed_enablement_and_workspace_skill_overrides(tmp_path):
    spec, _ = setup(tmp_path, 'qwen'); spec = spec.with_env(HOME=str(tmp_path))
    ps = projects(tmp_path); base = tmp_path / '.qwen'
    for name in ['active', 'disabled', 'cached']:
        folder = base / 'extensions' / name
        write(folder / 'qwen-extension.json', {'name': name, 'version': '1.0'})
        skill(folder / 'skills/one', 'one')
        skill(folder / 'skills/two', 'two')
    write(base / 'extension-store/state.json', {'version': 2, 'extensions': {
        'a': {'name': 'active', 'artifactDirectory': 'active', 'defaultActivation': 'enabled', 'skillWorkspaceOverrides': {ps[1].workspace_root: {'one': False}}},
        'b': {'name': 'disabled', 'artifactDirectory': 'disabled', 'defaultActivation': 'disabled'}}})
    rows = discover_skills(qwen.inventory_roots(spec, ps, ()))
    assert {r['name'] for r in rows} == {'active:one', 'active:two'}
    assert next(r for r in rows if r['name'] == 'active:one')['defaultBlockedProjectIds'] == ['project:child']
    assert next(r for r in rows if r['name'] == 'active:two')['defaultBlockedProjectIds'] == []


def test_dsh_reads_declarative_patch_without_evaluating_js(tmp_path):
    spec, _ = setup(tmp_path, 'dsh'); spec = spec.with_env(HOME=str(tmp_path), DSH_HOME=str(tmp_path / '.dsh'))
    ps = projects(tmp_path); skill(tmp_path / '.dsh/skills/native', 'native')
    skill(tmp_path / 'custom/custom', 'custom')
    patch = tmp_path / '.dsh/profiles/acp/cordis.patch.yml';patch.parent.mkdir(parents=True)
    patch.write_text('- id: unrelated\n  config: !!js process.exit(1)\n- id: skill-filesystem\n  config:\n    includeDefaultRoots: false\n    customSkillDirs: ["' + str(tmp_path / 'custom') + '"]\n')
    rows = discover_skills(dsh.inventory_roots(spec, ps, ()))
    assert {r['name'] for r in rows} == {'custom'}


def test_omp_uses_registered_enabled_plugins_not_cache(tmp_path):
    spec, _ = setup(tmp_path, 'omp'); spec = spec.with_env(HOME=str(tmp_path))
    ps = projects(tmp_path); base = tmp_path / '.omp/plugins'
    write(base / 'package.json', {'dependencies': {'active': '1', 'disabled': '1'}})
    write(base / 'omp-plugins.lock.json', {'plugins': {'active': {'enabled': True}, 'disabled': {'enabled': False}, 'stale': {'enabled': True}}})
    for name in ['active', 'disabled', 'stale', 'cached']:
        write(base / 'node_modules' / name / 'package.json', {'name': name, 'omp': {}})
        skill(base / 'node_modules' / name / 'skills/example', name)
    write(Path(ps[1].workspace_root) / '.omp/plugin-overrides.json', {'disabled': ['active']})
    rows = discover_skills(omp.inventory_roots(spec, ps, ()))
    assert {r['name'] for r in rows} == {'active'}
    assert rows[0]['defaultBlockedProjectIds'] == ['project:child']


@pytest.mark.parametrize('adapter,backend,prefix', [(kilo,'kilo','KILO'), (opencode,'opencode','OPENCODE')])
def test_native_family_config_paths_keep_ownership(tmp_path, adapter, backend, prefix):
    spec, _ = setup(tmp_path, backend); spec = spec.with_env(HOME=str(tmp_path))
    ps = projects(tmp_path); skill(tmp_path / 'shared/one', 'global')
    skill(Path(ps[1].workspace_root) / 'local/two', 'project')
    write(tmp_path / '.config' / backend / (backend + '.json'), {'skills': {'paths': ['~/shared']}})
    write(Path(ps[1].workspace_root) / (backend + '.json'), {'skills': {'paths': ['local']}})
    rows = discover_skills(adapter.inventory_roots(spec, ps, ()))
    assert {r['name']: r['defaultProjectId'] for r in rows} == {'global':'project:default','project':'project:child'}


async def test_installed_dsh_badge_preserves_body_and_png_without_deepseek_leak(tmp_path):
    spec, _ = native_spec(tmp_path, 'dsh'); ps = projects(tmp_path)
    await dsh.refresh_inventory(spec)
    rows = discover_skills(dsh.inventory_roots(spec, ps, ()))
    badge = next(r for r in rows if r['name'] == 'dsh-badge')
    assert (Path(badge['sourcePath']).parent / 'dsh-badge.png').is_file()
    # The bridge shares the native dependency but does not enable this provider.
    from drivers.skill_execution.dsh_inventory import roots
    assert not any(r.path.name == 'dsh-badge' for r in roots(spec, ps, (), 'deepseek-acp', 'deepseek-acp', 'DeepSeek'))


async def test_installed_qwen_bundled_skills_are_discoverable(tmp_path):
    spec, _ = native_spec(tmp_path, 'qwen'); ps = projects(tmp_path)
    rows = discover_skills(qwen.inventory_roots(spec, ps, ()))
    assert len(rows) >= 18
    assert all(r['agentKey'] == 'qwen' for r in rows)


async def test_installed_kilo_builtins_read_through_native_cli(tmp_path):
    spec, _ = native_spec(tmp_path, 'kilo'); ps = projects(tmp_path)
    await kilo.refresh_inventory(spec)
    rows = discover_skills(kilo.inventory_roots(spec, ps, ()))
    assert rows and all(r['agentKey'] == 'kilo' for r in rows)
    assert all(Path(r['sourcePath']).is_relative_to(tmp_path / 'state') for r in rows)


def test_omp_pinned_store_and_marketplace_follow_enabled_records(tmp_path):
    spec, _ = setup(tmp_path, 'omp'); spec = spec.with_env(HOME=str(tmp_path))
    ps = projects(tmp_path); agent = tmp_path / '.omp/agent'
    good = tmp_path / '.omp/skillshare/@fixture/good/1.0.0'
    bad = tmp_path / '.omp/skillshare/@fixture/bad/1.0.0'
    for path, name in [(good, 'pinned'), (bad, 'incomplete')]:
        skill(path, name)
        (path / '.skillshare-integrity').write_text('sha512-complete' if path == good else 'wrong')
    write(agent / 'skills.lock.json', {'version': 1, 'skills': {
        '@fixture/good': {'version':'1.0.0', 'integrity':'sha512-complete'},
        '@fixture/bad': {'version':'1.0.0', 'integrity':'sha512-complete'}}})
    installed = tmp_path / 'installed'; disabled = tmp_path / 'disabled'
    skill(installed / 'skills/example', 'marketplace');skill(disabled / 'skills/example', 'disabled')
    write(tmp_path / '.omp/plugins/installed_plugins.json', {'version': 2, 'plugins': {
        'active@fixture':[{'installPath':str(installed),'scope':'user'}],
        'inactive@fixture':[{'installPath':str(disabled),'scope':'user','enabled':False}]}})
    rows = discover_skills(omp.inventory_roots(spec, ps, ()))
    assert {r['name'] for r in rows} == {'pinned','marketplace'}
    # Broken native metadata must not crash the common Skill pool.
    write(tmp_path / '.omp/plugins/installed_plugins.json', [])
    assert {r['name'] for r in discover_skills(omp.inventory_roots(spec, ps, ()))} == {'pinned'}
