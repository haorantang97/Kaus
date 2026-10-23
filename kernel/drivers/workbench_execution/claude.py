"""Agent SDK tool-free session options; never mutate native configuration."""
from drivers.base import UnsupportedCapabilityError


async def prepare(spec, metadata, runtime_info):
    from drivers.skill_execution.claude import supported
    if not await supported(spec, runtime_info):
        raise UnsupportedCapabilityError("请更新 Agent 运行组件后使用创作助手。")
    metadata = {"_kausExecutionPolicy": "context-only", "mcpServers": [], "_meta": {
        "claudeCode": {"options": {
            "tools": [], "skills": [], "plugins": [], "agents": {},
            "settingSources": [], "strictMcpConfig": True, "mcpServers": {},
            "settings": {"disableAllHooks": True}, "hooks": {},
            "promptSuggestions": False,
            "systemPrompt": "Complete the supplied module task using only its supplied context. Return the requested response without accessing external tools.",
        }}}}
    return spec.with_env(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1"), metadata
