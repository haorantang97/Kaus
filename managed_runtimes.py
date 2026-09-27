"""Resolve locally installed, versioned Agent executables at the assembly edge.

The manifest is written by the local installer, never by a browser request.
Native credentials and native configuration remain with the Agent. Explicit
backend launch commands continue to take precedence over managed installations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parent / 'state' / 'agent-runtimes'


def managed_launch(preset_id: str, *, root: Path | None = None) -> tuple[tuple[str, ...] | None, dict[str, str]] | None:
    root = (root or RUNTIME_ROOT).resolve()
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        return None
    document = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('runtimes'), dict):
        raise ValueError('Agent runtime manifest is invalid')
    item: Any = document['runtimes'].get(preset_id)
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) - {'command', 'path', 'executable', 'version'}:
        raise ValueError('Agent runtime entry is invalid')

    def contained(value: str) -> Path:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError('Agent runtime path must be relative')
        path = (root / value).resolve()
        path.relative_to(root)
        return path

    command = item.get('command')
    if command is not None:
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v or '\0' in v for v in command):
            raise ValueError('Agent runtime command is invalid')
        executable = contained(command[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None
        command = (str(executable), *command[1:])
    else:
        executable = contained(item.get('executable'))
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None
    paths = item.get('path', [])
    if not isinstance(paths, list):
        raise ValueError('Agent runtime path list is invalid')
    search = [str(contained(entry)) for entry in paths]
    if any(not Path(entry).is_dir() for entry in search):
        return None
    return command, {'PATH': os.pathsep.join([*search, os.environ.get('PATH', '')])} if search else {}
