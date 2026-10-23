"""Expose native Hermes computer tools through the existing ACP child."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sqlite3

import yaml

from drivers.base import UnsupportedCapabilityError
from .group_execution import hermes_python

DESKTOP_ENV = "KAUS_COMPUTER_USE_DESKTOP"
BROWSER_ENV = "KAUS_COMPUTER_USE_BROWSER"
LOCAL_TRANSPORT = "_kausHermesLocalComputerTransport"


def local_transport_spec(owner, binding):
    """A local HTTP ledger can resume through the same engine's ACP process.

    Remote gateways and unconfigured native homes remain on their original
    transport. Never create a second home or infer a session from another DB.
    """
    from drivers.acp.client import AcpAgentSpec
    from .skill_adapter import native_binary_path
    config = owner.gateway_config_for(binding)
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    home = config.hermes_home
    if not (home / "config.yaml").is_file() or not (home / "state.db").is_file():
        return None
    spec = AcpAgentSpec(command=(owner.hermes_bin, "acp"), env={
        "HERMES_HOME": str(home),
        "KAUS_HERMES_HTTP_TRANSPORT": "1",
        "PATH": native_binary_path(os.environ.get("PATH", ""), home, owner.hermes_root),
    })
    return spec if hermes_python(spec) else None


async def local_http_projection(owner, binding, conversation, effective):
    """Project computer controls without changing native Skills or configuration."""
    spec = local_transport_spec(owner, binding)
    if spec is None:
        return None
    from drivers.computer_use.integration import prepare_projection
    from drivers.base import CreateSessionOptions, SessionProjection
    from . import skill_adapter
    metadata = await prepare_projection(spec, skill_adapter, binding, conversation,
                                        effective, {}, supports_mcp=True)
    metadata[LOCAL_TRANSPORT] = True
    if conversation.native_session_id:
        metadata["_kausHermesHttpSession"] = owner.canonical_map_for(binding).head_for(conversation.native_session_id)
    cwd = _http_cwd(Path(spec.env["HERMES_HOME"]), metadata.get("_kausHermesHttpSession"))
    return SessionProjection(options=CreateSessionOptions(workspace_root=cwd, metadata=metadata))


def _http_cwd(home, session_id):
    """Read the selected native metadata without opening a writable ledger."""
    from .http_session_bridge import _saved_cwd
    cwd = None
    if session_id:
        try:
            with sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
                cwd = _saved_cwd({"model_config": row[0]}) if row else None
        except (OSError, sqlite3.Error):
            pass
    if not cwd:
        try:
            config = yaml.safe_load((home / "config.yaml").read_text()) or {}
            terminal = config.get("terminal") or {}
            cwd = terminal.get("cwd")
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            pass
        cwd = cwd or os.environ.get("TERMINAL_CWD")
    if isinstance(cwd, str) and cwd.strip() and cwd != ".":
        return str(Path(cwd).expanduser().absolute())
    return None


def prepare_spec(spec, metadata):
    """Compose with Skill/Group/MCP launchers; never activate a restricted task."""
    metadata = metadata or {}
    decision = metadata.get("_kausComputerUse") or {}
    controls = decision.get("controls") or {}
    context_only = metadata.get("_kausExecutionPolicy") == "context-only"
    values = {}
    if metadata.get(LOCAL_TRANSPORT):
        values["KAUS_HERMES_HTTP_TRANSPORT"] = "1"
        if metadata.get("_kausHermesHttpSession"):
            values["KAUS_HERMES_HTTP_SESSION"] = metadata["_kausHermesHttpSession"]
    for kind, key in (("browser", BROWSER_ENV), ("desktop", DESKTOP_ENV)):
        selection = decision.get(kind) or {}
        if context_only or controls.get(kind) is False or selection.get("status") in {"disabled", "denied"}:
            values[key] = "0"
    selection = decision.get("desktop") or {}
    selected = (selection.get("source") == "native" and selection.get("status") == "ready")
    if DESKTOP_ENV not in values and "desktop" in decision:
        values[DESKTOP_ENV] = "1" if selected else "0"
    browser = decision.get("browser") or {}
    if BROWSER_ENV not in values:
        if browser.get("source") in {"project", "fallback"} and browser.get("status") == "ready":
            values[BROWSER_ENV] = "0"
        elif browser.get("source") == "native":
            # A refreshed explicit native selection may clear an earlier
            # per-session off value; native configuration still owns its tools.
            values[BROWSER_ENV] = "1"
    if values:
        spec = spec.with_env(**values)
    if not spec.env.get("KAUS_HERMES_HTTP_TRANSPORT") and not spec.env.get("KAUS_HERMES_HTTP_SESSION") and not any(spec.env.get(key) in {"0", "1"} for key in (DESKTOP_ENV, BROWSER_ENV)):
        return spec
    command = spec.command
    launcher = str(Path(__file__).with_name("mcp_launcher.py"))
    if len(command) > 2 and command[1:3] == ("-B", launcher):
        return spec
    owned = {str(Path(__file__).with_name(name)) for name in ("skill_launcher.py", "group_launcher.py")}
    if len(command) > 2 and command[1] == "-B" and command[2] in owned:
        python, target, args = command[0], command[2], command[3:]
    elif command and Path(command[0]).name in {"hermes", "hermes.exe"}:
        python, target, args = hermes_python(spec), "native", command[1:]
    else:
        raise UnsupportedCapabilityError("此 Hermes 启动器尚未适配电脑操作")
    if not python:
        raise UnsupportedCapabilityError("无法找到 Hermes 运行环境")
    return spec.model_copy(update={
        "command": (python, "-B", launcher, target, *args),
        "env": dict(spec.env),
    })


def _unavailable(code, status="unknown"):
    return {kind: {"status": status, "requirements": [code],
                   "provider": "hermes-native", "selected_tool_enabled": False,
                   "exposed": kind == "browser" and status == "unknown"}
            for kind in ("browser", "desktop")}


async def probe_native(spec):
    """Run read-only native dependency checks; never install or drive a surface."""
    python = hermes_python(spec)
    if not python:
        return _unavailable("hermes_runtime_missing", "unavailable")
    command = (python, "-B", str(Path(__file__).with_name("computer_use_probe.py")))
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=spec.build_env(), cwd=spec.cwd,
            start_new_session=os.name == "posix",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return _unavailable("native_probe_unavailable")
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=12)
        result = json.loads(output)
        if process.returncode or not isinstance(result, dict):
            raise ValueError()
        return {kind: result[kind] for kind in ("browser", "desktop")}
    except asyncio.CancelledError:
        raise
    except (ValueError, KeyError, asyncio.TimeoutError):
        return _unavailable("native_probe_incomplete")
    finally:
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
