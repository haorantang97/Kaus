import json
from copy import deepcopy
from types import SimpleNamespace

from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.translator import AcpTranslationContext, AcpTranslator
from drivers.session_mcp.display import filter_tool_title
from drivers.skill_execution import pi


def test_display_metadata_only_changes_known_mcp_titles(tmp_path):
    config=tmp_path/'private.json'
    title='mcp__fixture__ping_123456789a'
    spec=AcpAgentSpec(command=('unused',),env={'KAUS_SESSION_MCP_CONFIG':str(config)},inherit_env=False)
    catalog=tmp_path/'private.json.titles.json'
    catalog.write_text(json.dumps({'schema':1,'titles':{title:'Check connection'}}))
    assert pi.filter_tool_title(spec,title)=='Check connection'
    assert filter_tool_title(spec,'read_file')=='read_file'
    assert filter_tool_title(AcpAgentSpec(command=('unused',)),title)==title
    catalog.unlink();catalog.symlink_to(tmp_path/'elsewhere')
    assert filter_tool_title(spec,title)=='MCP'


async def test_common_driver_maps_only_tool_and_permission_display_fields(tmp_path):
    title='mcp__fixture__ping_123456789a'
    config=tmp_path/'private.json'
    (tmp_path/'private.json.titles.json').write_text(json.dumps({'schema':1,'titles':{title:'Check connection'}}))
    spec=AcpAgentSpec(command=('unused',),env={'KAUS_SESSION_MCP_CONFIG':str(config)},inherit_env=False)
    context=AcpTranslationContext(project_id='project:test',conversation_id='conversation:test',
        agent_binding_id='binding:test:pi',backend_id='backend:pi',session_id='native',run_token='fixed')
    translator=AcpTranslator(context);translator.begin_run()
    state=SimpleNamespace(runtime_spec=spec,session_id='native',closed=False,client_interactions=None,
        translator=translator,interrupt_requested=False,prompt_task=None,approval_mode='host:ask',skill_plan=None)
    driver=AcpDriver(spec)
    driver._runtime_adapter=lambda:pi
    driver._skill_adapter=lambda:None
    events=[];driver._push=lambda state,rows:events.extend(row.event for row in rows)
    params={'sessionId':'native','update':{'sessionUpdate':'tool_call','toolCallId':'call','title':title,
        'toolName':title,'rawInput':{'nativeName':title}}}
    original=deepcopy(params)
    driver._on_notification(state,'session/update',params)
    started=next(event for event in events if event.type=='tool.started')
    assert started.name=='Check connection' and started.input=={'nativeName':title}
    assert params==original
    permission={'sessionId':'native','toolCall':deepcopy(params['update']),
        'options':[{'optionId':'allow','name':'Allow once','kind':'allow_once'}]}
    original=deepcopy(permission)
    await driver._on_request(state,10,'session/request_permission',permission)
    assert next(event for event in events if event.type=='permission.requested').request.title=='Check connection'
    assert permission==original
    driver._on_notification(state,'session/update',{'sessionId':'native','update':{
        'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':title}}})
    assert any(getattr(event,'text',None)==title for event in events)
