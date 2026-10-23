"""Native terminal identity/config mapping; no model, account or user-home calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest
from unittest.mock import AsyncMock
from app.conversations.models import Conversation
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import commandcode, pi, pi_runtime
from test_selection import setup


def test_precise_native_resume_templates():
    from drivers.acp.presets import get_preset
    assert get_preset('pi').resume_argv('same-id') == ('pi','--session','same-id')
    assert get_preset('commandcode').resume_argv('bridge-id') == ('command-code','--resume','bridge-id')


def fixture(tmp_path):
    spec, binding = setup(tmp_path)
    work = tmp_path/'work'; work.mkdir()
    binding = binding.model_copy(update={'runtime_config': {'workspace_root': str(work)}})
    records = tmp_path/'bridge'; records.mkdir()
    spec = spec.model_copy(update={'command': ('bridge', '--state-dir', str(records)), 'cwd': str(work)})
    conv = Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture')
    ident = str(uuid.uuid4()); native = str(uuid.uuid4())
    conv = conv.model_copy(update={'native_session_id': ident})
    data = {'version':1,'id':ident,'nativeId':native,'cwd':str(work),'model':'provider/saved','effort':'low','mode':'plan','history':[]}
    path = records/(ident+'.json'); path.write_text(json.dumps(data))
    return spec,binding,conv,path,data


def terminal(adapter,spec,binding,conv,plan=None):
    return adapter.external_cli_command(plan,('/native','--resume',conv.native_session_id),conversation=conv,binding=binding,spec=spec)


def test_commandcode_maps_native_id_and_saved_configuration(tmp_path):
    spec,binding,conv,path,data=fixture(tmp_path)
    before=path.read_bytes()
    cmd=terminal(commandcode,spec,binding,conv)
    assert cmd==('/native','--resume',data['nativeId'],'--no-auto-update','--skip-onboarding',
        '--model','provider/saved','--effort','low','--permission-mode','plan')
    assert conv.native_session_id not in cmd and path.read_bytes()==before


def test_commandcode_explicit_current_settings_override_saved(tmp_path):
    spec,binding,conv,path,data=fixture(tmp_path)
    conv=conv.model_copy(update={'model_id':'provider/current','reasoning_mode':'high','approval_mode':'native:plan'})
    cmd=terminal(commandcode,spec,binding,conv)
    assert cmd[-6:]==('--model','provider/current','--effort','high','--permission-mode','plan')


@pytest.mark.parametrize('failure',['missing','no-native','wrong-cwd','wrong-id','link','fifo','bad-json','bad-setting'])
def test_commandcode_invalid_mapping_never_opens_another_session(tmp_path,failure):
    spec,binding,conv,path,data=fixture(tmp_path)
    if failure=='missing':path.unlink()
    elif failure=='no-native':data['nativeId']=None;path.write_text(json.dumps(data))
    elif failure=='wrong-cwd':data['cwd']=str(tmp_path/'other');path.write_text(json.dumps(data))
    elif failure=='wrong-id':data['id']=str(uuid.uuid4());path.write_text(json.dumps(data))
    elif failure=='link':target=path.with_suffix('.target');path.rename(target);path.symlink_to(target)
    elif failure=='fifo':path.unlink();os.mkfifo(path)
    elif failure=='bad-setting':data['mode']=[];path.write_text(json.dumps(data))
    else:path.write_text('broken')
    with pytest.raises(UnsupportedCapabilityError,match='映射不可用'):
        terminal(commandcode,spec,binding,conv)


@pytest.mark.parametrize('adapter',[pi,commandcode])
@pytest.mark.parametrize('mode',['host:auto','host:ask'])
def test_terminal_does_not_expand_unsupported_approval(tmp_path,adapter,mode):
    spec,binding,conv,*_=fixture(tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        terminal(adapter,spec,binding,conv.model_copy(update={'approval_mode':mode}))


def test_pi_explicit_model_and_thinking_preserve_native_id(tmp_path):
    spec,binding,conv,*_=fixture(tmp_path)
    cmd=terminal(pi,spec,binding,conv.model_copy(update={'model_id':'provider/current','reasoning_mode':'low'}))
    assert cmd==('/native','--resume',conv.native_session_id,'--model','provider/current','--thinking','low')


def scope(tmp_path,adapter):
    native=tmp_path/'native';native.write_text('#!/bin/sh\nprintf "%s\\n" "$KAUS_PI_SKILL_SCOPE" "$NODE_OPTIONS" "$@"\n');native.chmod(0o700)
    script='#!/bin/sh\nexec '+str(native)+' "$@"\n'
    launcher=tmp_path/('native-launch-'+hashlib.sha256(script.encode()).hexdigest()[:24]+'.sh');launcher.write_text(script);launcher.chmod(0o700)
    manifest=tmp_path/'pi.json';manifest.write_text('{}')
    source=Path(pi.__file__).with_name('pi_guard.mjs').read_text()
    guard=tmp_path/('pi-guard-'+hashlib.sha256(source.encode()).hexdigest()[:24]+'.mjs');guard.write_text(source)
    spec=SimpleNamespace(command=('bridge','--command',str(launcher)),env={'PI_ACP_PI_COMMAND':str(launcher),'KAUS_PI_SKILL_SCOPE':str(manifest)})
    return SimpleNamespace(root=tmp_path,spec=spec),launcher,guard,manifest


def test_pi_terminal_retains_dynamic_skill_guard_without_serializing_original_node_options(tmp_path):
    plan,launcher,guard,manifest=scope(tmp_path,pi)
    cmd=pi.cli_command(plan,('/native','--session','same-id'))
    value='--existing-synthetic-secret'
    out=subprocess.run(cmd,env={**os.environ,'NODE_OPTIONS':value},capture_output=True,text=True,check=True).stdout.splitlines()
    assert out==[str(manifest),value+' --import='+guard.as_uri(),'--session','same-id']
    assert value not in Path(cmd[0]).read_text()
    assert pi.cli_command(plan,())==()


@pytest.mark.parametrize('adapter',[pi,commandcode])
def test_modified_native_skill_launcher_rejected(tmp_path,adapter):
    plan,launcher,*_=scope(tmp_path,adapter)
    launcher.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(UnsupportedCapabilityError,match='已变化'):
        adapter.cli_command(plan,('/native','--resume','id'))


@pytest.mark.parametrize('adapter',[pi,commandcode])
async def test_card_preparation_then_terminal_preserves_skill_loader_and_latest_mcp(tmp_path,monkeypatch,adapter):
    from drivers.acp.client import AcpAgentSpec
    from drivers.acp.driver import AcpDriver
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec
    from drivers.base import CreateSessionOptions
    from drivers.session_mcp import projection
    monkeypatch.setattr(projection,'_terminal_input_script',lambda *args:'# native harness readiness verified separately\n')
    spec,binding,conv,*_=fixture(tmp_path)
    plan,launcher,guard,manifest=scope(tmp_path,adapter)
    plan.spec=AcpAgentSpec(command=(*spec.command,'--command',str(launcher)),cwd=spec.cwd,
        env={**spec.env,**plan.spec.env},inherit_env=False)
    plan.metadata={}
    original=plan.spec
    metadata={'mcpServers':[{'name':'fixture','command':'synthetic','args':['A'],'env':[]}]}
    driver=AcpDriver(spec,backend_key=binding.backend_key)
    driver.register_binding(binding)
    driver._skill_plans[conv.id]=plan
    monkeypatch.setattr(driver,'_skill_adapter',lambda:adapter)
    connect=driver._connect
    async def fake_connect(**kwargs):
        return await connect(workspace_root=kwargs['workspace_root'],agent_spec=fake_agent_spec(cwd=spec.cwd))
    monkeypatch.setattr(driver,'_connect',fake_connect)
    monkeypatch.setattr(driver,'_resume_session',AsyncMock(return_value={'sessionId':conv.native_session_id}))
    monkeypatch.setattr(projection,'verify',AsyncMock())
    async def rpc(prepared,*args):
        assert projection.BASE_ENV not in prepared.env
        assert prepared.env['PI_ACP_PI_COMMAND']==str(launcher)
        return prepared
    monkeypatch.setattr(pi,'_prepare_rpc_spec',rpc)
    adapter.remember_session_options(spec,conv,metadata)
    handle=await driver.start_runtime(conv,'card',session_options=CreateSessionOptions(metadata=metadata))
    try:
        # Exercise the actual driver assignment of prepared spec back to plan.
        assert projection.BASE_ENV in plan.spec.env
        driver.validate_external_session(conv)
        cmd=terminal(adapter,spec,binding,conv,plan)
        assert '--extension' in Path(cmd[0]).read_text() if adapter is pi else '--mod' in Path(cmd[0]).read_text()
        # Reopening with an empty selection must not execute the old MCP wrapper.
        adapter.remember_session_options(spec,conv,{})
        empty=terminal(adapter,spec,binding,conv,plan)
        text=Path(empty[0]).read_text()
        assert projection.CONFIG_ENV not in text
        if adapter is pi:
            assert str(launcher) in text and str(manifest) in text
        else:
            assert str(launcher) in text and '--mod' not in text
        assert projection.restore_native_spec(plan.spec,kind='pi' if adapter is pi else 'commandcode')==original
        # The same prepared spec can cold-start without wrapping old MCP again.
        restored=await adapter.prepare_runtime_spec(plan.spec,conv,{})
        assert projection.BASE_ENV not in restored.env
    finally:
        await driver.stop_runtime(handle)


async def test_pi_rpc_unknown_pair_fails_before_start_without_modifying_native(tmp_path,monkeypatch):
    spec,binding,conv,*_=fixture(tmp_path)
    package=tmp_path/'package';rpc=package/'dist/modes/rpc/rpc-mode.js';session=package/'dist/core/agent-session.js'
    rpc.parent.mkdir(parents=True);session.parent.mkdir(parents=True)
    rpc.write_text('unknown rpc');session.write_text('unknown session')
    monkeypatch.setattr(pi_runtime,'package_for',lambda spec:package)
    with pytest.raises(UnsupportedCapabilityError,match='尚未兼容'):
        await pi_runtime.prepare_runtime_spec(spec,conv,{})
    assert rpc.read_text()=='unknown rpc' and session.read_text()=='unknown session'


async def test_pi_rpc_compatible_native_keeps_own_interface(tmp_path,monkeypatch):
    spec,binding,conv,*_=fixture(tmp_path)
    package=tmp_path/'package';rpc=package/'dist/modes/rpc/rpc-mode.js';session=package/'dist/core/agent-session.js'
    rpc.parent.mkdir(parents=True);session.parent.mkdir(parents=True)
    rpc.write_text('get_available_thinking_levels');session.write_text('agent_settled')
    monkeypatch.setattr(pi_runtime,'package_for',lambda spec:package)
    assert await pi_runtime.prepare_runtime_spec(spec,conv,{}) is spec


async def test_pi_rpc_preload_precedes_skill_preload_and_source_stays_unchanged(tmp_path,monkeypatch):
    spec,binding,conv,*_=fixture(tmp_path)
    spec=spec.with_env(NODE_OPTIONS='--import=existing-skill-guard')
    package=tmp_path/'package';rpc=package/'dist/modes/rpc/rpc-mode.js';session=package/'dist/core/agent-session.js'
    rpc.parent.mkdir(parents=True);session.parent.mkdir(parents=True)
    rpc.write_text('fixture rpc');session.write_text('fixture session')
    monkeypatch.setattr(pi_runtime,'package_for',lambda spec:package)
    monkeypatch.setattr(pi_runtime,'VERIFIED',{tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in (rpc,session))})
    result=await pi_runtime.prepare_runtime_spec(spec,conv,{})
    assert result.env['NODE_OPTIONS'].endswith(' --import=existing-skill-guard')
    manifest=Path(result.env['KAUS_PI_RPC_COMPAT'])
    assert manifest.stat().st_mode & 0o077 == 0
    assert json.loads(manifest.read_text())['rpc']==str(rpc.resolve())
    assert rpc.read_text()=='fixture rpc' and session.read_text()=='fixture session'
