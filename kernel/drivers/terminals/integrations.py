"""Reuse installed terminal integrations without copying vendor hook logic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from drivers.base import CliLaunchSpec
from .launcher import terminal_app_id, terminal_app_path
from . import managed_process


CMUX_WRAPPERS = {
    "claude-code": ("cmux-claude-wrapper", "CMUX_CUSTOM_CLAUDE_PATH"),
    "codex": ("cmux-codex-wrapper", "CMUX_CUSTOM_CODEX_PATH"),
    "hermes": ("cmux-hermes-agent-wrapper", "CMUX_CUSTOM_HERMES_AGENT_PATH"),
    "hermes-acp": ("cmux-hermes-agent-wrapper", "CMUX_CUSTOM_HERMES_AGENT_PATH"),
    "amp": ("cmux-amp-wrapper", "CMUX_CUSTOM_AMP_PATH"),
}


def terminal_wrapper(spec: CliLaunchSpec, terminal: str) -> tuple[Path, str] | None:
    """The terminal app's own agent wrapper (it installs its own hooks), if one applies."""
    binding = CMUX_WRAPPERS.get(spec.terminal_integration or "")
    app = terminal_app_path(terminal) if terminal_app_id(terminal) == "cmux" else None
    if binding and app:
        candidate = app / "Contents/Resources/bin" / binding[0]
        if candidate.is_file() and os.access(candidate, os.X_OK):
            # A configured wrapper already provides integration; never wrap it twice.
            if Path(spec.command[0]).resolve() != candidate.resolve():
                return candidate, binding[1]
    return None


#: Wrappers that deep-merge a caller's ``--settings`` into their own hooks, and
#: the program that merge needs. Without it the wrapper keeps the caller's
#: settings and drops its own hooks, so Kaus then leaves the wrapper alone.
CMUX_SETTINGS_MERGE = {"claude-code": "node"}


def wrapper_keeps_caller_hooks(spec: CliLaunchSpec, terminal: str, path: str | None = None) -> bool:
    """Whether hook settings passed through the terminal's own wrapper reach the agent
    alongside the wrapper's hooks."""
    requirement = CMUX_SETTINGS_MERGE.get(spec.terminal_integration or "")
    return bool(requirement and terminal_wrapper(spec, terminal) and shutil.which(requirement, path=path))


def prepare_command(spec: CliLaunchSpec, directory: Path, terminal: str) -> tuple[str, ...]:
    """Keep the selected binary and native arguments behind a one-shot guard."""
    wrapper, custom_key = terminal_wrapper(spec, terminal) or (None, None)
    metadata = directory / "native.json"
    metadata.write_text(json.dumps({"executable": spec.command[0],
                                   "config_directory": spec.config_directory,
                                   "custom_path_key": custom_key}), encoding="utf-8")
    metadata.chmod(0o600)
    entry = directory / "native-entry"
    entry.write_text("#!/bin/sh\nexec " + shlex.join((sys.executable,
        str(Path(managed_process.__file__).resolve()), str(directory))) + ' "$@"\n', encoding="utf-8")
    entry.chmod(0o700)
    if wrapper:
        command = ("/usr/bin/env", f"{custom_key}={entry}")
        if spec.terminal_integration == "claude-code":
            # Preserve the same auth/provider environment as a direct Kaus
            # launch. Values remain in the process environment, never on disk.
            command += ("CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV=1",)
        # cmux must inspect the same config root as the selected engine before
        # deciding whether hooks are already installed there.
        if spec.config_directory:
            command += ("=".join(spec.config_directory),)
        return (*command, str(wrapper), *spec.command[1:])
    return (str(entry), *spec.command[1:])
