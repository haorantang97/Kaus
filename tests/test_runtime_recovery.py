import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'kernel'))
import managed_runtimes as managed
from runtime_management import RuntimeManager


def job(**updates):
    return {'id': 'a' * 32, 'version': '1.10.0', 'state': 'verifying', **updates}


@pytest.mark.parametrize('raw', ['{broken', '[]', '\udcff'])
def test_corrupt_journal_preserved_without_blocking_startup(tmp_path, raw):
    payload = raw.encode('utf-8', errors='surrogateescape')
    (tmp_path / 'jobs.json').write_bytes(payload)
    manager = RuntimeManager(tmp_path)
    assert manager.jobs == {}
    assert manager.snapshot('codex')['recoveryWarning']
    backups = list(tmp_path.glob('jobs.recovery-*.json'))
    assert len(backups) == 1 and backups[0].read_bytes() == payload
    assert json.loads((tmp_path / 'jobs.json').read_text()) == {}


@pytest.mark.parametrize('malformed', [None, [], 'oops', job(state=[]), job(version='../escape')])
def test_malformed_row_keeps_other_completed_jobs(tmp_path, malformed):
    saved = {'codex': malformed, 'claude-code': job(state='complete')}
    (tmp_path / 'jobs.json').write_text(json.dumps(saved))
    manager = RuntimeManager(tmp_path)
    assert list(manager.jobs) == ['claude-code']
    assert manager.jobs['claude-code']['state'] == 'complete'
    assert list(tmp_path.glob('jobs.recovery-*.json'))


def test_selected_completed_activation_recovers_and_never_deletes(tmp_path):
    saved = job()
    target = tmp_path / 'codex' / f"1.10.0-{saved['id']}" / 'main'
    target.parent.mkdir(parents=True)
    target.write_text('#!/bin/sh\nexit 0\n')
    target.chmod(0o700)
    managed.activate_runtime('codex', {'version': '1.10.0', 'command': [str(target.relative_to(tmp_path))]}, root=tmp_path)
    (tmp_path / 'jobs.json').write_text(json.dumps({'codex': saved}))
    manager = RuntimeManager(tmp_path)
    assert manager.jobs['codex']['state'] == 'complete'
    assert manager.jobs['codex']['refreshPending']
    assert target.is_file()


def test_bad_manifest_preserves_interrupted_version(tmp_path):
    saved = job()
    target = tmp_path / 'codex' / f"1.10.0-{saved['id']}"
    target.mkdir(parents=True)
    (tmp_path / 'jobs.json').write_text(json.dumps({'codex': saved}))
    (tmp_path / 'manifest.json').write_text('{broken')
    manager = RuntimeManager(tmp_path)
    assert manager.jobs['codex']['state'] == 'failed'
    assert target.is_dir()
    assert manager.recovery_warning


def test_unactivated_interrupted_version_removed(tmp_path):
    saved = job()
    target = tmp_path / 'codex' / f"1.10.0-{saved['id']}"
    target.mkdir(parents=True)
    (tmp_path / 'jobs.json').write_text(json.dumps({'codex': saved}))
    manager = RuntimeManager(tmp_path)
    assert manager.jobs['codex']['state'] == 'failed'
    assert not target.exists()


def test_recovery_never_follows_linked_journal_or_directory(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    root = tmp_path / 'managed'; root.mkdir()
    journal = outside / 'jobs.json'; journal.write_text('{bad')
    (root / 'jobs.json').symlink_to(journal)
    manager = RuntimeManager(root)
    assert manager.jobs == {} and journal.read_text() == '{bad'
    (root / 'jobs.json').unlink()
    saved = job()
    (root / 'jobs.json').write_text(json.dumps({'codex': saved}))
    (root / 'codex').symlink_to(outside, target_is_directory=True)
    target = outside / f"1.10.0-{saved['id']}"; target.mkdir()
    RuntimeManager(root)
    assert target.is_dir()


def test_recovery_write_failure_does_not_block_startup(tmp_path, monkeypatch):
    (tmp_path / 'jobs.json').write_text(json.dumps({'codex': job()}))
    monkeypatch.setattr(managed, '_atomic_json', lambda *a: (_ for _ in ()).throw(PermissionError()))
    manager = RuntimeManager(tmp_path)
    assert manager.jobs['codex']['state'] == 'failed'
    assert manager.recovery_warning == '安装记录暂未保存'


def test_cleanup_failure_does_not_block_startup(tmp_path, monkeypatch):
    target = tmp_path / '.install-orphan'; target.mkdir()
    os.utime(target, (0, 0))
    import runtime_management
    monkeypatch.setattr(runtime_management.shutil, 'rmtree', lambda *a: (_ for _ in ()).throw(PermissionError()))
    manager = RuntimeManager(tmp_path)
    assert manager.recovery_warning and target.exists()
