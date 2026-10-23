"""Per-session switches for known native MCP configuration; no home edits."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shlex
import shutil


def adapter_name(adapter):
    return getattr(adapter, "__name__", "").rsplit(".", 1)[-1]


def supported(adapter):
    return adapter_name(adapter) in {"codex", "claude"}


def disabled_servers(metadata):
    rows = metadata.get("_kausComputerUse", {}).get("nativeDisabledServers", [])
    return sorted(set(name for name in rows if isinstance(name, str) and name and "\x00" not in name))


def config_flags(names):
    return [part for name in names for part in ("-c", "mcp_servers." + json.dumps(name) + ".enabled=false")]


def prepare_spec(spec, adapter, metadata):
    names = disabled_servers(metadata)
    kind = adapter_name(adapter)
    if kind == "claude" and (names or metadata.get("_kausNativeComputerPolicy")):
        # Verified native ACP SDK path for new and resumed sessions. This removes
        # these exact servers from the model and harness-internal tool routing.
        from copy import deepcopy
        metadata["_meta"] = meta = deepcopy(metadata.get("_meta", {}))
        options = meta.setdefault("claudeCode", {}).setdefault("options", {})
        prior = metadata.pop("_kausNativeComputerPolicy", None)
        if prior is not None:
            if prior.get("hadDisallowedTools"):
                options["disallowedTools"] = prior["disallowedTools"]
            else:
                options.pop("disallowedTools", None)
        if names:
            metadata["_kausNativeComputerPolicy"] = {
                "hadDisallowedTools": "disallowedTools" in options,
                "disallowedTools": list(options.get("disallowedTools", []))}
            options["disallowedTools"] = list(dict.fromkeys([
                *options.get("disallowedTools", []), *("mcp__" + name + "__*" for name in names)]))
        return spec
    if kind != "codex":
        return spec
    values = dict(spec.env)
    previous = values.pop("KAUS_CU_CODEX_PATH", None)
    if previous is not None:
        if previous:
            values["CODEX_PATH"] = previous
        else:
            values.pop("CODEX_PATH", None)
        spec = spec.model_copy(update={"env": values})
    if not names:
        return spec
    from drivers.base import UnsupportedCapabilityError
    from drivers.projection_store import atomic_write_text
    from drivers.skill_execution.common import state_root
    native = spec.build_env().get("CODEX_PATH") or shutil.which("codex", path=spec.build_env().get("PATH"))
    if not native:
        raise UnsupportedCapabilityError("原生电脑工具配置尚未就绪")
    # Append to app-server arguments, preserving scoped launchers that dispatch
    # by their first argument. Help/version are untouched for capability checks.
    script = ('#!/bin/sh\ncase "$1" in --help|--version) exec ' + shlex.quote(native)
              + ' "$@";; esac\nexec ' + shlex.quote(native) + ' "$@" '
              + shlex.join(config_flags(names)) + '\n')
    directory = state_root(spec) / "computer-policy"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = directory / (hashlib.sha256(script.encode()).hexdigest() + ".sh")
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_text() != script:
            raise UnsupportedCapabilityError("原生电脑工具配置不一致")
    else:
        atomic_write_text(launcher, script)
    launcher.chmod(0o700)
    return spec.with_env(CODEX_PATH=str(launcher), KAUS_CU_CODEX_PATH=spec.env.get("CODEX_PATH", ""))


def external_command(command, adapter, metadata, *, scoped=False):
    names = disabled_servers(metadata)
    if not names:
        return tuple(command)
    kind = adapter_name(adapter)
    if kind == "codex":
        # The scoped terminal starts the same policy-wrapped app server.
        return tuple(command) if scoped else (*command[:1], *config_flags(names), *command[1:])
    if kind == "claude":
        return (*command, "--disallowedTools", ",".join("mcp__" + name + "__*" for name in names))
    return tuple(command)
