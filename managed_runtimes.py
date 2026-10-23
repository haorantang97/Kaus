"""Versioned Agent launch records owned by Kaus; native installations stay untouched.

Activation only selects an already-installed, validated directory. This module
never downloads a package, updates a native CLI, or stops an existing process.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parent / 'state' / 'agent-runtimes'


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _document(root: Path) -> dict:
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        return {'version': 1, 'runtimes': {}}
    document = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('runtimes'), dict):
        raise ValueError('Agent runtime manifest is invalid')
    history = document.get('history', {})
    if not isinstance(history, dict) or any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in history.values()):
        raise ValueError('Agent runtime history is invalid')
    return document


def _resolve_entry(root: Path, item):
    if not isinstance(item, dict) or set(item) - {'command', 'path', 'executable', 'version'}:
        raise ValueError('Agent runtime entry is invalid')
    if 'version' in item and not isinstance(item['version'], str):
        raise ValueError('Agent runtime version is invalid')

    def contained(value):
        if not isinstance(value, str) or not value or Path(value).is_absolute() or '\0' in value:
            raise ValueError('Agent runtime path must be relative')
        path = (root / value).resolve()
        path.relative_to(root)
        return path

    command = item.get('command')
    if command is not None:
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v or '\0' in v for v in command):
            raise ValueError('Agent runtime command is invalid')
        executable = contained(command[0])
        command = (str(executable), *command[1:])
    else:
        executable = contained(item.get('executable'))
    paths = item.get('path', [])
    if not isinstance(paths, list):
        raise ValueError('Agent runtime path list is invalid')
    search = [str(contained(entry)) for entry in paths]
    if not executable.is_file() or not os.access(executable, os.X_OK) or any(not Path(p).is_dir() for p in search):
        return None
    return command, {'PATH': os.pathsep.join([*search, os.environ.get('PATH', '')])} if search else {}


def managed_launch(preset_id: str, *, root: Path | None = None):
    root = (root or RUNTIME_ROOT).resolve()
    item = _document(root)['runtimes'].get(preset_id)
    return _resolve_entry(root, item) if item is not None else None


def managed_snapshot(preset_id: str, *, root: Path | None = None) -> dict:
    root = (root or RUNTIME_ROOT).resolve()
    item = _document(root)['runtimes'].get(preset_id)
    return {'configured': item is not None, 'revision': _digest(item),
            'version': item.get('version') if isinstance(item, dict) else None}


def _atomic_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _manifest_lock(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / '.manifest.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def activate_runtime(preset_id: str, entry: dict, *, root: Path | None = None,
                     expected_revision: str | None = None) -> dict:
    """Select an installed immutable version and retain its previous selection."""
    root = (root or RUNTIME_ROOT).resolve()
    if not isinstance(preset_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', preset_id):
        raise ValueError('Agent runtime identity is invalid')
    with _manifest_lock(root):
        if _resolve_entry(root, entry) is None:
            raise ValueError('Agent runtime files are unavailable')
        if not entry.get('version') or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]*', entry['version']):
            raise ValueError('An exact installed version is required')
        document = _document(root)
        previous = document['runtimes'].get(preset_id)
        if expected_revision is not None and expected_revision != _digest(previous):
            raise ValueError('Agent runtime selection changed; refresh before retrying')
        if previous != entry:
            history = document.setdefault('history', {}).setdefault(preset_id, [])
            if previous is not None:
                history.append(previous)
            document['history'][preset_id] = history[-5:]
            document['runtimes'][preset_id] = entry
            _atomic_json(root / 'manifest.json', document)
        return managed_snapshot(preset_id, root=root)


def rollback_runtime(preset_id: str, *, root: Path | None = None,
                     expected_revision: str | None = None) -> dict:
    """Restore the last installed selection; no installation or process mutation."""
    root = (root or RUNTIME_ROOT).resolve()
    with _manifest_lock(root):
        document = _document(root)
        current = document['runtimes'].get(preset_id)
        if expected_revision is not None and expected_revision != _digest(current):
            raise ValueError('Agent runtime selection changed; refresh before retrying')
        history = document.get('history', {}).get(preset_id, [])
        if not history:
            raise ValueError('No previous managed runtime is available')
        previous = history[-1]
        if _resolve_entry(root, previous) is None:
            raise ValueError('Previous runtime files are unavailable')
        document['runtimes'][preset_id] = previous
        document['history'][preset_id] = history[:-1]
        _atomic_json(root / 'manifest.json', document)
        return managed_snapshot(preset_id, root=root)


def file_stamp(value: str | Path | None):
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        return (str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ino)
    except OSError:
        return (str(path), None)


def native_path(driver) -> str | None:
    from drivers.acp.native_cli import native_path as resolve_native_path
    return resolve_native_path(driver)


def launch_fingerprint(driver) -> str:
    """Hash launch/config file metadata, never save credential or argv contents."""
    spec = driver.assembly_spec
    agent = driver.agent_spec
    preset = getattr(driver, 'preset', None)
    env = agent.build_env()
    executable = shutil.which(agent.command[0], path=env.get('PATH')) or agent.command[0]
    files = [file_stamp(executable), file_stamp(native_path(driver))]
    for value in agent.command[1:]:
        if Path(value).is_absolute() and Path(value).is_file():
            files.append(file_stamp(value))
    root = env.get(getattr(preset, 'config_root_env', None)) or getattr(preset, 'config_root_default', None)
    if root:
        files.extend(file_stamp(Path(root).expanduser() / name) for name in preset.config_watch_files)
    selection = managed_snapshot(preset.id, root=getattr(driver, 'managed_runtime_root', None)) if preset and not spec.command else None
    return _digest({'command': agent.command, 'cwd': agent.cwd, 'env': dict(agent.env),
                    'path': env.get('PATH'), 'options': dict(spec.options), 'files': files,
                    'authEnvironment': {key: env.get(key) for key in (*spec.env_keys, *(preset.env_keys_hint if preset else ()))},
                    'selection': selection, 'presetCommand': preset.command if preset else None,
                    'loginCommand': preset.login_command if preset else None})


async def read_native_version(driver, *, timeout=4.0) -> dict:
    """Read a native --version response without login, inference or auto-update."""
    path = native_path(driver)
    if not path or not Path(path).is_file():
        return {'engineVersion': None, 'nativePath': path, 'nativeVersionState': 'not_found'}
    process = None
    try:
        process = await asyncio.create_subprocess_exec(path, '--version', env=driver.agent_spec.build_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        async def collect():
            output = await process.stdout.read(16384)
            await process.wait()
            return output
        output = await asyncio.wait_for(collect(), timeout)
        match = re.search(r'\b(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?)\b', output.decode(errors='replace'))
        version = match.group(1) if process.returncode == 0 and match else None
        return {'engineVersion': version, 'nativePath': path,
                'nativeVersionState': 'observed' if version else 'unknown',
                'engineVersionSource': 'native_cli_version_command'}
    except (OSError, asyncio.TimeoutError):
        return {'engineVersion': None, 'nativePath': path, 'nativeVersionState': 'unavailable'}
    finally:
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def save_observation(root: Path, backend_id: str, value: dict):
    """One private file per backend; no secrets, raw output or launch arguments."""
    payload = {**value, 'backendId': backend_id, 'observedAt': datetime.now(timezone.utc).isoformat()}
    _atomic_json(root / 'runtime-observations' / (_digest(backend_id) + '.json'), payload)
