"""Actual launcher scripts transfer connection settings without serializing values."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import CliLaunchSpec
from drivers.terminals.environment import EnvironmentRelay, receive, selected_environment
from drivers.terminals import integrations
from runtime.external_cli import TerminalLauncher, launch_dir

CONVERSATION = 'conversation:22222222-2222-4222-8222-222222222222'
SECRET = 'synthetic secret " $() value'


def test_launch_spec_excludes_values_in_all_wire_and_repr_forms():
    spec = CliLaunchSpec(command=('agent',), private_env={'API_KEY':SECRET})
    for value in (repr(spec), str(spec), spec.model_dump_json(), str(spec.model_dump()),
                  str(spec.model_dump(mode='json', by_alias=True)), str(spec.model_dump(include={'private_env'}))):
        assert SECRET not in value and 'private_env' not in value and 'privateEnv' not in value
    assert spec.model_copy().private_env == {'API_KEY':SECRET}


def test_only_connection_overrides_and_declared_auth_are_forwarded(monkeypatch):
    monkeypatch.setenv('SHARED_AUTH','inherited-synthetic')
    monkeypatch.setenv('UNRELATED_SECRET','must-not-forward')
    spec=AcpAgentSpec(command=('agent',),env={'API_KEY':SECRET,'HOME':'/fixture',
        'PATH':'/engine/bin','CMUX_WORKSPACE_ID':'stale','TERM':'wrong','KAUS_LAUNCH_ID':'old'})
    assert selected_environment(spec,('SHARED_AUTH','ABSENT_AUTH')) == {
        'SHARED_AUTH':'inherited-synthetic','API_KEY':SECRET,'HOME':'/fixture'}


@pytest.mark.parametrize('app', ['Terminal','iTerm','cmux'])
async def test_missing_connection_auth_keeps_login_shell_auth(tmp_path,app):
    native=tmp_path/'native.py'
    native.write_text('import os,hashlib\nprint(hashlib.sha256(os.environ["SHELL_ONLY_AUTH"].encode()).hexdigest())\n')
    source=AcpAgentSpec(command=(sys.executable,str(native)),inherit_env=False,
        env={'MODEL_URL':'http://127.0.0.1:9/v1'})
    private=selected_environment(source,('SHELL_ONLY_AUTH',))
    assert 'SHELL_ONLY_AUTH' not in private
    children=[]
    def opener(app,script):
        children.append(subprocess.Popen(['/bin/sh',str(script)],
            env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'SHELL_ONLY_AUTH':SECRET},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        return True,None
    launcher=TerminalLauncher(root=tmp_path/'launches',app=app,opener=opener,environment_timeout=5)
    result=await launcher.launch(CliLaunchSpec(command=source.command,private_env=private),conversation_id=CONVERSATION)
    assert result.launched
    out,err=await asyncio.to_thread(children[0].communicate,timeout=8)
    assert children[0].returncode==0,err
    assert out.strip()==hashlib.sha256(SECRET.encode()).hexdigest()
    assert SECRET not in result.launch.model_dump_json()+repr(result)+out+err
    assert not launcher._environment_relays
    await launcher.aclose()


@pytest.mark.parametrize('app', ['Terminal','iTerm','cmux'])
async def test_run_command_receives_private_values_and_keeps_terminal_integration(tmp_path,monkeypatch,app):
    native=tmp_path/'native.py'
    native.write_text('import os,json,hashlib\nprint(json.dumps({"authHash":hashlib.sha256(os.environ["API_KEY"].encode()).hexdigest(),"url":os.environ["MODEL_URL"],"cmux":os.environ.get("CMUX_WORKSPACE_ID"),"term":os.environ.get("TERM"),"oldAuth":os.environ.get("OTHER_AUTH"),"config":os.environ.get("FIXTURE_CONFIG_HOME"),"args":__import__("sys").argv[1:]}))\n')
    if app == 'cmux':
        bundle=tmp_path/'cmux.app';wrapper=bundle/'Contents/Resources/bin/cmux-codex-wrapper'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/bin/sh\n[ -n "$API_KEY" ] || exit 92\nexec "$CMUX_CUSTOM_CODEX_PATH" "$@"\n');wrapper.chmod(0o700)
        monkeypatch.setattr(integrations,'terminal_app_path',lambda app:bundle)
    terminal_env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'TERM':'terminal-fixture',
                  'CMUX_WORKSPACE_ID':'fresh-workspace','OTHER_AUTH':'stale-shell-auth'}
    children=[]
    def opener(app,script):
        children.append(subprocess.Popen(['/bin/sh',str(script)],env=terminal_env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        return True,None
    launcher=TerminalLauncher(root=tmp_path/'launches',app=app,opener=opener,environment_timeout=5)
    spec=CliLaunchSpec(command=(sys.executable,str(native),'id with spaces'),terminal_integration='codex',
        config_directory=('FIXTURE_CONFIG_HOME',str(tmp_path/'chosen-config')),
        private_env={'API_KEY':SECRET,'MODEL_URL':'http://127.0.0.1:9/v1','OTHER_AUTH':None,
                     'CMUX_WORKSPACE_ID':'stale-connection','TERM':'stale-connection'})
    result=await launcher.launch(spec,conversation_id=CONVERSATION)
    assert result.launched
    out,err=await asyncio.to_thread(children[0].communicate,timeout=8)
    assert children[0].returncode==0,err
    row=json.loads(out)
    assert row=={'authHash':hashlib.sha256(SECRET.encode()).hexdigest(),'url':'http://127.0.0.1:9/v1',
        'cmux':'fresh-workspace','term':'terminal-fixture','oldAuth':None,
        'config':str(tmp_path/'chosen-config'),'args':['id with spaces']}
    assert not launcher._environment_relays
    assert SECRET not in result.launch.model_dump_json()+repr(result)+err+out
    for path in result.script_path.parent.rglob('*'):
        if path.is_file():assert SECRET not in path.read_text()
    replay=await asyncio.create_subprocess_exec('/bin/sh',str(result.script_path),env=terminal_env,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    _,error=await replay.communicate()
    assert replay.returncode==125 and SECRET.encode() not in error
    await launcher.aclose()


async def test_one_use_socket_cleans_up_and_cannot_be_replayed():
    relay=await EnvironmentRelay({'API_KEY':SECRET},timeout=1).start()
    root=relay.directory;path=relay.path
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert await asyncio.to_thread(receive,path)=={'API_KEY':SECRET}
    assert await relay.wait()
    assert not root.exists() and relay._values=={}
    with pytest.raises(OSError):await asyncio.to_thread(receive,path)


async def test_timeout_and_close_clear_pending_values():
    relay=await EnvironmentRelay({'API_KEY':SECRET},timeout=.02).start()
    root=relay.directory
    assert not await relay.wait()
    assert not root.exists() and relay._values=={}
    relay=await EnvironmentRelay({'API_KEY':SECRET},timeout=5).start()
    await relay.aclose()
    assert not await relay.wait() and relay._values=={}


@pytest.mark.parametrize('behavior',['failed','timeout','raises'])
async def test_failed_open_releases_relay_and_revokes_delayed_script(tmp_path,behavior):
    def opener(app,path):
        if behavior=='raises':raise RuntimeError('synthetic failure')
        return (behavior=='timeout',None)
    launcher=TerminalLauncher(root=tmp_path,opener=opener,environment_timeout=.03)
    result=await launcher.launch(CliLaunchSpec(command=('/usr/bin/true',),private_env={'API_KEY':SECRET}),conversation_id=CONVERSATION)
    assert not result.launched and not launcher._environment_relays
    assert (result.script_path.parent/'revoked').exists()
    assert SECRET not in result.launch.model_dump_json()+repr(result)


async def test_stop_and_shutdown_revoke_unconsumed_transfers(tmp_path):
    signal=asyncio.Event()
    def opener(app,path):signal_loop.call_soon_threadsafe(signal.set);return True,None
    signal_loop=asyncio.get_running_loop()
    launcher=TerminalLauncher(root=tmp_path,opener=opener,environment_timeout=5)
    task=asyncio.create_task(launcher.launch(CliLaunchSpec(command=('/usr/bin/true',),private_env={'API_KEY':SECRET}),conversation_id=CONVERSATION))
    await signal.wait()
    await launcher.aclose()
    result=await task
    assert not result.launched and not launcher._environment_relays


async def test_invalid_environment_never_falls_back_to_native(tmp_path):
    opened=[]
    launcher=TerminalLauncher(root=tmp_path,opener=lambda *args:(opened.append(args) or True,None))
    result=await launcher.launch(CliLaunchSpec(command=('/usr/bin/true',),private_env={'invalid-name':SECRET}),conversation_id=CONVERSATION)
    assert not result.launched and not opened
    assert SECRET not in result.launch.model_dump_json()+repr(result)


async def test_acp_launch_carries_only_the_selected_connection_environment(tmp_path):
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset
    from app.projects.models import AgentBinding
    from app.conversations.models import Conversation
    native=tmp_path/'custom-native';native.write_text('#!/bin/sh\nexit 0\n');native.chmod(0o700)
    spec=AcpAgentSpec(command=(str(native),'--acp'),inherit_env=False,
        env={'API_KEY':SECRET,'OPENAI_BASE_URL':'http://127.0.0.1:9/v1','HOME':str(tmp_path),
             'PATH':os.environ['PATH'],'CMUX_WORKSPACE_ID':'old'})
    driver=AcpDriver(spec,backend_key='qwen',preset=get_preset('qwen'))
    binding=AgentBinding.create(project='project:fixture',backend='qwen');driver.register_binding(binding)
    conv=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture').model_copy(update={'native_session_id':'same-id'})
    result=await driver.build_external_cli_launch(conv)
    assert result.private_env['API_KEY']==SECRET
    assert result.private_env['OPENAI_BASE_URL']=='http://127.0.0.1:9/v1'
    assert 'CMUX_WORKSPACE_ID' not in result.private_env and 'PATH' not in result.private_env
    assert result.command[:3]==(str(native),'--resume','same-id')
    assert SECRET not in result.model_dump_json()+repr(result)


async def test_cancel_pending_launch_revokes_socket_and_script(tmp_path):
    signal=asyncio.Event();loop=asyncio.get_running_loop()
    def opener(app,path):loop.call_soon_threadsafe(signal.set);return True,None
    launcher=TerminalLauncher(root=tmp_path,opener=opener,environment_timeout=5)
    task=asyncio.create_task(launcher.launch(CliLaunchSpec(command=('/usr/bin/true',),private_env={'API_KEY':SECRET}),conversation_id=CONVERSATION))
    await signal.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not launcher._environment_relays
    assert list(tmp_path.glob('*/revoked'))


async def test_stop_pending_launch_revokes_socket(tmp_path):
    from types import SimpleNamespace
    signal=asyncio.Event();loop=asyncio.get_running_loop()
    def opener(app,path):loop.call_soon_threadsafe(signal.set);return True,None
    launcher=TerminalLauncher(root=tmp_path,opener=opener,environment_timeout=5)
    task=asyncio.create_task(launcher.launch(CliLaunchSpec(command=('/usr/bin/true',),private_env={'API_KEY':SECRET}),conversation_id=CONVERSATION))
    await signal.wait();launch_id=next(iter(launcher._environment_relays))
    assert (await launcher.stop(SimpleNamespace(id=launch_id,external_process_ref=None)))[0]
    assert not (await task).launched and not launcher._environment_relays
