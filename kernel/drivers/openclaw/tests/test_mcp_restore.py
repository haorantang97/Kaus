from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.base import CreateSessionOptions, UnsupportedCapabilityError
from drivers.openclaw.driver import OpenClawAcpDriver
from app.capabilities.models import EffectiveCapability, EffectiveCapabilities


def driver():
    return OpenClawAcpDriver(AcpAgentSpec(command=('openclaw','acp')), backend_key='openclaw', preset=get_preset('openclaw'))


@pytest.mark.parametrize('resume', [False, True])
async def test_session_mcp_never_disappears_on_native_new_or_cold_load(resume):
    agent=driver();connection=SimpleNamespace(call=AsyncMock())
    metadata={'mcpServers':[{'name':'synthetic','command':'synthetic-never-run','args':[],'env':[]}]}
    with pytest.raises(UnsupportedCapabilityError,match='不支持会话级 MCP'):
        if resume:
            await agent._resume_session(connection,'existing-session','/tmp',metadata=metadata)
        else:
            await agent._new_session(connection,CreateSessionOptions(workspace_root='/tmp',metadata=metadata))
    connection.call.assert_not_awaited()


async def test_project_mcp_is_scoped_before_native_acp_projection(monkeypatch):
    from app.projects.models import AgentBinding
    from app.conversations.models import Conversation
    from drivers.base import SessionProjection
    from drivers.openclaw import skill_adapter
    agent=driver()
    binding=AgentBinding.create(project='p',backend='openclaw');agent.register_binding(binding)
    conversation=Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title='fixture')
    metadata={'mcpServers':[{'name':'probe','command':'synthetic-never-run'}]}
    base=AsyncMock(return_value=SessionProjection(options=CreateSessionOptions(metadata=metadata),summary={'mcpServers':['probe']}))
    monkeypatch.setattr(AcpDriver,'session_options_for',base)
    plan=SimpleNamespace(agent_id='kaus-owned',metadata={'sessionKey':'agent:kaus-owned:kaus'})
    project=AsyncMock(return_value=plan);monkeypatch.setattr(skill_adapter,'project_session',project)
    effective=EffectiveCapabilities(project_id=binding.project_id,entries=(EffectiveCapability(capability_type='mcp',capability_id='probe',source_project_id=binding.project_id,inherited=False,config={'command':'synthetic-never-run'}),))
    result=await agent.session_options_for(conversation,effective)
    project.assert_awaited_once()
    assert result.options.metadata=={'_meta':plan.metadata}
    assert result.summary=={'mcpServers':['probe']}
    assert agent._skill_plans[conversation.id] is plan


@pytest.mark.parametrize('engine',['antigravity'])
async def test_unverified_terminal_resume_has_no_guessed_session_id_command(engine):
    preset=get_preset(engine)
    agent=AcpDriver(AcpAgentSpec(command=('synthetic-never-run',)),backend_key=engine,preset=preset)
    assert preset.resume_argv_template is None
    with pytest.raises(UnsupportedCapabilityError,match='暂不支持终端续接'):
        await agent.build_external_cli_launch(SimpleNamespace(id='conversation',native_session_id='original-native-id'))
