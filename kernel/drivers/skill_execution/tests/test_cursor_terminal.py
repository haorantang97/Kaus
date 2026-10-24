import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from app.conversations.models import Conversation
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import cursor, cursor_terminal as adapter
from test_selection import setup

SID = '11111111-2222-4333-8444-555555555555'


def context(tmp_path):
    spec, binding = setup(tmp_path, 'cursor')
    spec = spec.with_env(CURSOR_CONFIG_DIR=str(tmp_path/'native'))
    native, terminal, _ = adapter.session_paths(spec, SID, str(tmp_path))
    native.mkdir(parents=True)
    (native/'meta.json').write_text(json.dumps({'schemaVersion':1,'cwd':str(tmp_path)}))
    with sqlite3.connect(native/'store.db') as db:
        db.execute('create table messages (body text)');db.execute('insert into messages values (?)', ('card first',))
    return spec, binding, native, terminal


def test_directory_bridge_keeps_one_database_and_wal(tmp_path):
    spec, _, native, terminal = context(tmp_path)
    adapter.bridge_store(spec, SID, str(tmp_path))
    assert terminal.is_symlink() and terminal.resolve() == native
    assert (terminal/'store.db').stat().st_ino == (native/'store.db').stat().st_ino
    with sqlite3.connect(terminal/'store.db') as cli:
        cli.execute('pragma journal_mode=WAL')
        cli.execute('insert into messages values (?)', ('terminal second',));cli.commit()
        assert (native/'store.db-wal').exists()
        with sqlite3.connect(native/'store.db') as card:
            assert card.execute('select body from messages').fetchall() == [('card first',),('terminal second',)]
    adapter.bridge_store(spec, SID, str(tmp_path))


def test_bridge_rejects_another_workspace_or_existing_store(tmp_path):
    spec, _, native, terminal = context(tmp_path)
    with pytest.raises(UnsupportedCapabilityError, match='工作区'):
        adapter.bridge_store(spec, SID, str(tmp_path/'other'))
    terminal.mkdir(parents=True);(terminal/'keep').write_text('personal')
    with pytest.raises(UnsupportedCapabilityError, match='另一份'):
        adapter.bridge_store(spec, SID, str(tmp_path))
    assert (terminal/'keep').read_text() == 'personal'
    with pytest.raises(ValueError): adapter.bridge_store(spec, '../outside', str(tmp_path))


def test_terminal_choices_forward_native_model_and_approval_without_guessing(tmp_path):
    spec, binding = setup(tmp_path, 'cursor')
    conv = Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture').evolve(
        model_id='opus[effort=high]',execution_mode='plan',approval_mode='host:ask')
    command = cursor.external_cli_command(None, ('cursor-agent','--resume',SID),conversation=conv,binding=binding,spec=spec)
    assert command == ('cursor-agent','--resume',SID,'--model','opus[effort=high]','--mode','plan')
    with pytest.raises(UnsupportedCapabilityError, match='自动审批'):
        cursor.external_cli_command(None, command,conversation=conv.evolve(approval_mode='host:auto'),binding=binding,spec=spec)


def test_mcp_overlay_merges_project_only_and_retains_native_skill_mode(tmp_path):
    node = shutil.which('node')
    if not node: pytest.skip('Node.js unavailable')
    # Exercise the exact verified parser seam, including a missing project file.
    index = tmp_path/'index.js'
    index.write_text('const i=require("node:fs/promises"),E={qg:JSON.parse},I=x=>x,P={parse:x=>x};'
                     'function* parser(e,t){const r=yield(0,i.readFile)(e,"utf8"),n=I((0,E.qg)(r),t);return P.parse(n)};'
                     'module.exports=async function(e){const g=parser(e);let n=g.next();while(!n.done){n=g.next(await n.value)}return n.value};')
    project = tmp_path/'project.json'; personal = tmp_path/'personal.json'
    personal.write_text('{"mcpServers":{"personal":{"command":"keep"}}}')
    manifest = tmp_path/'scope.json';manifest.write_text(json.dumps({'modules':{str(index):hashlib.sha256(index.read_bytes()).hexdigest()},'paths':None}))
    guard = Path(cursor.__file__).with_name('cursor_guard.cjs')
    script = '''const assert=require('node:assert'),fs=require('node:fs');
(async()=>{const [index,project,personal]=process.argv.slice(1),read=require(index);
assert(!process.env.KAUS_CURSOR_TERMINAL_MCP);
assert.deepStrictEqual(await read(project),{mcpServers:{selected:{command:'fixture',env:{TOKEN:'synthetic-secret'}}}});
assert.deepStrictEqual(await read(personal),{mcpServers:{personal:{command:'keep'}}});
fs.writeFileSync(project,JSON.stringify({mcpServers:{existing:{command:'preserved'}}}));
assert.deepStrictEqual(Object.keys((await read(project)).mcpServers).sort(),['existing','selected']);
console.log('verified')})().catch(e=>{console.error(e);process.exitCode=1});'''
    env = {**os.environ,'KAUS_CURSOR_SKILL_SCOPE':str(manifest),'NODE_OPTIONS':'--require='+json.dumps(str(guard),ensure_ascii=False),
           'KAUS_CURSOR_TERMINAL_MCP':json.dumps({'path':str(project),'servers':{'selected':{'command':'fixture','env':{'TOKEN':'synthetic-secret'}}}})}
    result = subprocess.run((node,'-e',script,str(index),str(project),str(personal)),env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'verified'
    assert 'synthetic-secret' not in project.read_text() and 'synthetic-secret' not in manifest.read_text()
