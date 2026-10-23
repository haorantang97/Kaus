"""Small read-only catalogue queries; persisted data contains source paths only."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import time

from drivers.projection_store import atomic_write_text
from drivers.skill_inventory import NativeSkillRoot
from .common import state_root
from .inventory import project_workspaces


def cache_path(spec, key):
    identity = json.dumps({"command": spec.command, "cwd": spec.cwd, "env": spec.build_env()}, sort_keys=True)
    return state_root(spec) / "native-inventory" / key / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


async def query(spec, command, *, cwd=None):
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd or spec.cwd,
        env=spec.build_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name == 'posix')
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 25)
    except asyncio.TimeoutError:
        raise ValueError("技能来源读取超时") from None
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL) if os.name == 'posix' else process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode or len(output) > 8 * 1024 * 1024:
        raise ValueError("技能来源读取失败")
    return output.decode("utf-8", errors="replace")


def save_paths(spec, key, paths):
    result = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.name == "SKILL.md" and path.is_file():
            result.append(str(path.resolve()))
    target = cache_path(spec, key)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(target, json.dumps({"paths": sorted(set(result)), "updatedAt": time.time()}))


def cached_roots(spec, key, label, projects, bindings):
    projects = tuple(projects)
    root_id = next((p.id for p in projects if p.slug == "default"), None)
    root_id = root_id or next((p.id for p in projects if not p.parent_project_id), None)
    if not root_id:
        return ()
    try:
        data = json.loads(cache_path(spec, key).read_text())
    except (OSError, UnicodeError, ValueError):
        return ()
    workspaces = project_workspaces(projects, bindings, key)
    result = []
    for raw in data.get("paths", []):
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if path.name != "SKILL.md" or not path.is_file():
            continue
        owners = [(project, workspace) for project, workspace in workspaces if path.is_relative_to(workspace)]
        # Prefer the most specific registered project directory.
        depth = max((len(workspace.parts) for _, workspace in owners), default=-1)
        ids = [project for project, workspace in owners if len(workspace.parts) == depth] or [root_id]
        result.extend(NativeSkillRoot(path.parent, key, label, project) for project in ids)
    return tuple(result)
