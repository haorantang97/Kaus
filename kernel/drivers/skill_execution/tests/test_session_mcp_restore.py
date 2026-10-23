"""Cold restore configuration tests; native runtime evidence lives in output/."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import copilot
from drivers.goose import mcp_scope


def spec_for(tmp_path):
    source=tmp_path/'native';source.mkdir()
    return AcpAgentSpec(command=('synthetic',), inherit_env=False,
        env={'HOME':str(tmp_path/'home'),'COPILOT_HOME':str(source),'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')})


def server(label='A'):
    return {'name':'selected','command':'synthetic','args':[label],'env':[{'name':'TEST_SECRET','value':'synthetic-secret'}]}


async def test_copilot_private_config_replaces_and_removes_owned_mcp_preserving_skills(tmp_path):
    spec=spec_for(tmp_path);source=Path(spec.env['COPILOT_HOME'])
    settings={'skillDirectories':['/synthetic/selected'],'disabledSkills':['builtin'],'ignoredSkillsLocations':['/synthetic/ignored']}
    (source/'settings.json').write_text(json.dumps(settings))
    baseline={'mcpServers':{'native':{'command':'native-config'}}}
    (source/'mcp-config.json').write_text(json.dumps(baseline))
    conv=SimpleNamespace(id='conversation',native_session_id=None)
    prepared=await copilot.prepare_runtime_spec(spec,conv,{'mcpServers':[server()]})
    private=Path(prepared.env['COPILOT_HOME'])
    assert copilot.read_jsonc(private/'settings.json')==settings
    assert copilot.read_jsonc(private/'mcp-config.json')['mcpServers']['selected']['args']==['A']
    assert (private/'mcp-config.json').stat().st_mode & 0o777==0o600
    assert 'selected' not in copilot.read_jsonc(source/'mcp-config.json')['mcpServers']
    prepared=await copilot.prepare_runtime_spec(prepared,conv,{'mcpServers':[server('B')]})
    assert copilot.read_jsonc(private/'mcp-config.json')['mcpServers']['selected']['args']==['B']
    await copilot.prepare_runtime_spec(prepared,conv,{'mcpServers':[]})
    assert copilot.read_jsonc(private/'mcp-config.json')==baseline
    assert copilot.read_jsonc(private/'settings.json')==settings
    remote={'type':'http','name':'remote','url':'https://synthetic.invalid/mcp','headers':[]}
    metadata=await copilot.before_runtime_session(None,prepared,conv,{'mcpServers':[server(),remote],'_meta':{'selected':'retained'}})
    assert metadata=={'mcpServers':[remote],'_meta':{'selected':'retained'}}


async def test_copilot_links_only_exact_session_and_rejects_changed_link(tmp_path):
    spec=spec_for(tmp_path);source=Path(spec.env['COPILOT_HOME'])
    for sid in ('resume-id','another-id'):
        path=source/'session-state'/sid;path.mkdir(parents=True);(path/'events.jsonl').write_text('{}\n')
    conv=SimpleNamespace(id='conversation',native_session_id='resume-id')
    prepared=await copilot.prepare_runtime_spec(spec,conv,{'mcpServers':[server()]})
    sessions=Path(prepared.env['COPILOT_HOME'])/'session-state'
    assert [p.name for p in sessions.iterdir()]==['resume-id']
    assert (sessions/'resume-id').resolve()==source/'session-state/resume-id'
    (sessions/'resume-id').unlink();(sessions/'resume-id').symlink_to(source/'session-state/another-id')
    with pytest.raises(UnsupportedCapabilityError,match='链接已变化'):
        await copilot.prepare_runtime_spec(prepared,conv,{'mcpServers':[server()]})


@pytest.mark.parametrize('sid',['../../escape','/tmp/other','valid-but-missing'])
async def test_copilot_invalid_or_missing_native_session_is_not_recreated(tmp_path,sid):
    spec=spec_for(tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        await copilot.prepare_runtime_spec(spec,SimpleNamespace(id='c',native_session_id=sid),{'mcpServers':[server()]})


async def test_copilot_native_empty_untouched_and_auth_not_copied(tmp_path):
    spec=spec_for(tmp_path);conv=SimpleNamespace(id='c',native_session_id=None)
    assert await copilot.prepare_runtime_spec(spec,conv,{}) is spec
    (Path(spec.env['COPILOT_HOME'])/'config.json').write_text('{"oauth_token":"synthetic-secret"}')
    with pytest.raises(UnsupportedCapabilityError,match='系统凭据'):
        await copilot.prepare_runtime_spec(spec,conv,{'mcpServers':[server()]})
    assert not list((tmp_path/'state').rglob('config.json'))


class NativeExtensions:
    def __init__(self, entries=None):self.entries=entries or {};self.calls=[]
    async def call(self,method,params):
        self.calls.append((method,params))
        if method.endswith('/list'):
            return {'extensions':[{'extension':{'type':'mcp','server':value},'extensionKey':name} for name,value in self.entries.items()]+[{'extension':{'type':'platform','name':'developer'},'extensionKey':'platform'}]}
        if method.endswith('/remove'):
            del self.entries[params['extensionKey']];return {}
        raise AssertionError(method)


async def test_goose_new_update_remove_owned_only_and_no_secret_in_ledger(tmp_path):
    spec=spec_for(tmp_path);conv=SimpleNamespace(id='c',native_session_id=None)
    native=NativeExtensions({'native':{'name':'native','command':'user-native'}})
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[server()]})
    native.entries['selected']=server()
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'same-id')
    record=mcp_scope._path(spec,conv)
    assert 'synthetic-secret' not in record.read_text() and record.stat().st_mode & 0o777==0o600
    conv.native_session_id='same-id'
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[server('B')]})
    native.entries['selected']=server('B')
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'same-id')
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[]})
    assert metadata['mcpServers']==[mcp_scope._empty('selected')]
    native.entries['selected']=metadata['mcpServers'][0]
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'same-id')
    assert list(native.entries)==['native']
    assert json.loads(record.read_text())['entries']=={}
    assert [params['extensionKey'] for method,params in native.calls if method.endswith('/remove')]==['selected']


async def test_goose_pending_crash_can_recover_and_foreign_edits_fail_closed(tmp_path):
    spec=spec_for(tmp_path);conv=SimpleNamespace(id='c',native_session_id=None)
    native=NativeExtensions({'selected':server()})
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[server()]})
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'same-id');conv.native_session_id='same-id'
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[]})
    native.entries['selected']=metadata['mcpServers'][0]
    # Crash between native placeholder persistence and removal, then cold retry.
    recovered=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[]})
    await mcp_scope.after_runtime_session(native,spec,conv,recovered,'same-id')
    assert not native.entries
    native.entries['selected']=server()
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[server()]})
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'same-id')
    native.entries['selected']=server('external-edit')
    with pytest.raises(UnsupportedCapabilityError,match='外部修改'):
        await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[]})


async def test_goose_foreign_replacement_before_remove_is_not_deleted(tmp_path):
    spec=spec_for(tmp_path);conv=SimpleNamespace(id='c',native_session_id=None)
    native=NativeExtensions({'selected':server()})
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[server()]})
    await mcp_scope.after_runtime_session(native,spec,conv,metadata,'sid');conv.native_session_id='sid'
    metadata=await mcp_scope.before_runtime_session(native,spec,conv,{'mcpServers':[]})
    with pytest.raises(UnsupportedCapabilityError,match='来源发生变化'):
        await mcp_scope.after_runtime_session(native,spec,conv,metadata,'sid')
    assert native.entries['selected']==server()
