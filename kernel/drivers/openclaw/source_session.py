"""Kaus-owned sessions under an unchanged native OpenClaw Agent.

Computer-use ownership must not replace the Agent's identity, private skills,
memory, or executor configuration. The receipt authorizes cancellation of one
exact session on one connection; it contains no native configuration.
"""
from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import os
import re
import secrets
import stat

from drivers.base import UnsupportedCapabilityError
from drivers.acp.client import AcpRpcError, AcpTransportError
from drivers.skill_execution.common import materialize, state_root
from .computer_use import _explicit_policy, _option, source_agent_id
from .driver import OpenClawGateway


_KEY = re.compile(r"^agent:[A-Za-z0-9_-]+:kaus-[a-f0-9]{24}$")
_AGENT = re.compile(r"^[A-Za-z0-9_-]+$")


class SourceSessionGateway(OpenClawGateway):
    allowed_methods = frozenset({"config.get", "sessions.describe"})


def _connection_id(spec):
    # Hash rather than persist startup arguments or environment values.
    encoded = json.dumps([list(spec.command), sorted(spec.env.items())], sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _receipt_path(spec, key):
    digest = hashlib.sha256((_connection_id(spec) + ":" + key).encode()).hexdigest()
    return state_root(spec) / "openclaw-source-sessions" / (digest + ".json")


def _read_receipt(path):
    try:
        if path.parent.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_uid != os.getuid():
                return None
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def owns_session(spec, key, ownership):
    if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(ownership, dict):
        return False
    receipt = _read_receipt(_receipt_path(spec, key))
    return bool(receipt and receipt.get("schema") == 1 and
        receipt.get("connectionId") == _connection_id(spec) and
        receipt.get("sessionKey") == key == ownership.get("sessionKey") and
        isinstance(ownership.get("receipt"), str) and
        isinstance(receipt.get("receipt"), str) and
        hmac.compare_digest(receipt["receipt"], ownership["receipt"]))


def check_native_controls(config, source, controls):
    for capability, tool in (("browser", "browser"), ("desktop", "computer")):
        if controls.get(capability) is not False:
            continue
        disabled = _explicit_policy(config, source, tool) is False
        if capability == "browser":
            plugins = config.get("plugins", {})
            disabled |= (config.get("browser", {}).get("enabled") is False or
                         plugins.get("enabled") is False or
                         plugins.get("entries", {}).get("browser", {}).get("enabled") is False)
        if not disabled:
            raise UnsupportedCapabilityError("原生 Agent 无法按会话关闭电脑工具，请调整原生工具权限")


async def prepare(spec, binding, effective, conversation, controls):
    gateway = SourceSessionGateway(spec, timeout=5)
    try:
        snapshot = await gateway.call("config.get", {})
        config = snapshot.get("config")
        if not isinstance(config, dict):
            if any(value is False for value in controls.values()):
                raise UnsupportedCapabilityError("无法核验 OpenClaw 原生 Agent")
            await gateway.close()
            return None
        source = source_agent_id(config, spec, binding)
        if not isinstance(source, str) or not _AGENT.fullmatch(source):
            if any(value is False for value in controls.values()):
                raise UnsupportedCapabilityError("无法确定 OpenClaw 原生 Agent")
            await gateway.close()
            return None
        check_native_controls(config, source, controls)
        if _option(spec, "--session") or _option(spec, "--session-label"):
            # A fixed launch selector already refers to a native conversation.
            # Computer-use discovery must not silently redirect it.
            if _option(spec, "--session-label") and any(value is False for value in controls.values()):
                raise UnsupportedCapabilityError("无法核验指定原生会话的电脑工具权限")
            await gateway.close()
            return None
        identity = hashlib.sha256((binding.id + ":" + conversation.id).encode()).hexdigest()[:24]
        key = f"agent:{source}:kaus-{identity}"
        path = _receipt_path(spec, key)
        expected = {"schema": 1, "connectionId": _connection_id(spec), "bindingId": binding.id,
                    "conversationId": conversation.id, "sourceAgentId": source, "sessionKey": key}
        receipt = _read_receipt(path)
        if path.exists() or path.is_symlink():
            if (not receipt or not isinstance(receipt.get("receipt"), str) or
                    any(receipt.get(name) != value for name, value in expected.items())):
                raise UnsupportedCapabilityError("原生会话归属不匹配")
        else:
            if conversation.native_session_id:
                # Preserve an older conversation's existing route and context.
                await gateway.close()
                return None
            description = await gateway.call("sessions.describe", {"key": key})
            if description.get("session") is not None:
                raise UnsupportedCapabilityError("原生会话标识已被占用")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.parent.is_symlink():
                raise UnsupportedCapabilityError("原生会话归属目录不可用")
            receipt = {**expected, "receipt": secrets.token_hex(32)}
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(receipt, stream)
            except FileExistsError:
                receipt = _read_receipt(path)
                if not receipt or any(receipt.get(name) != value for name, value in expected.items()):
                    raise UnsupportedCapabilityError("原生会话归属不匹配") from None
        plan = materialize(spec, binding, effective, layout="roots", dry_run=False)
        plan.source_session = True
        plan.spec, plan.gateway, plan.agent_id = spec, gateway, source
        plan.session_key, plan.metadata = key, {"sessionKey": key}
        plan.ownership = {"sessionKey": key, "receipt": receipt["receipt"]}
        plan.manage_skills = False
        plan.runtime_mcp_servers = ()
        plan.mcp_overrides, plan.mcp_config = {}, {}
        plan.computer_controls = dict(controls)
        return plan
    except (AcpRpcError, AcpTransportError, OSError, asyncio.TimeoutError):
        await gateway.close()
        if any(value is False for value in controls.values()):
            raise UnsupportedCapabilityError("无法核验原生电脑工具权限") from None
        # Native ACP may remain usable when operator config/status scopes are
        # unavailable. Preserve its original session; cancellation is read-only.
        return None
    except BaseException:
        await gateway.close()
        raise


async def validate(plan):
    if not owns_session(plan.spec, plan.session_key, plan.ownership):
        raise UnsupportedCapabilityError("原生会话归属不匹配")
    snapshot = await plan.gateway.call("config.get", {})
    config = snapshot.get("config")
    if not isinstance(config, dict) or plan.agent_id not in config.get("agents", {}).get("entries", {}):
        raise UnsupportedCapabilityError("OpenClaw 原生 Agent 不可用")
    check_native_controls(config, plan.agent_id, plan.computer_controls)

    from .source_mcp import validate as validate_mcp
    await validate_mcp(plan, snapshot)
