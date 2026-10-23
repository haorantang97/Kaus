"""Locate a CLI's installed package without executing shell startup files."""
from pathlib import Path
import json
import re
import shlex
import shutil


def locate(spec, package_name):
    env = spec.build_env()
    executable = shutil.which(spec.command[0], path=env.get("PATH"))
    candidates = [Path(executable)] if executable else []
    candidates.extend(Path(arg) for arg in spec.command[1:] if arg.endswith(('.js', '.mjs')))
    visited = set()
    while candidates:
        candidate = candidates.pop().expanduser().resolve()
        if candidate in visited:
            continue
        visited.add(candidate)
        for parent in candidate.parents:
            manifest = parent / 'package.json'
            try:
                if json.loads(manifest.read_text()).get('name') == package_name:
                    return parent
            except (OSError, ValueError):
                pass
        try:
            if candidate.stat().st_size > 65536:
                continue
            text = candidate.read_text()
            if not text.startswith('#!'):
                continue
            # A simple absolute exec target in a local launcher. Never eval its
            # exports, substitutions, arguments or configuration.
            for match in re.finditer(r'^exec\s+([^\n]+)', text, re.M):
                argv = shlex.split(match.group(1))
                if argv and Path(argv[0]).is_absolute():
                    candidates.append(Path(argv[0]))
        except (OSError, UnicodeError, ValueError):
            pass
    return None
