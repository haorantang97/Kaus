"""Build and verify the pinned Grok skill runtime before registering it.

Run with PYTHONPATH=kernel python -m drivers.grok.build_runtime --state-root PATH.
Requires Git, the pinned Rust toolchain and protoc; never replaces the user's CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from drivers.acp.client import AcpAgentSpec, AcpConnection
from drivers.skill_execution.managed_builds import register_managed_build
from drivers.skill_execution.grok import skill_list_result


async def verify_scoped_binary(binary):
    with tempfile.TemporaryDirectory(prefix='kaus-grok-build-check-') as directory:
        root = Path(directory)
        home, workspace = root / 'home', root / 'workspace'
        workspace.mkdir()
        selected = root / 'selected/SKILL.md'
        ambient = home / '.grok/skills/ambient/SKILL.md'
        for path, name in ((selected, 'kaus-selected'), (ambient, 'kaus-ambient')):
            path.parent.mkdir(parents=True)
            path.write_text(f'---\nname: {name}\ndescription: synthetic verification\n---\n{name}\n')
        evidence = {}
        for mode, allowed in (('native', None), ('selected', [str(selected)]), ('empty', [])):
            env = {'HOME': str(home), 'GROK_HOME': str(home / '.grok'), 'PATH': os.environ.get('PATH', '')}
            if allowed is not None:
                env['KAUS_GROK_SKILL_ALLOWLIST'] = json.dumps(allowed)
            connection = await AcpConnection.spawn(AcpAgentSpec(command=(str(binary), '--no-auto-update', 'agent', 'stdio'),
                cwd=str(workspace), env=env, inherit_env=False))
            try:
                await connection.call('initialize', {'protocolVersion': 1,
                    'clientInfo': {'name': 'kaus-build-verification', 'version': '1'}, 'clientCapabilities': {}}, timeout=30)
                result = await connection.call('_x.ai/skills/list', {'cwd': str(workspace)}, timeout=30)
                names = sorted(item['name'] for item in skill_list_result(result))
                if mode == 'native':
                    assert 'kaus-ambient' in names, names
                else:
                    assert names == (['kaus-selected'] if mode == 'selected' else []), names
                # Repeated native discovery sees new files; scoped discovery stays frozen.
                dynamic = home / '.grok/skills/dynamic/SKILL.md'
                dynamic.parent.mkdir(parents=True, exist_ok=True)
                dynamic.write_text('---\nname: kaus-dynamic\ndescription: fixture\n---\ndynamic\n')
                later = await connection.call('_x.ai/skills/list', {'cwd': str(workspace)}, timeout=30)
                if mode != 'native':
                    assert sorted(item['name'] for item in skill_list_result(later)) == names
                evidence[mode] = {'skills': names, 'refreshVerified': True}
            finally:
                await connection.aclose()
        return evidence


def build(state_root, source=None, *, profile='release'):
    metadata_file = Path(__file__).with_name('build.json')
    data = json.loads(metadata_file.read_text())
    patch = Path(__file__).with_name('project-skills.patch')
    if hashlib.sha256(patch.read_bytes()).hexdigest() != data['patchHash']:
        raise ValueError('Build patch checksum mismatch')
    state_root = Path(state_root).expanduser().resolve()
    work = state_root / 'build-work/grok'
    work.mkdir(parents=True, exist_ok=True)
    source = Path(source).resolve() if source else work / 'source'
    if not source.exists():
        subprocess.run(['git', 'clone', '--no-checkout', data['sourceUrl'], str(source)], check=True)
        subprocess.run(['git', 'checkout', '--detach', data['sourceCommit']], cwd=source, check=True)
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() != data['sourceCommit']:
        raise ValueError('Source commit mismatch')
    tree = subprocess.check_output(['git', 'ls-tree', '-r', 'HEAD'], cwd=source)
    if hashlib.sha256(tree).hexdigest() != data['sourceHash']:
        raise ValueError('Source tree checksum mismatch')
    if subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=source).strip():
        raise ValueError('Source contains untracked build inputs')
    current = subprocess.check_output(['git', 'diff', 'HEAD', '--'], cwd=source)
    if not current:
        subprocess.run(['git', 'apply', str(patch.resolve())], cwd=source, check=True)
    elif current != patch.read_bytes():
        raise ValueError('Source has changes outside the pinned patch')
    target = work / 'target'
    env = {**os.environ, 'CARGO_BUILD_JOBS': '2', 'CARGO_INCREMENTAL': '0',
           'CARGO_PROFILE_DEV_DEBUG': '0', 'CARGO_PROFILE_RELEASE_DEBUG': '0', 'CARGO_TARGET_DIR': str(target)}
    if shutil.which('protoc'):
        env['PROTOC'] = shutil.which('protoc')
    for package, name in (('xai-grok-agent', 'kaus_selection_covers_all_sources_and_empty'),
                           ('xai-grok-tools', 'kaus_runtime_dynamic_scope')):
        subprocess.run(['cargo', 'test', '-p', package, '--lib', name], cwd=source, env=env, check=True)
    subprocess.run(['cargo', 'build', '-p', 'xai-grok-pager-bin', '--profile', profile], cwd=source, env=env, check=True)
    binary = target / ('debug' if profile == 'dev' else profile) / 'xai-grok-pager'
    evidence = asyncio.run(verify_scoped_binary(binary))
    evidence_file = work / 'verification.json'
    evidence_file.write_text(json.dumps(evidence, indent=2) + '\n')
    spec = AcpAgentSpec(command=(str(binary),), env={'KAUS_SKILL_STATE_ROOT': str(state_root)}, inherit_env=False)
    return register_managed_build(spec, engine_key='grok', binary_path=binary,
        source_version=data['sourceVersion'], source_commit=data['sourceCommit'], source_hash=data['sourceHash'],
        patch_version=data['patchVersion'], patch_hash=data['patchHash'], license_id=data['license'], source_url=data['sourceUrl'],
        notices=(source / 'LICENSE', patch, metadata_file, evidence_file), build_profile=profile)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--source')
    args = parser.parse_args()
    print(build(args.state_root, args.source))
