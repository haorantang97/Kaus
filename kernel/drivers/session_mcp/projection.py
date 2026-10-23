"""Original per-conversation native extension projection, without global edits."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sys

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution.terminal_settings import immutable
from drivers.skill_execution import bridge_terminal

CONFIG_ENV = "KAUS_SESSION_MCP_CONFIG"
RECEIPT_ENV = "KAUS_SESSION_MCP_RECEIPT"
NONCE_ENV = "KAUS_SESSION_MCP_NONCE"
BASE_ENV = "KAUS_SESSION_MCP_NATIVE"
# Command Code 1.66.0; only this installed, exercised source is transformed.
COMMANDCODE_TUI_VERIFIED = {'adf05d4e64358d631e4627e7cfaee23ac7903690ca44fe5b0e715f8b814b95ca'}


def _terminal_input_script(spec, conversation):
    from drivers.skill_execution.node_package import locate
    argv = list(spec.command)
    native = argv[argv.index('--command') + 1] if '--command' in argv else shutil.which('command-code', path=spec.build_env().get('PATH'))
    package = locate(spec.model_copy(update={'command': (native,)}), 'command-code') if native else None
    try:
        entry = (package / 'dist/cli.mjs').resolve() if package else None
        source = entry.read_bytes() if entry else b''
        digest = hashlib.sha256(source).hexdigest()
        if digest not in COMMANDCODE_TUI_VERIFIED:
            raise ValueError()
    except (OSError, ValueError):
        raise UnsupportedCapabilityError('当前原生终端版本尚未兼容，请更新引擎或适配器') from None
    manifest = immutable(spec, conversation, json.dumps({'path': str(entry), 'hash': digest}), suffix='.commandcode-tui.json')
    gate = immutable(spec, conversation, Path(__file__).with_name('commandcode_tui_compat.mjs').read_text(), suffix='.mjs')
    return ('export KAUS_COMMANDCODE_TUI_COMPAT=' + shlex.quote(str(manifest)) + '\n'
        + 'export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }"'
        + shlex.quote('--import=' + gate.as_uri()) + '\n')


def _terminal_ready(spec, conversation, command):
    """Even empty-MCP terminals must finish native resume before accepting input."""
    script = '#!/bin/sh\n' + _terminal_input_script(spec, conversation)
    script += 'exec ' + shlex.quote(command[0]) + ' "$@"\n'
    launcher = immutable(spec, conversation, script, suffix=".sh")
    launcher.chmod(0o700)
    return (str(launcher), *command[1:])


def _servers(metadata):
    rows = metadata.get("mcpServers", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UnsupportedCapabilityError("MCP 配置格式无效")
    return rows


def remember(spec, conversation, metadata):
    bridge_terminal.remember(spec, conversation, metadata)


def _config(spec, conversation, metadata):
    # The private JSON is configuration, never a shell command. Values are not
    # printed in errors or copied into the native transcript / launcher text.
    return immutable(spec, conversation, json.dumps({"schema": 1,
        "servers": _servers(metadata),
        "python": sys.executable, "broker": str(Path(__file__).with_name("broker.py"))}, sort_keys=True), suffix=".mcp.json")


def _launcher(spec, conversation, native, config, *, kind, terminal=False):
    source = Path(__file__).with_name("extension.mjs").read_text()
    extension = immutable(spec, conversation, source, suffix=".mjs")
    argv = [native, "--extension" if kind == "pi" else "--mod", str(extension)]
    script = "#!/bin/sh\nexport " + CONFIG_ENV + "=" + shlex.quote(str(config)) + "\n"
    if terminal and kind == "commandcode":
        script += _terminal_input_script(spec, conversation)
    script += "exec " + shlex.join(argv) + ' "$@"\n'
    launcher = immutable(spec, conversation, script, suffix=".sh")
    launcher.chmod(0o700)
    return str(launcher)


def restore_native_spec(spec, *, kind):
    """Remove only our MCP wrapper, retaining the selected native Skill loader."""
    prior = spec.env.get(BASE_ENV)
    if prior:
        try:
            original = json.loads(prior)
            command = original['command']
            native = original['native']
            if (not isinstance(command, list) or not command or
                    any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in command) or
                    (native is not None and (not isinstance(native, str) or not native or '\x00' in native))):
                raise ValueError()
            values = dict(spec.env)
            for key in (CONFIG_ENV, RECEIPT_ENV, NONCE_ENV, BASE_ENV):
                values.pop(key, None)
            if kind == "pi":
                if native is None:
                    values.pop("PI_ACP_PI_COMMAND", None)
                else:
                    values["PI_ACP_PI_COMMAND"] = native
            spec = spec.model_copy(update={"env": values, "command": tuple(command)})
        except (ValueError, KeyError, TypeError):
            raise UnsupportedCapabilityError("原生 MCP 启动配置无效") from None
    return spec


async def prepare(spec, conversation, metadata, *, kind):
    spec = restore_native_spec(spec, kind=kind)
    if not _servers(metadata):
        return spec
    env = spec.build_env()
    argv = list(spec.command)
    if kind == "pi":
        native = env.get("PI_ACP_PI_COMMAND") or shutil.which("pi", path=env.get("PATH"))
    else:
        native = argv[argv.index("--command") + 1] if "--command" in argv else shutil.which("command-code", path=env.get("PATH"))
    if not native or not Path(native).is_file():
        raise UnsupportedCapabilityError("原生 MCP 扩展入口不可用")
    config = _config(spec, conversation, metadata)
    launcher = _launcher(spec, conversation, native, config, kind=kind)
    values = {CONFIG_ENV: str(config), BASE_ENV: json.dumps({"command": list(spec.command),
        "native": spec.env.get("PI_ACP_PI_COMMAND")})}
    if kind == "pi":
        nonce = secrets.token_hex(24)
        values.update(PI_ACP_PI_COMMAND=launcher, **{NONCE_ENV: nonce,
            RECEIPT_ENV: str(config.parent / ("mcp-receipt-" + nonce + ".json"))})
    else:
        if "--command" in argv:
            argv[argv.index("--command") + 1] = launcher
        else:
            argv.extend(("--command", launcher))
        spec = spec.model_copy(update={"command": tuple(argv)})
    return spec.with_env(**values)


async def probe(spec):
    config = spec.build_env().get(CONFIG_ENV)
    if not config:
        return
    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("broker.py")), config, "--probe",
        cwd=spec.cwd, env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 35)
        ready = json.loads(stdout.decode().splitlines()[0]) if stdout else {}
        if process.returncode or ready.get("ready") is not True:
            raise ValueError()
    except (OSError, ValueError, IndexError, asyncio.TimeoutError) as exc:
        raise UnsupportedCapabilityError("MCP 连接失败，请检查服务配置") from exc
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def before_session(connection, spec, conversation, metadata, *, kind):
    return {**metadata, "mcpServers": []} if kind == "pi" else metadata


async def verify(spec):
    env = spec.build_env()
    raw = env.get(RECEIPT_ENV)
    if not raw:
        return
    target = Path(raw)
    try:
        for _ in range(100):
            if target.exists():
                if target.is_symlink() or target.stat().st_size > 4096:
                    raise ValueError()
                result = json.loads(target.read_text())
                if result != {"schema": 1, "nonce": env.get(NONCE_ENV), "ok": True}:
                    raise ValueError()
                return
            await asyncio.sleep(.05)
        raise ValueError()
    except (OSError, ValueError, TypeError) as exc:
        raise UnsupportedCapabilityError("原生 MCP 工具未完成注册") from exc
    finally:
        target.unlink(missing_ok=True)


def terminal(spec, conversation, command, *, kind):
    path = bridge_terminal.options_path(spec, conversation)
    if not path.exists():
        return tuple(command)
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        metadata = json.loads(path.read_text())
        if not _servers(metadata):
            return _terminal_ready(spec, conversation, command) if kind == "commandcode" else tuple(command)
    except (OSError, ValueError, TypeError) as exc:
        raise UnsupportedCapabilityError("终端 MCP 配置不可用") from exc
    config = _config(spec, conversation, metadata)
    return (_launcher(spec, conversation, command[0], config, kind=kind, terminal=True), *command[1:])
