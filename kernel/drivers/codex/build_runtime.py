"""Build and verify the pinned project-skill app-server; keep the original TUI.

PYTHONPATH=kernel python -m drivers.codex.build_runtime --state-root PATH
Uses a private build directory and never installs or replaces the user's Codex.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time

from drivers.acp.client import AcpAgentSpec, AcpConnection
from drivers.skill_execution.managed_builds import file_hash, register_managed_build
from drivers.codex.companions import install as install_companions


async def verify_scoped_binary(binary):
    """Use the native loader, same live process and public mutation APIs."""
    evidence = {}
    for mode in ('native', 'selected', 'empty'):
        with tempfile.TemporaryDirectory(prefix='kaus-codex-build-check-') as directory:
            root = Path(directory).resolve()
            home, config, workspace = (root / name for name in ('home', 'config', 'workspace'))
            for path in (home, config, workspace):
                path.mkdir()
            def skill(path, name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'---\nname: {name}\ndescription: synthetic verification\n---\n{name}\n')
                return path
            selected = skill(root / 'scope/.agents/skills/selected/SKILL.md', 'kaus-selected')
            ambient = skill(home / '.agents/skills/ambient/SKILL.md', 'kaus-ambient')
            skill(workspace / '.agents/skills/workspace/SKILL.md', 'kaus-workspace')
            plugin = config / 'plugins/cache/kaus-fixture/sample/local'
            (plugin / '.codex-plugin').mkdir(parents=True)
            (plugin / '.codex-plugin/plugin.json').write_text('{"name":"sample"}')
            skill(plugin / 'skills/plugin/SKILL.md', 'plugin')
            config.joinpath('config.toml').write_text('[features]\nplugins=true\n'
                '[plugins."sample@kaus-fixture"]\nenabled=true\n'
                '[projects.' + json.dumps(str(workspace)) + ']\ntrust_level="trusted"\n')
            env = {'HOME': str(home), 'CODEX_HOME': str(config), 'PATH': os.environ.get('PATH', '')}
            if mode != 'native':
                env['KAUS_CODEX_SKILL_ALLOWLIST'] = json.dumps([str(selected)] if mode == 'selected' else [])
            connection = await AcpConnection.spawn(AcpAgentSpec(command=(str(binary),),
                cwd=str(workspace), env=env, inherit_env=False))
            try:
                await connection.call('initialize', {'clientInfo': {'name': 'kaus-build-verification', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}, timeout=30)
                connection.notify('initialized', {})
                await connection.call('skills/extraRoots/set', {'extraRoots': [str(selected.parent.parent)]})
                async def listed():
                    result = await connection.call('skills/list', {'cwds': [str(workspace)], 'forceReload': True})
                    rows = result['data']
                    assert all(not row.get('errors') for row in rows), rows
                    return sorted(skill['name'] for row in rows for skill in row['skills'] if skill['enabled'])
                initial = await listed()
                expected = ['kaus-selected'] if mode == 'selected' else []
                if mode == 'native':
                    assert {'kaus-selected', 'kaus-ambient', 'kaus-workspace', 'sample:plugin'} <= set(initial), initial
                else:
                    assert initial == expected, initial
                # No watcher sleep: new root and new files must be excluded on
                # the very first native discovery after creation.
                dynamic = skill(root / 'new-root/dynamic/SKILL.md', 'kaus-dynamic')
                skill(selected.parent / 'nested/SKILL.md', 'kaus-nested')
                skill(plugin / 'skills/dynamic/SKILL.md', 'dynamic')
                await connection.call('skills/extraRoots/set', {'extraRoots': [str(selected.parent.parent), str(dynamic.parent.parent)]})
                await connection.call('skills/config/write', {'path': str(ambient), 'enabled': True})
                later = await listed()
                if mode == 'native':
                    assert {'kaus-dynamic', 'sample:dynamic'} <= set(later), later
                else:
                    assert later == expected, later
                evidence[mode] = {'initial': initial, 'afterDynamicRootAndEnable': later,
                                  'sameProcess': True, 'modelCalls': 0}
            finally:
                await connection.aclose()
    return evidence


async def verify_precreated_binary(binary):
    """Prove an empty exact ID is durable across a native process restart."""
    evidence = {}
    for mode in ('native', 'selected', 'empty'):
        with tempfile.TemporaryDirectory(prefix='kaus-codex-empty-check-') as directory:
            root = Path(directory).resolve()
            home, config, workspace = (root / name for name in ('home', 'config', 'workspace'))
            for path in (home, config, workspace):
                path.mkdir()
            selected = root / 'scope/SKILL.md'
            selected.parent.mkdir()
            selected.write_text('---\nname: selected\ndescription: synthetic skill\n---\nfixture\n')
            env = {'HOME': str(home), 'CODEX_HOME': str(config), 'PATH': os.environ.get('PATH', ''),
                   'KAUS_CODEX_PERSIST_THREAD_START': '1'}
            if mode != 'native':
                env['KAUS_CODEX_SKILL_ALLOWLIST'] = json.dumps([str(selected)] if mode == 'selected' else [])
            spec = AcpAgentSpec(command=(str(binary),), cwd=str(workspace), env=env, inherit_env=False)
            settings = {'model': 'fixture', 'model_provider': 'fixture', 'model_providers': {'fixture': {
                'name': 'fixture', 'base_url': 'http://127.0.0.1:9/v1', 'wire_api': 'responses',
                'requires_openai_auth': False}}}
            async def connect():
                connection = await AcpConnection.spawn(spec)
                await connection.call('initialize', {'clientInfo': {'name': 'kaus-empty-verification', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}, timeout=30)
                connection.notify('initialized', {})
                return connection
            connection = await connect()
            try:
                created = await connection.call('thread/start', {'cwd': str(workspace), 'config': settings}, timeout=30)
                identity = created['thread']['id']
            finally:
                await connection.aclose()
            from drivers.codex.native_history import load_native_history
            history = await load_native_history(spec, None, identity)
            assert history.complete and not history.entries
            connection = await connect()
            try:
                resumed = await connection.call('thread/resume', {'threadId': identity, 'excludeTurns': True,
                    'config': settings, 'cwd': str(workspace)}, timeout=30)
                assert resumed['thread']['id'] == identity
            finally:
                await connection.aclose()
            evidence[mode] = {'emptyEntries': 0, 'exactIdColdResume': True, 'modelCalls': 0}
    return evidence


def _run_bounded(command, source, env):
    process = subprocess.Popen(command, cwd=source, env=env, start_new_session=True)
    try:
        while process.poll() is None:
            if shutil.disk_usage(env['CARGO_TARGET_DIR']).free < 25 * 1024 ** 3:
                raise RuntimeError('构建已停止：可用磁盘低于 25 GiB')
            time.sleep(2)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def prepare_source(work, data, patch):
    archive = work / 'source.tar.gz'
    if not archive.exists():
        url = f"https://codeload.github.com/openai/codex/tar.gz/{data['sourceCommit']}"
        download = archive.with_suffix('.download')
        try:
            subprocess.run(['curl', '--fail', '--location', '--max-time', '180',
                            '--proto', '=https', url, '--output', str(download)], check=True)
            if file_hash(download) != data['sourceHash']:
                raise ValueError('Source archive checksum mismatch')
            download.replace(archive)
        finally:
            download.unlink(missing_ok=True)
    if file_hash(archive) != data['sourceHash']:
        raise ValueError('Source archive checksum mismatch')
    source = work / ('codex-' + data['sourceCommit'])
    if not source.exists():
        with tarfile.open(archive) as archive_file:
            archive_file.extractall(work, filter='data')
    if not (source / '.git').exists():
        subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
        subprocess.run(['git', 'add', '.'], cwd=source, check=True)
        tree = subprocess.check_output(['git', 'write-tree'], cwd=source, text=True).strip()
        if tree != data['sourceTree']:
            raise ValueError('Source tree checksum mismatch')
        subprocess.run(['git', '-c', 'user.name=Kaus build', '-c', 'user.email=build@localhost',
                        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Pinned upstream source'], cwd=source, check=True)
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source, text=True).strip()
    if tree != data['sourceTree']:
        raise ValueError('Source tree checksum mismatch')
    if subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=source).strip():
        raise ValueError('Unexpected source inputs')
    current = subprocess.check_output(['git', 'diff', '--binary', 'HEAD', '--'], cwd=source)
    if not current:
        subprocess.run(['git', 'apply', str(patch.resolve())], cwd=source, check=True)
        subprocess.run(['git', 'add', '-N', 'codex-rs/ext/skills/src/managed_scope.rs'], cwd=source, check=True)
        current = subprocess.check_output(['git', 'diff', '--binary', 'HEAD', '--'], cwd=source)
    if current != patch.read_bytes():
        raise ValueError('Source changes differ from the pinned patch')
    return source


def build(state_root, work=None):
    metadata = Path(__file__).with_name('build.json')
    data = json.loads(metadata.read_text())
    patch = Path(__file__).with_name('project-skills.patch')
    if file_hash(patch) != data['patchHash']:
        raise ValueError('Build patch checksum mismatch')
    state_root = Path(state_root).expanduser().resolve()
    work = Path(work).resolve() if work else state_root / 'build-work/codex'
    work.mkdir(parents=True, exist_ok=True)
    source = prepare_source(work, data, patch)
    target = work / 'target'
    target.mkdir(exist_ok=True)
    # The caller can install the required toolchain under work/rustup. No default
    # toolchain, global CLI, credentials or native configuration is changed here.
    env = {**os.environ, 'CARGO_TARGET_DIR': str(target), 'CARGO_BUILD_JOBS': '2',
           'CARGO_INCREMENTAL': '0', 'CARGO_PROFILE_DEV_DEBUG': '0'}
    if (work / 'rustup').exists():
        env['RUSTUP_HOME'] = str(work / 'rustup')
    _run_bounded(['cargo', 'build', '--locked', '-p', 'codex-app-server', '--bin', 'codex-app-server'],
                 source / 'codex-rs', env)
    if json.loads(metadata.read_text()) != data or file_hash(patch) != data['patchHash']:
        raise ValueError('Build inputs changed during compilation; rebuild before registration')
    prepare_source(work, data, patch)
    binary = target / 'debug/codex-app-server'
    evidence = asyncio.run(verify_scoped_binary(binary))
    evidence['emptySessionPersistence'] = asyncio.run(verify_precreated_binary(binary))
    evidence_file = work / 'verification.json'
    evidence_file.write_text(json.dumps(evidence, indent=2) + '\n')
    # Complete setup in the build workspace before selecting a new runtime.
    # A failed download or protocol check must leave the current build usable.
    install_companions(binary, data['sourceVersion'])
    from drivers.codex.companions import HOST, TUI, RECEIPT
    spec = AcpAgentSpec(command=(str(binary),), env={'KAUS_SKILL_STATE_ROOT': str(state_root)}, inherit_env=False)
    managed = register_managed_build(spec, engine_key='codex', binary_path=binary,
        source_version=data['sourceVersion'], source_commit=data['sourceCommit'], source_hash=data['sourceHash'],
        patch_version=data['patchVersion'], patch_hash=data['patchHash'], license_id=data['license'], source_url=data['sourceUrl'],
        notices=(source / 'LICENSE', source / 'NOTICE', patch, metadata, evidence_file), build_profile='dev',
        runtime_files=tuple(binary.with_name(name) for name in (HOST, TUI, RECEIPT)))
    return managed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--work')
    args = parser.parse_args()
    print(build(args.state_root, args.work))
