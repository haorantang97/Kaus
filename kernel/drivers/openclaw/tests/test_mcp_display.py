from copy import deepcopy
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.acp.translator import AcpTranslationContext, AcpTranslator
from drivers.openclaw.driver import OpenClawAcpDriver
from drivers.openclaw.skill_adapter import ScopeGateway


def setup():
    owner = 'kaus-' + 'a' * 24
    names = {owner + '-0123456789ab': 'Research'}
    context = AcpTranslationContext(project_id='project:test', conversation_id='conversation:test',
        agent_binding_id='binding:test:openclaw', backend_id='backend:openclaw', session_id='native', run_token='fixed')
    translator = AcpTranslator(context); translator.begin_run()
    state = SimpleNamespace(skill_plan=SimpleNamespace(agent_id=owner,mcp_display_names=names),
        session_id='native',closed=False,client_interactions=None,translator=translator,
        interrupt_requested=False,prompt_task=None,approval_mode='host:ask')
    driver = OpenClawAcpDriver(AcpAgentSpec(command=('unused','acp')),backend_key='openclaw')
    events=[];driver._push=lambda state, rows:events.extend(row.event for row in rows)
    return driver,state,events,owner


async def test_titles_are_readable_without_changing_native_invocations_or_body():
    driver,state,events,owner=setup()
    title=owner+'-__echo: text: synthetic input'
    update={'sessionId':'native','update':{'sessionUpdate':'tool_call','toolCallId':'call',
        'title':title,'toolName':owner+'-__echo','rawInput':{'text':'synthetic input'}}}
    original=deepcopy(update)
    driver._on_notification(state,'session/update',update)
    started=next(event for event in events if event.type=='tool.started')
    assert started.name=='Research · echo'
    assert started.input=={'text':'synthetic input'} and update==original
    permission={'sessionId':'native','toolCall':deepcopy(update['update']),
        'options':[{'optionId':'allow','name':'Allow once','kind':'allow_once'}]}
    original_permission=deepcopy(permission)
    await driver._on_request(state,11,'session/request_permission',permission)
    interaction=next(event for event in events if event.type=='permission.requested')
    assert interaction.request.title=='Research · echo'
    assert permission==original_permission
    driver._on_notification(state,'session/update',{'sessionId':'native',
        'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':title}}})
    assert any(getattr(event,'text',None)==title for event in events)


def test_ambiguous_shortened_name_uses_mcp_and_native_services_keep_their_title():
    driver,state,_,owner=setup()
    state.skill_plan.mcp_display_names[owner+'-abcdef012345']='Finance'
    assert driver._display_tool_call(state,{'title':owner+'-__echo'})['title']=='MCP · echo'
    assert driver._display_tool_call(state,{'title':owner+'__echo'})['title']=='MCP · echo'
    assert driver._display_tool_call(state,{'title':owner+'-abcdef012345__echo'})['title']=='Finance · echo'
    title={'title':'native-mail__send','rawInput':{'text':'hello'}}
    assert driver._display_tool_call(state,title) is title


def test_scope_patch_preserves_unrelated_native_mcp_overrides():
    gateway=ScopeGateway(AcpAgentSpec(command=('unused','acp')),'kaus-owned')
    before={'mcpServers':{'native-on':True,'native-off':False},'tools':{'read':True}}
    params={'key':'agent:kaus-owned:kaus','expectedSessionId':'session',
        'expectedToolOverrides':before,'toolOverrides':{**before,'mcpServers':{**before['mcpServers'],'kaus-owned-server':True}}}
    gateway.validate_session_patch(params)
    params['toolOverrides']['mcpServers']['native-on']=False
    with pytest.raises(ValueError):gateway.validate_session_patch(params)


def test_source_session_mcp_titles_use_owned_namespace_not_source_identity():
    driver, state, _, _ = setup()
    namespace = 'kaus-source-' + 'b' * 24
    state.skill_plan.agent_id = 'user'
    state.skill_plan.mcp_namespace = namespace
    state.skill_plan.mcp_display_names = {namespace + '-0123456789ab': 'Browser'}
    assert driver._display_tool_call(state, {'title': namespace + '-__click'})['title'] == 'Browser · click'
    unchanged = {'title': 'user-native__tool'}
    assert driver._display_tool_call(state, unchanged) is unchanged
