import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from drivers.skill_inventory import discover_skills
from drivers.skill_execution import crush_inventory, zcode_inventory, zcode
from test_selection import setup


def projects(root):
    return [SimpleNamespace(id='project:root', slug='default', parent_project_id=None, workspace_root=None),
            SimpleNamespace(id='project:one', slug='one', parent_project_id='project:root', workspace_root=str(root / 'work'))]


def skill(path, name):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: fixture\n---\n{name}')
    return path


async def test_crush_inventory_singleflight_retry_and_local_builtin_copy(tmp_path, monkeypatch):
    spec, _ = setup(tmp_path, 'crush')
    binary = tmp_path / 'crush'
    binary.write_text('native v1')
    monkeypatch.setattr(crush_inventory, 'executable', lambda spec: binary)
    calls = []
    async def inspect(command, cwd, env, paths, **kwargs):
        calls.append(env)
        assert paths == [] and kwargs['include_builtin_content']
        assert env['CRUSH_GLOBAL_CONFIG'] != str(tmp_path / 'user')
        await asyncio.sleep(.01)
        return [{'id': 'crush://skills/native/SKILL.md', 'name': 'native',
                 'content': base64.b64encode(b'---\nname: native\n---\nfixture').decode()}]
    monkeypatch.setattr(crush_inventory, 'inspect', inspect)
    await asyncio.gather(*(crush_inventory.refresh(spec) for _ in range(3)))
    await crush_inventory.refresh(spec)
    assert len(calls) == 1
    cache = crush_inventory.cache_root(spec)
    assert json.loads((cache / 'source.json').read_text())['sources'][0]['uri'].startswith('crush://')
    source = next(cache.glob('*/SKILL.md'))
    assert source.read_text().endswith('fixture')
    binary.write_text('native v2')
    monkeypatch.setattr(crush_inventory, 'inspect', AsyncMock(side_effect=ValueError('not ready')))
    try:
        await crush_inventory.refresh(spec)
    except ValueError:
        pass
    assert not (crush_inventory.cache_root(spec) / 'source.json').exists()
    monkeypatch.setattr(crush_inventory, 'inspect', inspect)
    await crush_inventory.refresh(spec)
    assert len(calls) == 2


def test_crush_inventory_native_roots_are_agent_and_project_owned(tmp_path, monkeypatch):
    home, work = tmp_path / 'home', tmp_path / 'work'
    skill(home / '.config/crush/skills/global', 'global')
    skill(work / '.crush/skills/work', 'workspace')
    extra = skill(work / 'custom/extra', 'extra')
    (work / 'crush.json').write_text(json.dumps({'options': {'skills_paths': ['./custom']}}))
    spec, _ = setup(tmp_path, 'crush')
    monkeypatch.setattr(crush_inventory, 'cache_root', lambda spec: None)
    rows = discover_skills(crush_inventory.roots(spec.with_env(HOME=str(home)), projects(tmp_path), []))
    assert {r['name'] for r in rows} == {'global', 'workspace', 'extra'}
    assert all(r['agentKey'] == 'crush' for r in rows)
    assert next(r for r in rows if r['name'] == 'global')['defaultProjectIds'] == ['project:root']
    assert next(r for r in rows if r['name'] == 'extra')['defaultProjectIds'] == ['project:one']


async def test_zcode_builtin_cache_uses_native_cli_and_appserver_once(tmp_path, monkeypatch):
    spec, _ = setup(tmp_path, 'zcode')
    target = tmp_path / 'state/catalog.json'
    builtin = skill(tmp_path / 'native/builtin', 'builtin') / 'SKILL.md'
    plugin = skill(tmp_path / 'native/plugin', 'plugin') / 'SKILL.md'
    monkeypatch.setattr(zcode_inventory, 'cache_path', lambda spec: target)
    monkeypatch.setattr(zcode, 'native_command', lambda spec: 'native-fixture')
    cli, app = AsyncMock(return_value=[{'path': str(builtin)}]), AsyncMock(return_value=[{'path': str(plugin), 'pluginName': 'native-plugin'}])
    monkeypatch.setattr(zcode, 'native_catalog', cli)
    monkeypatch.setattr(zcode_inventory, 'plugin_catalog', app)
    await asyncio.gather(zcode_inventory.refresh(spec), zcode_inventory.refresh(spec))
    assert cli.await_count == app.await_count == 1
    assert {r['path'] for r in json.loads(target.read_text())['skills']} == {str(builtin), str(plugin)}
    assert cli.call_args.args[1].env['HOME'] != str(Path.home())


def test_zcode_inventory_respects_plugin_enablement_and_workspace_scope(tmp_path, monkeypatch):
    spec, _ = setup(tmp_path, 'zcode')
    home, work = tmp_path / 'home', tmp_path / 'work'
    skill(home / '.zcode/skills/global', 'global')
    skill(work / '.agents/skills/local', 'workspace')
    package = tmp_path / 'plugin'
    skill(package / 'skills/special', 'special')
    (package / '.zcode-plugin').mkdir()
    (package / '.zcode-plugin/plugin.json').write_text(json.dumps({'name': 'fixture'}))
    user = home / '.zcode/cli/config.json'; user.parent.mkdir(parents=True)
    user.write_text(json.dumps({'plugins': {'enabledPlugins': {'fixture@market': True}}}))
    installed = home / '.zcode/cli/plugins/installed_plugins.json'; installed.parent.mkdir()
    installed.write_text(json.dumps({'plugins': [{'id': 'fixture@market', 'installPath': str(package), 'scope': 'user'}]}))
    config = work / '.zcode/config.json'; config.parent.mkdir()
    config.write_text(json.dumps({'plugins': {'enabledPlugins': {'fixture@market': False}}}))
    monkeypatch.setattr(zcode_inventory, 'cache_path', lambda spec: tmp_path / 'absent')
    rows = discover_skills(zcode_inventory.roots(spec.with_env(HOME=str(home)), projects(tmp_path), []))
    names = {r['name']: r for r in rows}
    assert set(names) == {'global', 'workspace', 'fixture:special'}
    assert names['fixture:special']['defaultProjectIds'] == ['project:root']
    assert names['fixture:special']['defaultBlockedProjectIds'] == ['project:one']
    assert names['workspace']['defaultProjectIds'] == ['project:one']
    user.write_text(json.dumps({'plugins': {'enabledPlugins': {'fixture@market': False}}}))
    assert 'fixture:special' not in {r['name'] for r in discover_skills(zcode_inventory.roots(spec.with_env(HOME=str(home)), projects(tmp_path), []))}


def test_zcode_malformed_optional_config_does_not_break_project_tree(tmp_path, monkeypatch):
    spec, _ = setup(tmp_path, 'zcode')
    home = tmp_path / 'home'
    config = home / '.zcode/cli/config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'plugins': {'enabledPlugins': False, 'suppressedBuiltins': None}, 'storage': {'dir': False}}))
    work = tmp_path / 'work/.zcode/config.json'
    work.parent.mkdir(parents=True)
    work.write_text(json.dumps({'plugins': {'enabledPlugins': False}}))
    cache = tmp_path / 'catalog.json'
    source = skill(tmp_path / 'builtin', 'builtin') / 'SKILL.md'
    cache.write_text(json.dumps({'skills': [{'path': str(source), 'pluginName': 'builtin'}]}))
    monkeypatch.setattr(zcode_inventory, 'cache_path', lambda spec: cache)
    result = discover_skills(zcode_inventory.roots(spec.with_env(HOME=str(home)), projects(tmp_path), []))
    assert [r['name'] for r in result] == ['builtin']
