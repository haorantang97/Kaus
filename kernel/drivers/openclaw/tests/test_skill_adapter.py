import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.capabilities.models import EffectiveCapabilities,EffectiveCapability
from app.projects.models import AgentBinding
from app.conversations.models import Conversation
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.openclaw import skill_adapter as adapter
from drivers.openclaw.driver import OpenClawAcpDriver


def setup(tmp_path):
    spec=AcpAgentSpec(command=('unused','acp'),cwd=str(tmp_path),inherit_env=False,env={'KAUS_SKILL_STATE_ROOT':str(tmp_path/'state')})
    binding=AgentBinding.create(project='fixture',backend='openclaw')
    conversation=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture')
    source=tmp_path/'source/chosen/SKILL.md';source.parent.mkdir(parents=True);source.write_text('---\nname: chosen\ndescription: fixture\n---\nCHOSEN\n')
    effective=EffectiveCapabilities(project_id=binding.project_id,entries=(EffectiveCapability(capability_type='skill-selection',capability_id='default',source_project_id=binding.project_id,inherited=False,config={'assets':[{'capabilityType':'skills','capabilityId':'chosen','name':'chosen','sourcePath':str(source)}]}),))
    return spec,binding,conversation,effective


class Gateway:
    config={'agents':{'ownership':'explicit','entries':{}}}
    patches=[]
    def __init__(self,spec,agent_id):self.agent_id=agent_id
    async def close(self): pass
    async def call(self,method,params):
        if method=='config.get':return {'config':json.loads(json.dumps(self.config)),'hash':str(len(self.patches))}
        if method=='config.patch':
            assert params['baseHash']==str(len(self.patches))
            patch=json.loads(params['raw']);assert set(patch['agents']['entries'])=={self.agent_id}
            self.patches.append(params)
            entry=patch['agents']['entries'][self.agent_id]
            if entry is None:self.config['agents']['entries'].pop(self.agent_id,None)
            else:
                def merge(before,update):
                    if not isinstance(before,dict) or not isinstance(update,dict):return update
                    result=dict(before)
                    for key,value in update.items():
                        if value is None:result.pop(key,None)
                        else:result[key]=merge(result.get(key),value)
                    return result
                self.config['agents']['entries'][self.agent_id]=merge(self.config['agents']['entries'].get(self.agent_id,{}),entry)
            return {'ok':True}
        if method=='sessions.describe':return {'session':None}
        entry=self.config['agents']['entries'][self.agent_id]
        rows=[{'name':name,'filePath':str(Path(entry['agentDir'])/'workshop-skills'/name/'SKILL.md'),'blockedByAgentFilter':False,'eligible':True} for name in entry['skills']]
        rows.append({'name':'native-other','filePath':'/native/other/SKILL.md','blockedByAgentFilter':True,'eligible':True})
        return {'agentSkillFilter':entry['skills'],'skills':rows}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Gateway.config={'agents':{'ownership':'explicit','entries':{'existing':{'workspace':'/user-workspace','skills':['private']}}}}
    Gateway.patches=[]
    monkeypatch.setattr(adapter,'ScopeGateway',Gateway)


async def test_conversation_and_group_scopes_preserve_existing_agents(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    original=json.loads(json.dumps(Gateway.config['agents']['entries']['existing']))
    first=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    other=conversation.model_copy(update={'id':'group-fixture'})
    effective.entries[0].config['assets']=[]
    second=await adapter.project_session(spec,binding,effective,tmp_path,conversation=other)
    assert first.agent_id!=second.agent_id
    assert second.names==() and Gateway.config['agents']['entries'][second.agent_id]['skills']==[]
    assert Gateway.config['agents']['entries']['existing']==original
    await adapter.validate_turn(first,str(tmp_path))
    assert first.metadata=={'sessionKey':f'agent:{first.agent_id}:kaus'}


async def test_project_computer_use_off_overrides_source_grant_only_in_owned_agent(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    source=Gateway.config['agents']['entries']['existing']
    source['tools']={'alsoAllow':['browser','computer'],'deny':['exec']}
    control=EffectiveCapability(capability_type='policies',capability_id='computer_use',
        source_project_id=binding.project_id,inherited=False,config={'value':{'desktop':'off','browser':'auto'}})
    effective=effective.model_copy(update={'entries':(*effective.entries,control)})
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    assert plan.agent_config['tools']['deny']==['exec','computer']
    assert plan.agent_config['tools']['alsoAllow']==['browser','computer']
    assert source['tools']=={'alsoAllow':['browser','computer'],'deny':['exec']}


@pytest.mark.parametrize("capability,tool", [('browser','browser'),('desktop','computer')])
async def test_project_off_materializes_even_without_skills_or_mcp(tmp_path, capability, tool):
    spec,binding,conversation,_=setup(tmp_path)
    source=Gateway.config['agents']['entries']['existing']
    source['tools']={'alsoAllow':[tool]}
    effective=EffectiveCapabilities(project_id=binding.project_id,entries=(
        EffectiveCapability(capability_type='policies',capability_id='computer_use',
            source_project_id=binding.project_id,inherited=False,config={capability:'off'}),
    ))
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    assert plan.agent_config['tools']=={'alsoAllow':[tool],'deny':[tool]}
    assert plan.manage_skills is False
    assert source['tools']=={'alsoAllow':[tool]}
    assert capability in adapter.COMPUTER_USE_POLICY_SCOPES


async def test_existing_native_session_and_legacy_owner_are_never_changed(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    conversation=conversation.model_copy(update={'native_session_id':'native-session'})
    with pytest.raises(UnsupportedCapabilityError,match='已有原生'):await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    conversation=conversation.model_copy(update={'native_session_id':None})
    Gateway.config['agents'].pop('ownership')
    with pytest.raises(UnsupportedCapabilityError,match='所有权'):await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    assert Gateway.patches==[]


async def test_scope_drift_is_rejected_and_same_owned_agent_cold_resume_is_idempotent(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    first=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    resumed=conversation.model_copy(update={'native_session_id':'native-session'})
    second=await adapter.project_session(spec,binding,effective,tmp_path,conversation=resumed)
    assert first.session_key==second.session_key and len(Gateway.patches)==1
    Gateway.config['agents']['entries'][first.agent_id]['skills'].append('other')
    with pytest.raises(UnsupportedCapabilityError):await adapter.validate_turn(first,str(tmp_path))


async def test_gateway_model_and_permission_controls_keep_managed_route(tmp_path):
    spec,_,_,_=setup(tmp_path);driver=OpenClawAcpDriver(spec,backend_key='openclaw')
    driver._bind_session_route('acp-a',{'sessionKey':'agent:kaus-a:kaus'})
    driver._bind_session_route('acp-b',{'sessionKey':'agent:kaus-b:kaus'})
    assert driver._gateway_for('acp-a').options['--session']=='agent:kaus-a:kaus'
    assert driver._gateway_for('acp-b').options['--session']=='agent:kaus-b:kaus'
    assert '--session' not in driver.gateway.options


async def test_outside_edit_is_not_overwritten_on_rebuild(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    Gateway.config['agents']['entries'][plan.agent_id]={'workspace':'/external-change'}
    count=len(Gateway.patches)
    with pytest.raises(UnsupportedCapabilityError,match='外部修改'):
        await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    assert Gateway.config['agents']['entries'][plan.agent_id]=={'workspace':'/external-change'}
    assert len(Gateway.patches)==count


async def test_source_computer_policy_survives_managed_project_scope(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    source=Gateway.config['agents']['entries']['existing']
    source.update(tools={'profile':'coding','alsoAllow':['browser','computer'],
                         'deny':['dangerous-tool'],'exec':{'host':'private'},
                         'sandbox':{'tools':{'alsoAllow':['computer']}}},
                  agentDir='/private/agent',env={'TOKEN':'private-secret'})
    before=json.loads(json.dumps(Gateway.config))
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    current=Gateway.config['agents']['entries'][plan.agent_id]
    assert current['tools']=={'profile':'coding','alsoAllow':['browser','computer'],
                             'deny':['dangerous-tool'],'sandbox':{'tools':{'alsoAllow':['computer']}}}
    assert current['skills']==list(plan.names) and len(current['skills'])==1
    assert current['workspace']==str(tmp_path) and current['agentDir']!='/private/agent'
    assert 'private-secret' not in json.dumps(current)
    assert Gateway.config['agents']['entries']['existing']==before['agents']['entries']['existing']
    resumed=conversation.model_copy(update={'native_session_id':'native-session'})
    again=await adapter.project_session(spec,binding,effective,tmp_path,conversation=resumed)
    assert again.agent_config==current and len(Gateway.patches)==1


async def test_changed_source_tool_policy_replaces_only_owned_copy(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    source=Gateway.config['agents']['entries']['existing']
    source['tools']={'profile':'coding','alsoAllow':['computer']}
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    source['tools']={'profile':'coding','deny':['computer']}
    updated=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    assert updated.agent_id==plan.agent_id
    assert updated.agent_config['tools']['deny']==['computer']
    # config.patch explicitly removes old fields during a native deep merge.
    assert updated.agent_config['tools'].get('alsoAllow') is None
    assert source['tools']=={'profile':'coding','deny':['computer']}


async def test_runtime_preparation_keeps_existing_skill_selection_and_metadata(tmp_path):
    spec,binding,conversation,effective=setup(tmp_path)
    plan=await adapter.project_session(spec,binding,effective,tmp_path,conversation=conversation)
    plans={conversation.id:plan}
    adapter.remember_runtime_context(conversation,binding,effective,plans,str(tmp_path))
    metadata={'_meta':{**plan.metadata,'fixture':'retained'},'mcpServers':[],
              '_kausComputerUse':{'controls':{'desktop':False},'resources':[]}}
    await adapter.prepare_runtime_spec(spec,conversation,metadata)
    assert plans[conversation.id].names==plan.names
    assert plans[conversation.id].agent_config['skills']==plan.agent_config['skills']
    assert plans[conversation.id].agent_config['tools']['deny']==['computer']
    assert metadata['_meta']=={**plan.metadata,'fixture':'retained'}
