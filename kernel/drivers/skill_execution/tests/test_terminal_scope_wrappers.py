"""Managed terminal launch retains native IDs and process-local skill fences."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.acp.client import AcpAgentSpec
from drivers.skill_execution import copilot, qwen, omp, goose, grok
from test_selection import setup, effective
from test_remaining_scopes import native_fixture, FIXTURE_APP, FIXTURE_ENTRY


@pytest.mark.parametrize('adapter', [copilot, qwen, omp, goose, grok])
def test_native_mode_keeps_command_and_arguments(adapter):
    command=('/native cli', '--resume', 'id with spaces')
    assert adapter.cli_command(SimpleNamespace(root=None), command)==command


@pytest.mark.parametrize('adapter,env', [
    (qwen, {'KAUS_QWEN_SKILL_SCOPE':'/scope with spaces.json', 'NODE_OPTIONS':'--import=file:///guard.mjs'}),
    (omp, {'PI_CONFIG_FILES':'/original:/overlay with spaces.json'}),
])
def test_terminal_exports_exact_policy_and_preserves_id(adapter,env,tmp_path):
    native=tmp_path/'native cli'
    native.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nenv\n');native.chmod(0o700)
    plan=SimpleNamespace(root=tmp_path, spec=AcpAgentSpec(command=(str(native),), env=env))
    command=adapter.cli_command(plan,(str(native),'--resume','id with spaces'))
    result=subprocess.run(command,capture_output=True,text=True,check=True)
    assert result.stdout.splitlines()[:2]==['--resume','id with spaces']
    for key,value in env.items():assert key+'='+value in result.stdout.splitlines()
    launcher=Path(command[0]);assert launcher.stat().st_mode & 0o077==0
    launcher.write_text('replaced')
    with pytest.raises(UnsupportedCapabilityError):adapter.cli_command(plan,(str(native),'--resume','id with spaces'))


@pytest.mark.parametrize('adapter',[qwen,omp])
def test_missing_managed_launch_policy_does_not_fall_back(adapter,tmp_path):
    plan=SimpleNamespace(root=tmp_path,spec=AcpAgentSpec(command=('native',)))
    with pytest.raises(UnsupportedCapabilityError):adapter.cli_command(plan,('native','--resume','id'))


@pytest.fixture
def copilot_fixture(monkeypatch):
    monkeypatch.setattr(copilot,'inventory',native_fixture)
    monkeypatch.setattr(copilot,'VERIFIED_ACP_HASHES',{hashlib.sha256(FIXTURE_APP.encode()).hexdigest()})
    monkeypatch.setattr(copilot,'VERIFIED_ENTRY_HASHES',{hashlib.sha256(FIXTURE_ENTRY.encode()).hexdigest()})


@pytest.mark.parametrize('selected',[True,False])
async def test_copilot_acp_new_and_resume_receive_the_exact_native_policy(tmp_path,copilot_fixture,selected):
    node=shutil.which('node')
    if not node:pytest.skip('Node is not installed')
    spec,binding=setup(tmp_path,'copilot');spec=spec.with_env(HOME=str(tmp_path/'home'))
    value=effective(tmp_path,binding) if selected else effective(tmp_path,binding,())
    plan=await copilot.project(spec,binding,value,tmp_path)
    bootstrap=Path(plan.spec.env['COPILOT_CLI_DIST_DIR'])/'index.js'
    result=subprocess.run([node,str(bootstrap)],env=plan.spec.build_env(),capture_output=True,text=True,check=True)
    rows=json.loads(result.stdout)
    assert len(rows)==2
    for row in rows:
        assert row['clientKind']=='acp' and row['integrationId']==1
        for key,value in plan.skill_policy.items():assert row[key]==value
        assert row['disabledSkills']==['builtin']
    private=Path(plan.spec.env['COPILOT_HOME'])
    assert json.loads((private/'mcp-config.json').read_text())=={'mcpServers':{}}
    # Native migration removes old settings fields from config.json.
    (private/'config.json').write_text('{"firstLaunchAt":"synthetic"}')
    copilot._settings_matches(plan)
    (private/'settings.json').write_text('{}')
    with pytest.raises(UnsupportedCapabilityError,match='配置已变化'):copilot._settings_matches(plan)


async def test_copilot_unknown_bundle_or_changed_bootstrap_fails_closed(tmp_path,copilot_fixture,monkeypatch):
    spec,binding=setup(tmp_path,'copilot');spec=spec.with_env(HOME=str(tmp_path/'home'))
    monkeypatch.setattr(copilot,'VERIFIED_ACP_HASHES',set())
    with pytest.raises(UnsupportedCapabilityError,match='版本尚未验证'):
        await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    monkeypatch.setattr(copilot,'VERIFIED_ACP_HASHES',{hashlib.sha256(FIXTURE_APP.encode()).hexdigest()})
    plan=await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    bootstrap=Path(plan.spec.env['COPILOT_CLI_DIST_DIR'])/'index.js';bootstrap.unlink()
    with pytest.raises(UnsupportedCapabilityError,match='启动配置已变化'):copilot.cli_command(plan,('native','--resume','id'))


async def test_copilot_guard_rejects_module_replacement_before_loading(tmp_path,copilot_fixture):
    node=shutil.which('node')
    if not node:pytest.skip('Node is not installed')
    spec,binding=setup(tmp_path,'copilot');spec=spec.with_env(HOME=str(tmp_path/'home'))
    plan=await copilot.project(spec,binding,effective(tmp_path,binding),tmp_path)
    manifest=json.loads(Path(plan.spec.env['KAUS_COPILOT_SKILL_SCOPE']).read_text())
    Path(manifest['module']).write_text('throw Error("should never run");')
    result=subprocess.run([node,str(Path(plan.spec.env['COPILOT_CLI_DIST_DIR'])/'index.js')],env=plan.spec.build_env(),capture_output=True,text=True)
    assert result.returncode and '重新验证' in result.stderr and 'should never run' not in result.stderr
