"""Validated, immutable terminal configuration overlays owned by Kaus."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex

from drivers.base import UnsupportedCapabilityError
from .common import state_root


def selected(conversation, field, *option_ids):
    value = getattr(conversation, field, None)
    if value is None:
        options = getattr(conversation, 'config_options', {}) or {}
        value = next((options[key] for key in option_ids if key in options), None)
    if value is not None and (not isinstance(value, str) or not value or '\x00' in value or '\n' in value):
        raise UnsupportedCapabilityError('终端配置值无效')
    return value


def choice(value, choices, label):
    if value is None:
        return None
    if value not in choices:
        raise UnsupportedCapabilityError(f'终端不支持当前{label}')
    return choices[value] if isinstance(choices, dict) else value


def approval(conversation):
    value = selected(conversation, 'approval_mode')
    if value == 'host:auto':
        raise UnsupportedCapabilityError('终端无法沿用 Kaus 自动审批，请切换审批方式')
    return value if value == 'native:auto' else (value.removeprefix('native:') if value else None)


def immutable(spec, conversation, content, *, suffix):
    identity = hashlib.sha256(str(conversation.id).encode()).hexdigest()[:24]
    root = state_root(spec) / 'terminal-settings' / identity
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise UnsupportedCapabilityError('终端配置目录不可用')
    path = root / (hashlib.sha256(content.encode()).hexdigest() + suffix)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_text() != content:
            raise UnsupportedCapabilityError('终端配置已变化，请重试')
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
    return path


def environment(spec, conversation, command, values):
    if not values:
        return tuple(command)
    if any(not isinstance(value, str) or '\x00' in value for value in values.values()):
        raise UnsupportedCapabilityError('终端配置值无效')
    script = '#!/bin/sh\nexec env ' + shlex.join([key + '=' + value for key, value in sorted(values.items())])
    script += ' ' + shlex.quote(command[0]) + ' "$@"\n'
    launcher = immutable(spec, conversation, script, suffix='.sh')
    launcher.chmod(0o700)
    return (str(launcher), *command[1:])


def arguments(command, *pairs):
    """Append explicit values as separate argv elements, never shell text."""
    result = list(command)
    for flag, value in pairs:
        if value is not None:
            result.extend((flag, value))
    return tuple(result)
