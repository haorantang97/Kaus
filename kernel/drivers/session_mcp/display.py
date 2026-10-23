"""Read-only display metadata from the native MCP extension's ready catalog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat


def filter_tool_title(spec, title):
    if spec is None or not isinstance(title, str) or not title.startswith('mcp__'):
        return title
    config = spec.env.get('KAUS_SESSION_MCP_CONFIG')
    if not config:
        return title
    try:
        path = Path(str(config) + '.titles.json')
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
                raise ValueError()
            data = json.load(stream)
        label = data.get('titles', {}).get(title) if data.get('schema') == 1 else None
        return label if isinstance(label, str) and label.strip() else 'MCP'
    except (OSError, ValueError, TypeError, AttributeError):
        # Display metadata is optional and never controls actual execution.
        return 'MCP'
