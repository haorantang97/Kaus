"""Content-addressed skill baselines and writable, conversation-owned copies.

This module also runs inside the installed Agent interpreter. It has no Kaus
imports and never changes source skills, project memory, or the native session DB.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

MANIFEST = '.kaus-skills.json'
ENV_KEY = 'KAUS_HERMES_SKILL_SCOPE'


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(root):
    checksum = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in {'.git', '__pycache__'})
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            if path.is_symlink():
                raise RuntimeError('Skill snapshots cannot contain links')
            checksum.update(str(path.relative_to(root)).encode())
            if path.is_file():
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        checksum.update(chunk)
    return checksum.hexdigest()


def _owned(home):
    if home.is_symlink():
        raise RuntimeError('Skill snapshots require an owned home')
    marker = json.loads((home / '.kaus-scope.json').read_text())
    manifest_path = home / MANIFEST
    if manifest_path.is_symlink():
        raise RuntimeError('Skill policy must be owned')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('runtimePolicy') != {'version': 1, 'bindingId': marker.get('bindingId'), 'projectId': marker.get('projectId')}:
        raise RuntimeError('Skill snapshot ownership mismatch')
    if (home / 'skills').is_symlink() or not (home / 'skills').is_dir():
        raise RuntimeError('Skill snapshot requires private skills')
    return marker, manifest


def capture(home):
    """Publish a baseline once; existing generations are never overwritten."""
    home = Path(home)
    marker, manifest = _owned(home)
    content_hash = digest(home / 'skills')
    encoded = json.dumps([manifest, content_hash], sort_keys=True)
    generation = hashlib.sha256(encoded.encode()).hexdigest()
    parent = home / '.kaus-skill-generations'
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink():
        raise RuntimeError('Skill generation directory cannot be a link')
    destination = parent / generation
    if destination.exists():
        if destination.is_symlink() or json.loads((destination / MANIFEST).read_text()) != manifest or digest(destination / 'skills') != content_hash:
            raise RuntimeError('Immutable skill generation was modified')
        return destination
    stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=parent))
    try:
        shutil.copytree(home / 'skills', stage / 'skills', ignore=shutil.ignore_patterns('.git', '__pycache__'))
        if digest(stage / 'skills') != content_hash or digest(home / 'skills') != content_hash:
            raise RuntimeError('Skill source changed while publishing snapshot')
        _write(stage / MANIFEST, manifest)
        _write(stage / '.kaus-generation.json', {'generation': generation, 'contentHash': content_hash, **marker})
        try:
            stage.rename(destination)
        except FileExistsError:
            if digest(destination / 'skills') != content_hash:
                raise RuntimeError('Concurrent skill generation mismatch')
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def validate_scope(home, scope):
    home, scope = Path(home).resolve(), Path(scope)
    if scope.is_symlink() or not scope.resolve().is_relative_to(home / '.kaus-skill-sessions'):
        raise RuntimeError('Skill runtime scope is outside its project home')
    if any(path.is_symlink() for path in (home / '.kaus-scope.json', scope / '.kaus-skill-session.json', scope / MANIFEST)):
        raise RuntimeError('Skill runtime metadata must be owned')
    marker = json.loads((home / '.kaus-scope.json').read_text())
    session = json.loads((scope / '.kaus-skill-session.json').read_text())
    if any(session.get(key) != marker.get(key) for key in ('bindingId', 'projectId')):
        raise RuntimeError('Skill runtime scope ownership mismatch')
    expected = home / '.kaus-skill-sessions' / hashlib.sha256(session['conversationId'].encode()).hexdigest() / session['generation']
    if scope.resolve() != expected:
        raise RuntimeError('Skill runtime scope identity mismatch')
    if (scope / 'skills').is_symlink() or not (scope / 'skills').is_dir():
        raise RuntimeError('Skill runtime scope is missing')
    digest(scope / 'skills')  # reject links added after materialization
    return scope.resolve()


def pin_session(home, conversation_id, *, resume=False):
    """Pin once for cold resume; new conversations get their own writable copy."""
    home = Path(home)
    marker, _ = _owned(home)
    session_key = hashlib.sha256(conversation_id.encode()).hexdigest()
    parent = home / '.kaus-skill-sessions' / session_key
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.parent.is_symlink():
        raise RuntimeError('Skill session directory cannot be a link')
    current_path = parent / 'current.json'
    if current_path.is_symlink():
        raise RuntimeError('Skill session pointer must be owned')
    if resume and current_path.is_file():
        current = json.loads(current_path.read_text())
        scope = parent / current['generation']
        return validate_scope(home, scope)
    generation = capture(home)
    scope = parent / generation.name
    if not scope.exists():
        stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=parent))
        try:
            shutil.copytree(generation / 'skills', stage / 'skills')
            shutil.copy2(generation / MANIFEST, stage / MANIFEST)
            _write(stage / '.kaus-skill-session.json', {
                'bindingId': marker['bindingId'], 'projectId': marker['projectId'],
                'conversationId': conversation_id, 'generation': generation.name})
            try:
                stage.rename(scope)
            except FileExistsError:
                validate_scope(home, scope)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    validate_scope(home, scope)
    _write(current_path, {'generation': generation.name})
    return scope


def session_mode(home, conversation_id, *, mode=None):
    home = Path(home)
    parent = home / '.kaus-skill-modes'
    key = hashlib.sha256(conversation_id.encode()).hexdigest()
    path = parent / (key + '.json')
    previous = json.loads(path.read_text()).get('mode') if path.is_file() else None
    if mode is not None:
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink():
            raise RuntimeError('Skill mode storage must be owned')
        _write(path, {'conversationId': conversation_id, 'mode': mode})
    return previous


def native_output(home, conversation_id):
    return Path(home) / '.kaus-native-skill-outputs' / hashlib.sha256(conversation_id.encode()).hexdigest()
