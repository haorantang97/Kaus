import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.session_mcp import projection
from drivers.base import UnsupportedCapabilityError


@pytest.mark.parametrize('kind', ['pi', 'commandcode'])
async def test_prepared_spec_roundtrip_replaces_and_removes_mcp_without_changing_other_settings(tmp_path, kind):
    native = tmp_path/'native'; native.write_text('#!/bin/sh\n'); native.chmod(0o700)
    command = ('acp',) if kind == 'pi' else ('bridge', '--command', str(native))
    env = {'KAUS_SKILL_STATE_ROOT': str(tmp_path/'state'), 'KEEP_SETTING': 'kept', 'PI_ACP_PI_COMMAND': str(native)}
    spec = AcpAgentSpec(command=command, env=env, inherit_env=False, cwd=str(tmp_path))
    conv = SimpleNamespace(id='one')
    def metadata(value): return {'mcpServers': [{'name':'fixture','command':str(native),'args':[value],'env':[{'name':'KEY','value':'secret-example'}]}]}
    a = await projection.prepare(spec, conv, metadata('A'), kind=kind)
    b = await projection.prepare(a, conv, metadata('B'), kind=kind)
    assert json.loads(Path(b.env[projection.CONFIG_ENV]).read_text())['servers'][0]['args'] == ['B']
    launcher = Path(b.env['PI_ACP_PI_COMMAND'] if kind == 'pi' else b.command[b.command.index('--command')+1])
    assert 'secret-example' not in launcher.read_text()
    assert str(native) in launcher.read_text()
    assert (Path(b.env[projection.CONFIG_ENV]).stat().st_mode & 0o777) == 0o600
    empty = await projection.prepare(b, conv, {'mcpServers':[]}, kind=kind)
    assert empty == spec


async def test_receipt_requires_current_native_process_nonce(tmp_path):
    path = tmp_path/'receipt.json'
    spec = AcpAgentSpec(command=('unused',), inherit_env=False, env={projection.RECEIPT_ENV:str(path), projection.NONCE_ENV:'current'})
    path.write_text(json.dumps({'schema':1,'nonce':'old','ok':True}))
    with pytest.raises(UnsupportedCapabilityError): await projection.verify(spec)
    assert not path.exists()
    path.write_text(json.dumps({'schema':1,'nonce':'current','ok':True}))
    await projection.verify(spec)
    assert not path.exists()


def test_terminal_empty_latest_selection_does_not_reuse_old_tools(tmp_path):
    native=tmp_path/'native';native.write_text('#!/bin/sh\n')
    spec=AcpAgentSpec(command=('unused',),env={'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')},inherit_env=False)
    conv=SimpleNamespace(id='terminal',agent_binding_id='binding')
    command=(str(native),'--resume','same')
    projection.remember(spec,conv,{'mcpServers':[{'name':'fixture','command':str(native),'args':[],'env':[]}]})
    changed=projection.terminal(spec,conv,command,kind='pi')
    assert changed[0]!=command[0] and changed[1:]==command[1:]
    projection.remember(spec,conv,{'mcpServers':[]})
    assert projection.terminal(spec,conv,command,kind='pi')==command
