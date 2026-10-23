"""Bounded read-only primitives for public native conversation messages."""
from __future__ import annotations
from datetime import datetime
import json
import os
from pathlib import Path
import stat

MAX_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024


def records(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('native_history_outside_home')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('native_history_size_or_type')
        total = 0
        while True:
            offset = stream.tell()
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_LINE_BYTES or total > MAX_BYTES:
                raise ValueError('native_history_too_large')
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('native_history_invalid_record')
                yield offset, value


def text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return ''.join(part.get('text', '') for part in content
        if isinstance(part, dict) and not part.get('thought')
        and isinstance(part.get('text'), str)
        and part.get('type') in (None, 'text', 'input_text', 'output_text'))


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    except ValueError:
        return None
