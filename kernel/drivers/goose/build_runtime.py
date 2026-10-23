"""Build/register the exact upstream Goose CLI with Kaus's skill scope patch.

Usage: python -m drivers.goose.build_runtime --source /private/goose-checkout
       --state-root /private/kaus-skill-state [--register-only]
No system installation or user configuration is modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from drivers.acp.client import AcpAgentSpec
from drivers.skill_execution.managed_builds import register_managed_build


def build(source, state_root, *, register_only=False):
    package=Path(__file__).parent
    info=json.loads((package/'skill-scope-source.json').read_text())
    source=Path(source).resolve()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    if commit!=info['commit']:raise ValueError('Goose source commit does not match the reviewed patch')
    original=subprocess.check_output(['git','show','HEAD:crates/goose/src/skills/mod.rs'],cwd=source)
    if hashlib.sha256(original).hexdigest()!=info['sourceModuleSha256']:raise ValueError('Goose source module changed')
    patch=package/'skill-scope-1.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest()!=info['patchSha256']:raise ValueError('Goose patch changed')
    existing=subprocess.check_output(['git','diff','HEAD','--binary','--'],cwd=source)
    if subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=source):raise ValueError('Goose source has untracked files')
    if not existing:
        subprocess.run(['git','apply',str(patch.resolve())],cwd=source,check=True)
    elif existing!=patch.read_bytes():raise ValueError('Goose source has unrelated modifications')
    if not register_only:
        env={**os.environ,'CARGO_BUILD_JOBS':'2','CARGO_INCREMENTAL':'0','CARGO_PROFILE_RELEASE_DEBUG':'0'}
        subprocess.run(['cargo','+stable','build','--release','--locked','-p','goose-cli','--bin','goose'],cwd=source,env=env,check=True)
    binary=source/'target/release/goose'
    protocol=json.loads(subprocess.check_output([str(binary),'--kaus-skill-scope-version']))
    if protocol!={'schema':1,'upstreamVersion':'1.52.0','patchVersion':'kaus-skills-1'}:raise ValueError('Goose scope protocol mismatch')
    with tempfile.TemporaryDirectory(prefix='goose-scope-build-check-') as folder:
        root=Path(folder);manifest=root/'scope.json';manifest.write_text('{"schema":1,"paths":[]}')
        env={**os.environ,'KAUS_GOOSE_SKILL_MANIFEST':str(manifest)}
        if json.loads(subprocess.check_output([str(binary),'--kaus-skill-scope-list'],env=env,cwd=root))!=[]:
            raise ValueError('Goose managed-empty check failed')
    spec=AcpAgentSpec(command=(str(binary),),inherit_env=False,env={'KAUS_SKILL_STATE_ROOT':str(Path(state_root).resolve())})
    registered=register_managed_build(spec,engine_key='goose',binary_path=binary,source_version='1.52.0',
        source_commit=info['commit'],source_hash=info['sourceModuleSha256'],patch_version=info['patchVersion'],
        patch_hash=info['patchSha256'],license_id=info['license'],source_url=info['upstream'],
        notices=(package/'LICENSE.upstream',patch,package/'skill-scope-source.json'),build_profile='release')
    for builtin in (source/'crates/goose/src/skills/builtins').glob('*.md'):
        target=registered.parent/'builtin-skills'/builtin.stem/'SKILL.md'
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if target.exists() and target.read_bytes()!=builtin.read_bytes():raise ValueError('Built-in source differs from reviewed build')
        target.write_bytes(builtin.read_bytes())
    return registered


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',required=True);parser.add_argument('--state-root',required=True);parser.add_argument('--register-only',action='store_true')
    args=parser.parse_args();print(build(args.source,args.state_root,register_only=args.register_only))


if __name__=='__main__':main()
