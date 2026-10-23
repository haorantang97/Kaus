import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.session_mcp import projection
from drivers.skill_execution import node_package

SOURCE = '''
import {spawnSync} from 'node:child_process';
function submit(p){const i={feed:[],setFeed(){},setLiveEntries(){},isCmdCodeBusy:false};const I={};I.current={feed:i.feed,setFeed:i.setFeed,setLiveEntries:i.setLiveEntries,isCmdCodeBusy:i.isCmdCodeBusy,};return I.current.isCmdCodeBusy;}
function useQueuedMessageFlush(options){return options.isCmdCodeBusy;}
function flush(D){const C={isCmdCodeBusy:false};return useQueuedMessageFlush({isCmdCodeBusy:C.isCmdCodeBusy,});}
const child=spawnSync(process.execPath,['-e','console.log(JSON.stringify(process.env.KAUS_COMMANDCODE_TUI_COMPAT??null))'],{encoding:'utf8'});
console.log(JSON.stringify({flags:[submit(false),submit(true),flush(false),flush(true)],child:JSON.parse(child.stdout)}));
'''


def prepare(tmp_path, monkeypatch, *, verified=True):
    package=tmp_path/'package';entry=package/'dist/cli.mjs';entry.parent.mkdir(parents=True);entry.write_text(SOURCE)
    monkeypatch.setattr(node_package,'locate',lambda *args:package)
    if verified:
        monkeypatch.setattr(projection,'COMMANDCODE_TUI_VERIFIED',{hashlib.sha256(entry.read_bytes()).hexdigest()})
    spec=AcpAgentSpec(command=('bridge','--command','native'),env={'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')},inherit_env=False)
    script=projection._terminal_input_script(spec,SimpleNamespace(id='one'))
    manifest=next((tmp_path/'state').rglob('*.commandcode-tui.json'))
    preload=next((tmp_path/'state').rglob('*.mjs'))
    assert 'KAUS_COMMANDCODE_TUI_COMPAT' in script and 'NODE_OPTIONS' in script
    return entry,manifest,preload


@pytest.mark.parametrize('tamper',[False,True])
async def test_ready_controls_native_queue_without_changing_installed_source(tmp_path,monkeypatch,tamper):
    node=shutil.which('node')
    if not node:pytest.skip('Node is unavailable')
    entry,manifest,preload=prepare(tmp_path,monkeypatch)
    if tamper:entry.write_text(SOURCE+'\n// changed after verification\n')
    before=entry.read_bytes()
    process=await asyncio.create_subprocess_exec(node,'--import',preload.as_uri(),str(entry),
        env={**os.environ,'KAUS_COMMANDCODE_TUI_COMPAT':str(manifest),'NODE_OPTIONS':'--import='+preload.as_uri()},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(process.communicate(),5)
    assert entry.read_bytes()==before
    if tamper:
        assert process.returncode!=0 and not out and '版本已变化' in err.decode()
    else:
        assert process.returncode==0,err.decode()
        assert json.loads(out)=={'flags':[True,False,True,False],'child':None}


def test_unknown_native_version_stops_before_launch(tmp_path,monkeypatch):
    with pytest.raises(UnsupportedCapabilityError,match='版本尚未兼容'):
        prepare(tmp_path,monkeypatch,verified=False)
    assert (tmp_path/'package/dist/cli.mjs').read_text()==SOURCE
