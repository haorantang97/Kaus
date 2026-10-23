import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.crush_bridge import skills as crush_loader
from drivers.crush_bridge.protocol import BridgeError
from drivers.skill_execution import crush, zcode
from drivers.skill_execution.common import materialize
from test_selection import setup, effective


@pytest.mark.parametrize('adapter', [crush, zcode])
async def test_native_mode_does_not_probe_or_project(adapter, tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'fixture')
    value = effective(tmp_path, binding)
    value.entries[0].config['mode'] = 'native'
    monkeypatch.setattr(adapter.shutil, 'which', lambda *a, **k: (_ for _ in ()).throw(AssertionError('probe')))
    plan = await adapter.project(spec, binding, value, tmp_path)
    assert plan.root is None and not plan.report.unsupported


async def test_crush_scope_is_private_restored_and_exact(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'crush')
    executable = tmp_path / 'crush'
    executable.write_text('synthetic executable')
    monkeypatch.setattr(crush.shutil, 'which', lambda *a, **k: str(executable))
    value = effective(tmp_path, binding)
    copied = materialize(spec, binding, value, layout='roots', dry_run=False)
    rows = [{'id': str(copied.paths[0]), 'name': copied.names[0]},
            {'id': '/native/workspace/SKILL.md', 'name': 'blocked'},
            {'id': 'crush://skills/builtin/SKILL.md', 'name': 'builtin'}]
    monkeypatch.setattr(crush, 'inspect', AsyncMock(return_value=rows))
    plan = await crush.project(spec, binding, value, tmp_path)
    monkeypatch.setenv('KAUS_CRUSH_SKILL_SCOPE', plan.spec.env['KAUS_CRUSH_SKILL_SCOPE'])
    directory = tmp_path / 'owned-session'
    (directory / 'data').mkdir(parents=True)
    config = directory / 'data/crush.json'
    config.write_text(json.dumps({'models': {'large': 'unchanged'}, 'options': {'other': True}}))
    probe = AsyncMock(return_value=rows)
    monkeypatch.setattr(crush_loader, 'inspect', probe)
    scope = await crush_loader.prepare(str(executable), str(tmp_path), directory)
    written = json.loads(config.read_text())
    assert written['models'] == {'large': 'unchanged'}
    assert written['options']['other'] is True
    assert written['options']['disabled_skills'] == ['blocked', 'builtin']
    assert Path(value.entries[0].config['assets'][0]['sourcePath']).read_text().endswith('BODY_common\n')
    rows.append({'id': '/new-plugin/SKILL.md', 'name': 'late'})
    await crush_loader.prepare(str(executable), str(tmp_path), directory)
    assert 'late' in json.loads(config.read_text())['options']['disabled_skills']
    session = SimpleNamespace(skill_scope=scope, base='/workspaces/fixture', request=AsyncMock(return_value=rows))
    with pytest.raises(BridgeError, match='技能集合已变化'):
        await crush_loader.validate(session)
    session.request.return_value = [rows[0]]
    await crush_loader.validate(session)
    executable.write_text('updated executable')
    with pytest.raises(BridgeError, match='版本已变化'):
        crush_loader.read_scope(str(executable))


async def test_crush_missing_selected_or_unavailable_http_is_rejected(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'crush')
    binary = tmp_path / 'crush'
    binary.write_text('fixture')
    monkeypatch.setattr(crush.shutil, 'which', lambda *a, **k: str(binary))
    monkeypatch.setattr(crush, 'inspect', AsyncMock(return_value=[]))
    with pytest.raises(UnsupportedCapabilityError, match='未加载完整'):
        await crush.project(spec, binding, effective(tmp_path, binding), tmp_path)
    monkeypatch.setattr(crush, 'inspect', AsyncMock(side_effect=BridgeError('unsupported version')))
    with pytest.raises(UnsupportedCapabilityError, match='目录验证失败'):
        await crush.project(spec, binding, effective(tmp_path, binding, ()), tmp_path)


def fake_bundle(path):
    # Synthetic native class, intentionally matching only the public loader shape.
    path.write_text('''#!/usr/bin/env node
const fs = require('node:fs');
async function resolveDefaultSkillRoots() { return [{path: process.cwd()}]; }
class Adapter {
  async skillFilesUnderRoot(root) { return fs.readdirSync(root.path).filter(x => x.endsWith('.md')).map(x => root.path + '/' + x); }
  async discoverSkills(request) {
    const workingDirectory = request.workingDirectory;
    const roots = request.roots ?? await resolveDefaultSkillRoots(workingDirectory, this.options);
    const diagnostics = [];
    const results = [];
    for (const root3 of roots) {
      const skillPaths = await this.skillFilesUnderRoot(root3, diagnostics);
      for (const path6 of skillPaths) {
        const parsed = await this.parseSkill(path6, root3, diagnostics);
        results.push(path6);
      }
    }
    return results;
  }
  async parseSkill(path) { return {name:path}; }
  async load(name, request) {
    const paths = await this.discoverSkills(request);
    if (!paths.includes(name)) throw Error('not selected');
    return fs.readFileSync(name,'utf8');
  }
}
module.exports = Adapter;
''')


def test_zcode_guard_blocks_default_explicit_and_dynamic_sources(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for loader contract')
    bundle = tmp_path / 'zcode.cjs'
    fake_bundle(bundle)
    allowed, blocked = tmp_path / 'selected.md', tmp_path / 'blocked.md'
    allowed.write_text('SELECTED')
    blocked.write_text('BLOCKED')
    manifest = tmp_path / 'scope.json'
    data = {'module': str(bundle), 'moduleHash': hashlib.sha256(bundle.read_bytes()).hexdigest(),
            'paths': [str(allowed)], 'roots': [{'path': str(tmp_path)}]}
    manifest.write_text(json.dumps(data))
    guard = Path(zcode.__file__).with_name('zcode_guard.cjs')
    script = "const A=require(" + json.dumps(str(bundle)) + ");(async()=>{let a=new A();let r={workingDirectory:process.cwd(),roots:[{path:process.cwd()}]};console.log(JSON.stringify(await a.discoverSkills(r)));console.log(await a.load(" + json.dumps(str(allowed)) + ",r));try{await a.load(" + json.dumps(str(blocked)) + ",r);process.exit(2)}catch{}})()"
    env = {'KAUS_ZCODE_SKILL_SCOPE': str(manifest)}
    def run(): return subprocess.run([node, '--require', str(guard), '-e', script], env=env, cwd=tmp_path, text=True, capture_output=True)
    result = run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[0]) == [str(allowed)]
    (tmp_path / 'new-plugin.md').write_text('LATE')
    assert run().stdout == result.stdout
    data['paths'], data['roots'] = [], []
    manifest.write_text(json.dumps(data))
    empty = run()
    assert json.loads(empty.stdout.splitlines()[0]) == []
    bundle.write_text(bundle.read_text() + '\n// changed native version')
    changed = run()
    assert changed.returncode != 0 and '重新验证' in changed.stderr


async def test_zcode_project_checks_actual_paths_and_custom_launcher(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'zcode')
    native = tmp_path / 'installation/bin/zcode'
    native.parent.mkdir(parents=True)
    native.write_text('#!/bin/sh\nexec node "$0/../lib/zcode.cjs" "$@"\n')
    module = native.parent.parent / 'lib/zcode.cjs'
    module.parent.mkdir()
    fake_bundle(module)
    monkeypatch.setattr(zcode.shutil, 'which', lambda name, **k: str(native) if name == str(native) else None)
    spec = spec.model_copy(update={'command': ('python', 'bridge.py', '--command', str(native))})
    async def catalog(executable, scoped, cwd):
        data = json.loads(Path(scoped.env['KAUS_ZCODE_SKILL_SCOPE']).read_text())
        return [{'path': path} for path in data['paths']]
    monkeypatch.setattr(zcode, 'native_catalog', catalog)
    plan = await zcode.project(spec, binding, effective(tmp_path, binding), tmp_path)
    assert plan.spec.command == spec.command
    assert '--require=' in plan.spec.env['NODE_OPTIONS']
    monkeypatch.setattr(zcode, 'native_catalog', AsyncMock(return_value=[{'path': '/wrong/source'}]))
    with pytest.raises(UnsupportedCapabilityError, match='不一致'):
        await zcode.project(spec, binding, effective(tmp_path, binding, ()), tmp_path)
    module.write_text('unknown native format')
    preview = await zcode.project(spec, binding, effective(tmp_path, binding), tmp_path, dry_run=True)
    assert preview.report.unsupported
    with pytest.raises(UnsupportedCapabilityError, match='需要适配'):
        await zcode.project(spec, binding, effective(tmp_path, binding), tmp_path)


async def test_zcode_original_unique_names_survive_but_collision_is_disambiguated(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'zcode')
    native = tmp_path / 'native/zcode.cjs'
    native.parent.mkdir()
    fake_bundle(native)
    monkeypatch.setattr(zcode.shutil, 'which', lambda *a, **k: str(native))
    async def catalog(executable, scoped, cwd):
        data = json.loads(Path(scoped.env['KAUS_ZCODE_SKILL_SCOPE']).read_text())
        return [{'path': path} for path in data['paths']]
    monkeypatch.setattr(zcode, 'native_catalog', catalog)
    value = effective(tmp_path, binding, ('dynamic-workflows',))
    plan = await zcode.project(spec, binding, value, tmp_path)
    assert plan.names == ('dynamic-workflows',)
    scope = json.loads(Path(plan.spec.env['KAUS_ZCODE_SKILL_SCOPE']).read_text())
    assert scope['names'][str(plan.paths[0])] == 'dynamic-workflows'
    item = value.entries[0].config['assets'][0]
    value.entries[0].config['assets'].append({**item, 'capabilityType': 'zcode:skills', 'capabilityId': 'other'})
    duplicate = await zcode.project(spec, binding, value, tmp_path)
    assert len(set(duplicate.names)) == 2
    assert duplicate.names[0] != 'dynamic-workflows' and duplicate.names[1] == 'dynamic-workflows'


async def _wait_native_pid(task, pid_file):
    import asyncio
    try:
        async with asyncio.timeout(10):
            while True:
                if task.done():
                    await task  # Surface startup errors before diagnosing readiness.
                    pytest.fail('native catalogue exited before the cancellation handshake')
                try:
                    ready = pid_file.read_text().strip()
                except FileNotFoundError:
                    ready = ''
                if ready:
                    pid = int(ready)
                    assert pid > 0, f'invalid native PID: {ready!r}'
                    return pid
                await asyncio.sleep(.01)
    except TimeoutError:
        if task.done():
            await task
        pytest.fail('native process did not publish a PID within 10 seconds')


@pytest.mark.parametrize('adapter', ['crush', 'zcode'])
async def test_cancel_native_catalog_reaps_temporary_process(tmp_path, adapter):
    import asyncio
    import os
    import shlex
    import sys
    spec, _ = setup(tmp_path)
    pid_file = tmp_path / 'child.pid'
    native = tmp_path / 'native-fixture'
    code = (f'import os,time\nwith open({str(pid_file)!r},"w") as ready:\n'
            ' time.sleep(.05)\n ready.write(str(os.getpid()))\n ready.flush()\ntime.sleep(60)')
    native.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + '\n')
    native.chmod(0o700)
    if adapter == 'zcode':
        coroutine = zcode.native_catalog(str(native), spec, tmp_path)
    else:
        coroutine = crush_loader.inspect(str(native), tmp_path, spec.build_env(), [])
    task = asyncio.create_task(coroutine)
    try:
        pid = await _wait_native_pid(task, pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('adapter', ['crush', 'zcode'])
async def test_native_catalog_readiness_surfaces_startup_error(tmp_path, adapter):
    import asyncio
    spec, _ = setup(tmp_path)
    native = tmp_path / 'broken-native'
    native.write_text('#!/bin/sh\nexit 7\n')
    native.chmod(0o700)
    if adapter == 'zcode':
        coroutine = zcode.native_catalog(str(native), spec, tmp_path)
        error = UnsupportedCapabilityError
    else:
        coroutine = crush_loader.inspect(str(native), tmp_path, spec.build_env(), [])
        error = BridgeError
    task = asyncio.create_task(coroutine)
    try:
        with pytest.raises(error, match='失败'):
            await _wait_native_pid(task, tmp_path / 'missing.pid')
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
