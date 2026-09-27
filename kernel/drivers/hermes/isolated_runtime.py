"""Use the existing ACP driver when the HTTP engine needs isolated execution.

Only transport selection and model-id mapping live here. Streaming, tools,
approvals, cancellation and session lifetime stay in the shared ACP driver.
The source profile and its own credentials are retained.
"""
from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.base import UnsupportedCapabilityError
from drivers.hermes.group_execution import supported


def available(command):
    return supported(AcpAgentSpec(command=(command, "acp")))


def create_driver(owner, binding):
    preset = get_preset("hermes-acp")
    spec = AcpAgentSpec(command=(owner.hermes_bin, "acp"), env={
        "HERMES_HOME": str(owner.gateway_config_for(binding).hermes_home),
    })
    driver = AcpDriver(spec, backend_key=owner.backend_id, preset=preset)
    driver.register_binding(binding)
    return driver


async def start(driver, binding, conversation, surface, *, session_options=None):
    if conversation.reasoning_mode:
        driver.agent_spec = driver.agent_spec.with_env(KAUS_GROUP_REASONING=conversation.reasoning_mode)
    catalog = await driver.get_model_catalog(binding)
    selected = conversation.model_id
    if selected:
        candidates = [m for m in catalog.models if m.model_id == selected or (
            m.model_id.endswith(":" + selected) and (
                not conversation.provider_id or
                m.model_id.startswith(conversation.provider_id + ":") or
                m.model_id.startswith("custom:" + conversation.provider_id + ":")
            ))]
        if len(candidates) != 1:
            raise UnsupportedCapabilityError("当前 Agent 未提供所选厂家与模型，请刷新模型列表。")
        conversation = conversation.evolve(model_id=candidates[0].model_id)
    return await driver.start_runtime(conversation, surface, session_options=session_options)
