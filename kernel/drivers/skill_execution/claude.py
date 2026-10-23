"""Claude Agent SDK's native Skill allowlist, including namespaced plugin Skills.

The allowlist is a loader/tool boundary, not a filesystem sandbox. Native homes,
provider routing, login, hooks and non-Skill plugin functionality remain intact.
"""
import asyncio
import re

from drivers.base import UnsupportedCapabilityError
from .common import materialize, unsupported

COMPUTER_USE_TERMINAL_MCP = False


def _version(value):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


async def supported(spec, runtime_info):
    name, version = runtime_info or (None, None)
    if name != "@agentclientprotocol/claude-agent-acp" or _version(version) < (0, 90, 0):
        return False
    override = spec.build_env().get("CLAUDE_CODE_EXECUTABLE")
    if override:
        # Older CLI binaries silently ignore initialize.skills even when a new
        # SDK sends it. An explicit native binary must meet the verified floor.
        try:
            process = await asyncio.create_subprocess_exec(override, "--version", env=spec.build_env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return False
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
            return process.returncode == 0 and _version(stdout.decode()) >= (2, 1, 296)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False
    return True


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    if not await supported(spec, runtime_info):
        return unsupported(binding, "当前适配器或原生版本未提供已验证的技能白名单", dry_run=dry_run)
    plan = materialize(spec, binding, effective, layout="plugin", dry_run=dry_run)
    if plan.root is not None:
        plan.metadata = {"claudeCode": {"options": {
            "plugins": [{"type": "local", "path": str(plan.root)}],
            "skills": ["kaus-project:" + name for name in plan.names],
        }}}
    plan.spec = spec
    return plan


def cli_command(plan, command):
    # The interactive CLI currently has no equivalent of SDK initialize.skills.
    # --disable-slash-commands would also break the allowed set.
    raise UnsupportedCapabilityError("当前原生终端不支持项目技能白名单，请在对话页面继续")


async def load_native_history(spec, binding, native_session_id):
    from drivers.claude_code.native_history import load_native_history as load
    return await load(spec, binding, native_session_id)


async def load_local_native_history(spec, binding, native_session_id):
    return await load_native_history(spec, binding, native_session_id)
