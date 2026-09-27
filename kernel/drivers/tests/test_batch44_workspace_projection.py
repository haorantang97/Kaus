"""工作目录文件投影的落盘纪律与路径黑名单（批次四十四 / AD-166）。

只用 ``tmp_path`` 与假路径，**一个真实家目录都不碰**（HANDOFF §2）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers import managed_block, projection_store, workspace_projection as wp
from drivers.projection_store import BACKUP_RETENTION, Provenance, list_backups

MARKER = "kaus:instructions"
PROJECT = "project:media"
FILENAME = "AGENT-NOTES.md"
ROWS = (("instructions", "house-style"),)


def _materialize(root: Path, body: str, **kwargs) -> wp.BlockOutcome:
    params = {
        "rows": ROWS,
        "body": body,
        "workspace_root": str(root),
        "filename": FILENAME,
        "marker": MARKER,
        "project_id": PROJECT,
    }
    params.update(kwargs)
    return wp.materialize_block(**params)


# --------------------------------------------------------------------------- #
# 拒绝理由
# --------------------------------------------------------------------------- #


def test_no_workspace_root_is_an_explicit_refusal(tmp_path: Path) -> None:
    outcome = _materialize(tmp_path, "x", workspace_root=None)
    assert not outcome.applied
    assert [e.reason for e in outcome.unsupported] == ["no_workspace_root"]
    assert "工作目录" in (outcome.unsupported[0].detail or "")


def test_no_convention_is_an_explicit_refusal(tmp_path: Path) -> None:
    outcome = _materialize(tmp_path, "x", filename=None)
    assert [e.reason for e in outcome.unsupported] == ["no_convention"]


def test_nothing_to_project_produces_nothing(tmp_path: Path) -> None:
    outcome = _materialize(tmp_path, "", rows=())
    assert outcome == wp.BlockOutcome()


def test_shared_workspace_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / FILENAME
    other = managed_block.render_block(MARKER, "别人的正文", "project:trading")
    target.write_text(other + "\n", encoding="utf-8")
    snapshot = target.read_bytes()
    outcome = _materialize(tmp_path, "我的正文", dry_run=False)
    assert [e.reason for e in outcome.unsupported] == ["workspace_shared"]
    assert "project:trading" in (outcome.unsupported[0].detail or "")
    assert target.read_bytes() == snapshot, "共用目录时一个字节都不许写"


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda tmp: str(tmp / "nope"), id="不存在"),
        pytest.param(lambda tmp: str(tmp / "a-file"), id="不是目录"),
        pytest.param(lambda tmp: "relative/path", id="相对路径"),
    ],
)
def test_denied_paths(tmp_path: Path, make) -> None:
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    outcome = _materialize(tmp_path, "x", workspace_root=make(tmp_path), dry_run=False)
    assert [e.reason for e in outcome.unsupported] == ["workspace_denied"]


def test_home_itself_and_its_hidden_directories_are_denied(
    tmp_path: Path, monkeypatch
) -> None:
    """家目录本身、以及家目录下任何隐藏目录（各程序自己的窝）一律不写。"""
    home = tmp_path / "home"
    (home / ".some-engine" / "profiles" / "media").mkdir(parents=True)
    (home / "work" / "media").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert wp.workspace_denial(str(home)) is not None
    assert "隐藏目录" in (wp.workspace_denial(str(home / ".some-engine")) or "")
    assert "隐藏目录" in (
        wp.workspace_denial(str(home / ".some-engine" / "profiles" / "media")) or ""
    )
    assert wp.workspace_denial(str(home / "work" / "media")) is None


def test_filesystem_root_is_denied() -> None:
    assert wp.workspace_denial("/") is not None


# --------------------------------------------------------------------------- #
# dry-run / 真写 / 备份 / 记账
# --------------------------------------------------------------------------- #


def test_dry_run_computes_before_after_but_writes_nothing(tmp_path: Path) -> None:
    outcome = _materialize(tmp_path, "新指令")
    assert not (tmp_path / FILENAME).exists(), "dry-run 落盘了"
    assert not (tmp_path / projection_store.PROVENANCE_FILENAME).exists()
    (row,) = outcome.applied
    assert row.action == "set"
    assert row.before is None and row.after == "新指令"
    assert row.target_ref == f"file://{tmp_path / FILENAME}#{MARKER}"
    assert any("dry-run" in w for w in outcome.warnings)


def test_confirmed_write_creates_the_file_and_records_provenance(tmp_path: Path) -> None:
    outcome = _materialize(tmp_path, "新指令", dry_run=False)
    target = tmp_path / FILENAME
    assert managed_block.read_block(target.read_text(encoding="utf-8"), MARKER) == (
        PROJECT,
        "新指令",
    )
    assert outcome.backup_path is None, "原来没有这个文件，就没有可备份的东西"
    provenance = Provenance.load(tmp_path)
    key_path = wp.key_path_of(FILENAME, MARKER)
    assert provenance.manages(key_path)
    assert provenance.version_of(key_path) == wp.body_digest("新指令")


def test_existing_user_content_survives_verbatim(tmp_path: Path) -> None:
    target = tmp_path / FILENAME
    original = "# 我的笔记\n\n第一条。\n第二条。\n"
    target.write_text(original, encoding="utf-8")
    _materialize(tmp_path, "项目指令", dry_run=False)
    text = target.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert wp.block_outside(text, MARKER).strip() == original.strip()


def test_second_write_is_unchanged_and_makes_no_backup(tmp_path: Path) -> None:
    _materialize(tmp_path, "同一份", dry_run=False)
    snapshot = (tmp_path / FILENAME).read_bytes()
    again = _materialize(tmp_path, "同一份", dry_run=False)
    assert [e.action for e in again.applied] == ["unchanged"]
    assert again.backup_path is None
    assert (tmp_path / FILENAME).read_bytes() == snapshot


def test_backups_are_kept_at_five(tmp_path: Path) -> None:
    target = tmp_path / FILENAME
    for index in range(BACKUP_RETENTION + 3):
        _materialize(tmp_path, f"第 {index} 版", dry_run=False)
    backups = list_backups(target)
    assert 0 < len(backups) <= BACKUP_RETENTION
    assert all(b.parent == target.parent for b in backups), "备份不落 /tmp"


def test_failed_verification_rolls_back(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / FILENAME
    original = "# 用户的东西\n"
    target.write_text(original, encoding="utf-8")

    def _sabotage(path: Path, text: str) -> None:
        path.write_text("### 这不是我们算出来的内容 ###\n", encoding="utf-8")

    monkeypatch.setattr(projection_store, "atomic_write_text", _sabotage)
    with pytest.raises(projection_store.VerificationError):
        _materialize(tmp_path, "指令", dry_run=False)
    assert target.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# 四态对账
# --------------------------------------------------------------------------- #


def _drift(root: Path, body: str, **kwargs):
    params = {
        "rows": ROWS,
        "body": body,
        "workspace_root": str(root),
        "filename": FILENAME,
        "marker": MARKER,
        "project_id": PROJECT,
    }
    params.update(kwargs)
    return wp.inspect_block_drift(**params)


def test_drift_four_states(tmp_path: Path) -> None:
    # ① 还没写过 → unmanaged（不算漂移）
    (entry,) = _drift(tmp_path, "指令")
    assert entry.state == "unmanaged"

    # ② 写过、没人动 → in_sync
    _materialize(tmp_path, "指令", dry_run=False)
    (entry,) = _drift(tmp_path, "指令")
    assert entry.state == "in_sync"

    # ③ 有人手改了块里的字 → drifted
    target = tmp_path / FILENAME
    target.write_text(
        target.read_text(encoding="utf-8").replace("指令", "被人改过的指令"),
        encoding="utf-8",
    )
    (entry,) = _drift(tmp_path, "指令")
    assert entry.state == "drifted"
    assert entry.actual == "被人改过的指令"

    # ④ 块被整个删了 → missing
    target.write_text("只剩我自己的内容\n", encoding="utf-8")
    (entry,) = _drift(tmp_path, "指令")
    assert entry.state == "missing"


def test_drift_reports_another_projects_block_as_unmanaged(tmp_path: Path) -> None:
    (tmp_path / FILENAME).write_text(
        managed_block.render_block(MARKER, "别人的", "project:trading") + "\n",
        encoding="utf-8",
    )
    (entry,) = _drift(tmp_path, "我的")
    assert entry.state == "unmanaged"
    assert "另一个项目" in (entry.detail or "")


def test_drift_says_nothing_when_there_is_no_target(tmp_path: Path) -> None:
    assert _drift(tmp_path, "x", workspace_root=None) == ()
    assert _drift(tmp_path, "x", filename=None) == ()
    assert _drift(tmp_path, "x", workspace_root=str(tmp_path / "nope")) == ()
