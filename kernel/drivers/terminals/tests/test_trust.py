import json
import os
from pathlib import Path
import tomllib

import pytest

from drivers.terminals.trust import (GRANTED, LOCKED, MISSING, UNCHANGED, UNREADABLE,
                                     grant_workspace_trust)


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / 'work space'
    path.mkdir()
    return path


def test_claude_adds_only_the_folder_and_keeps_fields_and_mode(tmp_path, workspace):
    home = tmp_path / 'home'
    home.mkdir()
    config = home / '.claude.json'
    config.write_text(json.dumps({'oauthAccount': {'email': 'synthetic@example.test'},
        'projects': {'/other': {'allowedTools': ['Bash']}}}))
    config.chmod(0o600)
    assert grant_workspace_trust('claude', str(workspace), home=str(home)) == GRANTED
    data = json.loads(config.read_text())
    assert data['oauthAccount'] == {'email': 'synthetic@example.test'}
    assert data['projects']['/other'] == {'allowedTools': ['Bash']}
    assert data['projects'][os.path.realpath(workspace)] == {'hasTrustDialogAccepted': True}
    assert config.stat().st_mode & 0o777 == 0o600
    assert not Path(f'{config}.lock').exists()
    assert grant_workspace_trust('claude', str(workspace), home=str(home)) == UNCHANGED


def test_claude_never_creates_config_and_defers_to_its_lock(tmp_path, workspace):
    home = tmp_path / 'home'
    home.mkdir()
    assert grant_workspace_trust('claude', str(workspace), home=str(home)) == MISSING
    assert not (home / '.claude.json').exists()
    (home / '.claude.json').write_text('{}')
    Path(f'{home / ".claude.json"}.lock').mkdir()
    assert grant_workspace_trust('claude', str(workspace), home=str(home)) == LOCKED
    assert json.loads((home / '.claude.json').read_text()) == {}


def test_claude_uses_config_dir_and_legacy_file(tmp_path, workspace):
    config_dir = tmp_path / 'claude-config'
    config_dir.mkdir()
    (config_dir / '.claude.json').write_text('{}')
    assert grant_workspace_trust('claude', str(workspace), str(config_dir), home=str(tmp_path)) == GRANTED
    legacy = config_dir / '.config.json'
    legacy.write_text('{}')
    assert grant_workspace_trust('claude', str(workspace), str(config_dir), home=str(tmp_path)) == GRANTED
    assert os.path.realpath(workspace) in json.loads(legacy.read_text())['projects']


def test_claude_refuses_unexpected_projects_shape(tmp_path, workspace):
    (tmp_path / '.claude.json').write_text(json.dumps({'projects': []}))
    assert grant_workspace_trust('claude', str(workspace), home=str(tmp_path)) == UNREADABLE
    assert json.loads((tmp_path / '.claude.json').read_text()) == {'projects': []}


def test_codex_appends_table_and_respects_explicit_levels(tmp_path, workspace):
    home = tmp_path / 'codex'
    home.mkdir()
    config = home / 'config.toml'
    assert grant_workspace_trust('codex', str(workspace), str(home)) == MISSING
    config.write_text('model = "synthetic"\n[features]\nplugins = true')
    assert grant_workspace_trust('codex', str(workspace), str(home)) == GRANTED
    data = tomllib.loads(config.read_text())
    assert data['model'] == 'synthetic' and data['features'] == {'plugins': True}
    assert data['projects'][os.path.realpath(workspace)] == {'trust_level': 'trusted'}
    assert grant_workspace_trust('codex', str(workspace), str(home)) == UNCHANGED
    other = tmp_path / 'other'
    other.mkdir()
    config.write_text('[projects.' + json.dumps(os.path.realpath(other)) + ']\ntrust_level = "untrusted"\n')
    assert grant_workspace_trust('codex', str(other), str(home)) == UNCHANGED
    assert 'untrusted' in config.read_text()


def test_codex_leaves_inline_projects_table_untouched(tmp_path, workspace):
    config = tmp_path / 'config.toml'
    original = 'projects = { "/x" = { trust_level = "trusted" } }\n'
    config.write_text(original)
    assert grant_workspace_trust('codex', str(workspace), str(tmp_path)) == UNREADABLE
    assert config.read_text() == original


def test_cursor_marker_requires_existing_config_root(tmp_path, workspace):
    root = tmp_path / '.cursor'
    assert grant_workspace_trust('cursor', str(workspace), str(root)) == MISSING
    root.mkdir()
    assert grant_workspace_trust('cursor', str(workspace), str(root)) == GRANTED
    slug = os.path.realpath(workspace).lstrip('/').replace('/', '-')
    marker = root / 'projects' / slug / '.workspace-trusted'
    assert json.loads(marker.read_text())['workspacePath'] == os.path.realpath(workspace)
    assert grant_workspace_trust('cursor', str(workspace), str(root)) == UNCHANGED


def test_copilot_appends_folder_with_owner_only_mode(tmp_path, workspace):
    root = tmp_path / '.copilot'
    root.mkdir()
    config = root / 'config.json'
    assert grant_workspace_trust('copilot', str(workspace), str(root)) == MISSING
    config.write_text(json.dumps({'trustedFolders': ['/existing'], 'theme': 'dark'}))
    config.chmod(0o644)
    assert grant_workspace_trust('copilot', str(workspace), str(root)) == GRANTED
    data = json.loads(config.read_text())
    assert data == {'trustedFolders': ['/existing', os.path.realpath(workspace)], 'theme': 'dark'}
    assert config.stat().st_mode & 0o777 == 0o600
    assert grant_workspace_trust('copilot', str(workspace), str(root)) == UNCHANGED


def test_unknown_kind_is_a_programming_error(workspace):
    with pytest.raises(ValueError):
        grant_workspace_trust('unknown', str(workspace))
