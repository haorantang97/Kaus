"""Per-launch status hooks for native CLIs, passed as arguments only.

Nothing is written to the user's global agent configuration: the hook settings
travel on the command line of this one launch (Claude ``--settings``), and the
hook only reports status (see :mod:`drivers.terminals.status_hook`). Event names
follow Claude Code's hook events; stablyai/orca (MIT) uses the same events.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

KINDS = ("claude",)


def hook_arguments(kind: str) -> tuple[str, ...]:
    if kind != "claude":
        raise ValueError(f"Unknown status hook kind: {kind}")
    script = shlex.join((sys.executable, str(Path(__file__).with_name("status_hook.py"))))

    def hook(event: str) -> list[dict]:
        return [{"hooks": [{"type": "command", "command": f"{script} {event}", "timeout": 5}]}]

    # Notification fires for permission prompts and idle input waits; unlike
    # PermissionRequest it cannot change the agent's decision.
    settings = {"hooks": {"UserPromptSubmit": hook("prompt"), "Notification": hook("notification"),
                          "Stop": hook("stop")}}
    return ("--settings", json.dumps(settings, separators=(",", ":")))
