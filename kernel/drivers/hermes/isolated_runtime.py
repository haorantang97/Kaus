"""Use the existing ACP driver when the HTTP engine needs isolated execution.

Only transport selection and model-id mapping live here. Streaming, tools,
approvals, cancellation and session lifetime stay in the shared ACP driver.
The source profile and its own credentials are retained.
"""
import os

from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import get_preset
from drivers.base import UnsupportedCapabilityError
from drivers.hermes.group_execution import supported


def available(command):
    return supported(AcpAgentSpec(command=(command, "acp")))


def create_context_driver(owner, binding):
    spec = AcpAgentSpec(command=(owner.hermes_bin, "acp"), env={
        "HERMES_HOME": str(owner.gateway_config_for(binding).hermes_home),
        "HERMES_ACP_SKIP_CONFIGURED_MCP": "1"})
    driver = AcpDriver(spec, backend_key=owner.backend_id, preset=get_preset("hermes-acp"))
    driver.register_binding(binding)
    return driver


def create_driver(owner, binding, *, skill_scope=None, native_conversation=None):
    from drivers.hermes import project_scope
    from drivers.hermes.skill_adapter import native_binary_path
    preset = get_preset("hermes-acp")
    native_home = owner.gateway_config_for(binding).hermes_home
    home = native_home
    if project_scope.enabled(binding):
        home = project_scope.prepare(owner.hermes_root, home, binding)
    from drivers.hermes.skill_launch import command_for
    from drivers.hermes.skill_snapshots import native_output
    source = owner.gateway_config_for(binding).hermes_home if native_conversation else None
    output = native_output(home, native_conversation) if native_conversation else None
    command = command_for(owner.hermes_bin, home, ("acp",), native_source=source, native_output=output) if project_scope.enabled(binding) else (owner.hermes_bin, "acp")
    spec = AcpAgentSpec(command=command, env={
        "HERMES_HOME": str(home),
        "PATH": native_binary_path(os.environ.get("PATH", ""), native_home, owner.hermes_root),
        **({"HERMES_ACP_SKIP_CONFIGURED_MCP": "1"} if project_scope.enabled(binding) else {}),
        **({"KAUS_HERMES_SKILL_SCOPE": str(skill_scope)} if skill_scope else {}),
        **({"KAUS_HERMES_NATIVE_SKILL_SOURCE": str(source), "KAUS_HERMES_NATIVE_SKILL_OUTPUT": str(output)} if source else {}),
    })
    if project_scope.enabled(binding):
        from drivers.hermes.skill_adapter import guarded_spec
        spec = guarded_spec(spec, [])
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
