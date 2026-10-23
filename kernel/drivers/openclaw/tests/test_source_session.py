import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.openclaw import computer_use, source_session


@pytest.fixture
def scope(tmp_path, monkeypatch):
    spec = AcpAgentSpec(command=("unused","acp"),cwd=str(tmp_path),inherit_env=False,
                        env={"KAUS_SKILL_STATE_ROOT":str(tmp_path/"state")})
    binding = AgentBinding.create(project="fixture",backend="openclaw")
    conversation = Conversation.create(project_id=binding.project_id,agent_binding_id=binding.id,title="fixture")
    config = {"agents":{"entries":{"source":{"default":True,"skills":["private"],"agentDir":"/original",
                                            "tools":{"alsoAllow":["computer"]}}}}}
    gateway = AsyncMock()
    async def call(method, params):
        if method=="config.get":return {"config":copy.deepcopy(config)}
        if method=="sessions.describe":return {"session":None}
        raise AssertionError(method)
    gateway.call.side_effect=call
    monkeypatch.setattr(source_session,"SourceSessionGateway",lambda *_args,**_kwargs:gateway)
    effective=EffectiveCapabilities(project_id=binding.project_id)
    return spec,binding,conversation,effective,config,gateway


async def test_receipt_binds_exact_connection_key_and_survives_resume(scope):
    spec,binding,conv,effective,config,gateway=scope
    original=copy.deepcopy(config)
    plan=await source_session.prepare(spec,binding,effective,conv,{"browser":True,"desktop":True})
    assert source_session.owns_session(spec,plan.session_key,plan.ownership)
    assert not source_session.owns_session(spec,plan.session_key,{**plan.ownership,"receipt":"wrong"})
    assert not source_session.owns_session(spec,plan.session_key+"other",plan.ownership)
    assert not source_session.owns_session(spec.with_env(OPENCLAW_GATEWAY_URL="ws://other"),plan.session_key,plan.ownership)
    resumed=await source_session.prepare(spec,binding,effective,conv.model_copy(update={"native_session_id":"uuid"}),
                                         {"browser":True,"desktop":True})
    assert resumed.session_key==plan.session_key and resumed.ownership==plan.ownership
    assert config==original
    assert source_session._receipt_path(spec,plan.session_key).stat().st_mode & 0o777==0o600
    assert not {"config.patch","sessions.patch"} & {args[0] for args,_ in gateway.call.call_args_list}


async def test_old_session_is_not_rebound(scope):
    spec,binding,conv,effective,config,gateway=scope
    plan=await source_session.prepare(spec,binding,effective,conv.model_copy(update={"native_session_id":"original"}),
                                     {"browser":True,"desktop":True})
    assert plan is None
    assert [args[0] for args,_ in gateway.call.call_args_list]==["config.get"]
    assert not list(source_session.state_root(spec).glob("openclaw-source-sessions/*"))


@pytest.mark.parametrize("selector", [("--session","agent:source:main"),("--session-label","my-work")])
async def test_fixed_native_launch_route_is_not_rebound(scope,selector):
    spec,binding,conv,effective,config,gateway=scope
    spec=spec.model_copy(update={"command":(*spec.command,*selector)})
    assert await source_session.prepare(spec,binding,effective,conv,{}) is None
    assert [args[0] for args,_ in gateway.call.call_args_list]==["config.get"]


async def test_unclaimed_existing_session_is_not_adopted(scope):
    spec,binding,conv,effective,config,gateway=scope
    async def call(method,params):
        return {"config":config} if method=="config.get" else {"session":{"key":params["key"],"status":"done"}}
    gateway.call.side_effect=call
    with pytest.raises(UnsupportedCapabilityError,match="已被占用"):
        await source_session.prepare(spec,binding,effective,conv,{})


async def test_owned_source_stop_requires_receipt(scope, monkeypatch):
    spec,binding,conv,effective,config,gateway=scope
    plan=await source_session.prepare(spec,binding,effective,conv,{})
    stopper=AsyncMock()
    async def stop(method,params):
        if method=="sessions.abort":return {"ok":True}
        if method=="sessions.describe":return {"session":{"key":plan.session_key,"status":"done"}}
        raise AssertionError(method)
    stopper.call.side_effect=stop
    monkeypatch.setattr(computer_use,"StopGateway",lambda *_args,**_kwargs:stopper)
    metadata={"_meta":plan.metadata,"_kausOpenClawOwnership":plan.ownership}
    assert await computer_use.stop_computer_use(spec,"acp-uuid",metadata)
    assert stopper.call.call_args_list[0].args[0]=="sessions.abort"
    stopper.reset_mock()
    metadata["_kausOpenClawOwnership"]={"sessionKey":plan.session_key,"receipt":"wrong"}
    assert await computer_use.stop_computer_use(spec,"acp-uuid",metadata)  # Read-only terminal confirmation.
    assert [args[0] for args,_ in stopper.call.call_args_list]==["sessions.describe"]
    stopper.reset_mock()
    assert not await computer_use.abort_computer_use(spec,plan.session_key,metadata["_kausOpenClawOwnership"])
    assert not await computer_use.abort_computer_use(spec,"agent:source:main",plan.ownership)
    changed=plan.session_key[:-1]+("0" if plan.session_key[-1]!="0" else "1")
    assert not await computer_use.abort_computer_use(spec,changed,plan.ownership)
    stopper.call.assert_not_called()


async def test_native_disabled_policy_keeps_source_identity(scope):
    spec,binding,conv,effective,config,gateway=scope
    config["agents"]["entries"]["source"]["tools"]["deny"]=["computer","browser"]
    plan=await source_session.prepare(spec,binding,effective,conv,{"desktop":False,"browser":False})
    assert plan.agent_id=="source"
    await source_session.validate(plan)
    config["agents"]["entries"]["source"]["tools"].pop("deny")
    with pytest.raises(UnsupportedCapabilityError,match="无法按会话关闭"):
        await source_session.validate(plan)


async def test_unavailable_native_status_preserves_ordinary_session(scope):
    from drivers.acp.client import AcpRpcError
    spec,binding,conv,effective,config,gateway=scope
    gateway.call.side_effect=AcpRpcError(-32000,"synthetic missing config permission")
    assert await source_session.prepare(spec,binding,effective,conv,{"browser":True,"desktop":True}) is None
    with pytest.raises(UnsupportedCapabilityError,match="无法核验"):
        await source_session.prepare(spec,binding,effective,conv,{"desktop":False})
