"""技能执行副本：支持文件硬链接到原件，SKILL.md 是真副本，旧副本过期清理。"""
import os
import time
from pathlib import Path

from drivers.skill_execution.common import STALE_SELECTION_SECONDS, _link_or_copy, _prune_selections


def test_supporting_files_are_linked_and_skill_md_is_copied(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "SKILL.md").write_text("x")
    (source / "data.txt").write_text("payload")
    target = tmp_path / "dst"
    target.mkdir()
    _link_or_copy(source / "SKILL.md", target / "SKILL.md")
    _link_or_copy(source / "data.txt", target / "data.txt")
    assert os.stat(source / "data.txt").st_ino == os.stat(target / "data.txt").st_ino
    assert os.stat(source / "SKILL.md").st_ino != os.stat(target / "SKILL.md").st_ino


def test_old_selections_are_pruned_but_the_current_one_and_strangers_stay(tmp_path):
    owner = tmp_path / "owner"
    current, old, foreign = owner / "current", owner / "old", owner / "foreign"
    for path in (current, old, foreign):
        path.mkdir(parents=True)
    for path in (current, old):
        (path / ".kaus-skill-selection.json").write_text("{}")
    past = time.time() - STALE_SELECTION_SECONDS - 60
    for path in (old, foreign, current):
        os.utime(path, (past, past))
    _prune_selections(owner, current)
    assert current.exists() and foreign.exists() and not old.exists()
