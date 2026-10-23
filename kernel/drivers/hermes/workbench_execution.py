"""Private process guard; provider routing and authentication stay native."""
from pathlib import Path
from drivers.base import UnsupportedCapabilityError
from drivers.hermes.group_execution import hermes_python


async def prepare(spec, metadata, runtime_info):
    python = hermes_python(spec)
    if not python:
        raise UnsupportedCapabilityError("请更新 Agent 运行组件后使用创作助手。")
    return spec.model_copy(update={"command": (python, "-B", str(Path(__file__).with_name("workbench_launcher.py"))),
        "env": {**dict(spec.env), "HERMES_ACP_SKIP_CONFIGURED_MCP": "1"}}), {
            "_kausExecutionPolicy": "context-only", "mcpServers": []}
