# -*- coding: utf-8 -*-
"""backup_baseline.sh / restore_baseline.sh 的 fixture 测试。

在临时目录里造一个假 HERMES_HOME（含分身 symlink、密钥文件、体积目录），
真跑两个 bash 脚本，断言：安全红线生效、清单可校验、还原正确且不破坏 symlink。
绝不碰任何真实 ~/.hermes。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess

import pytest

OPS_DIR = pathlib.Path(__file__).resolve().parent.parent
BACKUP = OPS_DIR / "backup_baseline.sh"
RESTORE = OPS_DIR / "restore_baseline.sh"

SECRET_MARKERS = {
    "env": "SECRET_ENV_VALUE_MUST_NEVER_BE_COPIED",
    "cred": "SECRET_CRED_VALUE_MUST_NEVER_BE_COPIED",
    "auth": "SECRET_AUTH_VALUE_MUST_NEVER_BE_COPIED",
    "apikey": "SECRET_APIKEY_VALUE_MUST_NEVER_BE_COPIED",
    "skill": "SKILL_BODY_MUST_NEVER_BE_COPIED",
}


def _w(p: pathlib.Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "hermes"

    # ---- 根 profile（default = HERMES_HOME 本体）----
    _w(h / "config.yaml", "providers:\n  anthropic:\n    api_key: sk-ant-ROOT\n")
    _w(h / "SOUL.md", "ROOT SOUL\n")
    _w(h / "config.yaml.dashbak", "providers: {}\n")
    (h / "memories").mkdir(parents=True, exist_ok=True)
    _w(h / "memories" / "m.md", "root memory\n")
    _w(h / "skills" / "shared-skill" / "SKILL.md", SECRET_MARKERS["skill"] + "\n")

    # ---- 绝不能被复制的东西 ----
    _w(h / ".env", "OPENAI_API_KEY=%s\n" % SECRET_MARKERS["env"])
    _w(h / "credentials" / "oauth.json", '{"t": "%s"}\n' % SECRET_MARKERS["cred"])
    _w(h / "auth.json", '{"a": "%s"}\n' % SECRET_MARKERS["auth"])

    # ---- dashboard 树 ----
    d = h / "dashboard"
    _w(d / "server.py", "print('server')\n")
    _w(d / "hierarchy.json", '{"alpha": "default"}\n')
    _w(d / "web" / "src" / "App.tsx", "export default 1\n")
    _w(d / "web" / "package.json", "{}\n")
    _w(d / ".env", "BACKEND_SECRET=%s\n" % SECRET_MARKERS["env"])
    _w(d / "apikey.txt", SECRET_MARKERS["apikey"] + "\n")
    _w(d / "my_token.json", SECRET_MARKERS["apikey"] + "\n")
    _w(d / "node_modules" / "pkg" / "index.js", "bulk\n")
    _w(d / "web" / "dist" / "assets" / "app.js", "bulk\n")
    _w(d / "archive" / "old" / "z.txt", "bulk\n")
    _w(d / "tmp" / "t.txt", "bulk\n")
    _w(d / "__pycache__" / "x.pyc", "bulk\n")
    _w(d / ".pytest_cache" / "v" / "cache" / "x", "bulk\n")
    _w(d / ".git" / "config", "[core]\n")
    os.symlink(str(d / "server.py"), str(d / "link-to-server.py"))

    # ---- alpha：普通子 profile，skills 里有 symlink ----
    a = h / "profiles" / "alpha"
    _w(a / "config.yaml", "providers:\n  anthropic:\n    api_key: sk-ant-ROOT\n")
    _w(a / ".dash_inherited.json", '{"providers": {"anthropic": {"api_key": "sk-ant-ROOT"}}}\n')
    _w(a / "SOUL.md", "ALPHA SOUL\n")
    (a / "skills").mkdir(parents=True, exist_ok=True)
    os.symlink(str(h / "skills" / "shared-skill"), str(a / "skills" / "shared-skill"))
    (a / "memories").mkdir(parents=True, exist_ok=True)
    _w(a / ".env", "ALPHA_SECRET=%s\n" % SECRET_MARKERS["env"])

    # ---- twin：memories/config/skills 全 symlink 共享 ----
    t = h / "profiles" / "twin"
    t.mkdir(parents=True, exist_ok=True)
    os.symlink(str(h / "memories"), str(t / "memories"))
    os.symlink(str(h / "config.yaml"), str(t / "config.yaml"))
    os.symlink(str(h / "skills"), str(t / "skills"))
    _w(t / "SOUL.md", "TWIN SOUL\n")
    return h


def run_backup(home, *extra):
    r = subprocess.run(["bash", str(BACKUP), "--hermes-home", str(home), "--quiet"] + list(extra),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return pathlib.Path(r.stdout.strip().splitlines()[-1])


def run_restore(backup_dir, home, *extra):
    return subprocess.run(
        ["bash", str(RESTORE), str(backup_dir), "--hermes-home", str(home)] + list(extra),
        capture_output=True, text=True)


def all_files(root: pathlib.Path):
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 语法
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("script", [BACKUP, RESTORE])
def test_bash_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", [BACKUP, RESTORE])
def test_help_works(script):
    r = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "用法" in r.stdout


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #

def test_backup_layout(home):
    b = run_backup(home)
    assert b.is_dir()
    for f in ("MANIFEST.txt", "RESTORE.md", "RESTORE_MAP.tsv",
              "SKIPPED.txt", "SYMLINKS.txt", "BACKUP_INFO.txt"):
        assert (b / f).is_file(), f
    assert (b / "dashboard" / "server.py").is_file()
    assert (b / "dashboard" / "web" / "src" / "App.tsx").is_file()
    for prof in ("default", "alpha", "twin"):
        assert (b / "profiles" / prof / "profile.txt").is_file()
        assert (b / "profiles" / prof / "skills.listing.txt").is_file()
        assert (b / "profiles" / prof / "memories.link.txt").is_file()


def test_backup_never_copies_secrets(home):
    b = run_backup(home)
    names = [p.name for p in all_files(b)]
    assert ".env" not in names
    assert "auth.json" not in names
    assert "oauth.json" not in names
    assert "apikey.txt" not in names
    assert "my_token.json" not in names
    # 更强的断言：备份里任何文件的内容都不含这些标记串
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in all_files(b))
    for marker in SECRET_MARKERS.values():
        assert marker not in blob, marker


def test_backup_excludes_bulk_dirs(home):
    b = run_backup(home)
    rels = {str(p.relative_to(b)) for p in all_files(b)}
    for bad in ("node_modules", "dist", "archive", "tmp", "__pycache__",
                ".pytest_cache", ".git"):
        assert not any(bad in r.split("/") for r in rels), bad


def test_backup_include_git_flag(home):
    b = run_backup(home, "--include-git")
    rels = {str(p.relative_to(b)) for p in all_files(b)}
    assert "dashboard/.git/config" in rels


def test_backup_records_skipped_paths_only(home):
    b = run_backup(home)
    skipped = (b / "SKIPPED.txt").read_text(encoding="utf-8")
    assert "apikey.txt" in skipped
    assert "node_modules" in skipped
    for marker in SECRET_MARKERS.values():
        assert marker not in skipped        # 只记路径，不记内容


def test_backup_records_symlinks_without_following(home):
    b = run_backup(home)
    links = (b / "SYMLINKS.txt").read_text(encoding="utf-8")
    assert "dashboard/link-to-server.py" in links
    assert "profiles/twin/config.yaml" in links
    # twin 的 config.yaml 是 symlink，绝不能被复制成实体文件
    assert not (b / "profiles" / "twin" / "config.yaml").exists()


def test_backup_skills_layout_only(home):
    b = run_backup(home)
    listing = (b / "profiles" / "alpha" / "skills.listing.txt").read_text(encoding="utf-8")
    assert "shared-skill ->" in listing              # readlink 结果在
    assert SECRET_MARKERS["skill"] not in listing    # 技能内容不在
    assert not (b / "profiles" / "alpha" / "skills").exists()


def test_backup_detects_twin(home):
    b = run_backup(home)
    twin = (b / "profiles" / "twin" / "profile.txt").read_text(encoding="utf-8")
    alpha = (b / "profiles" / "alpha" / "profile.txt").read_text(encoding="utf-8")
    assert "main_twin_candidate: yes" in twin
    assert "main_twin_candidate: no" in alpha


def test_manifest_hashes_match(home):
    b = run_backup(home)
    lines = [l for l in (b / "MANIFEST.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines
    for line in lines:
        want, rel = line.split("  ", 1)
        p = b / rel
        assert p.is_file(), rel
        assert sha256(p) == want, rel
    # MANIFEST 不含自己
    assert "MANIFEST.txt" not in {l.split("  ", 1)[1] for l in lines}


def test_restore_map_targets_are_relative(home):
    b = run_backup(home)
    rows = [l for l in (b / "RESTORE_MAP.tsv").read_text(encoding="utf-8").splitlines()
            if l and not l.startswith("#")]
    pairs = dict(r.split("\t", 1) for r in rows)
    assert pairs["profiles/default/config.yaml"] == "config.yaml"
    assert pairs["profiles/alpha/config.yaml"] == "profiles/alpha/config.yaml"
    assert pairs["dashboard/server.py"] == "dashboard/server.py"
    for target in pairs.values():
        assert not target.startswith("/")


def test_backup_refuses_existing_dir(home, tmp_path):
    out = tmp_path / "already"
    out.mkdir()
    r = subprocess.run(["bash", str(BACKUP), "--hermes-home", str(home), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "已存在" in r.stderr


def test_backup_refuses_out_inside_dashboard(home):
    r = subprocess.run(["bash", str(BACKUP), "--hermes-home", str(home),
                        "--out", str(home / "dashboard" / "bk")],
                       capture_output=True, text=True)
    assert r.returncode != 0


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #

def test_restore_dry_run_writes_nothing(home):
    b = run_backup(home)
    (home / "dashboard" / "server.py").write_text("MUTATED\n", encoding="utf-8")
    r = run_restore(b, home)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout
    assert "dashboard/server.py" in r.stdout
    assert (home / "dashboard" / "server.py").read_text(encoding="utf-8") == "MUTATED\n"


def test_restore_apply(home):
    b = run_backup(home)
    (home / "dashboard" / "server.py").write_text("MUTATED\n", encoding="utf-8")
    (home / "profiles" / "alpha" / "SOUL.md").unlink()
    (home / "config.yaml").write_text("providers: {}\n", encoding="utf-8")

    r = run_restore(b, home, "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert (home / "dashboard" / "server.py").read_text(encoding="utf-8") == "print('server')\n"
    assert (home / "profiles" / "alpha" / "SOUL.md").read_text(encoding="utf-8") == "ALPHA SOUL\n"
    assert "api_key: sk-ant-ROOT" in (home / "config.yaml").read_text(encoding="utf-8")
    # 两条重启命令必须打印
    assert "com.hermes.dashboard.backend" in r.stdout
    assert "com.hermes.dashboard.web" in r.stdout
    assert "launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend" in r.stdout


def test_restore_keeps_twin_symlink(home):
    b = run_backup(home)
    r = run_restore(b, home, "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert (home / "profiles" / "twin" / "config.yaml").is_symlink()
    assert (home / "profiles" / "twin" / "memories").is_symlink()


def test_restore_creates_pre_restore_snapshot_that_rolls_back(home):
    b = run_backup(home)
    (home / "dashboard" / "server.py").write_text("MUTATED\n", encoding="utf-8")
    r = run_restore(b, home, "--apply", "--yes")
    assert r.returncode == 0, r.stderr

    snaps = sorted((home / "dashboard-backups").glob("pre-restore-*"))
    assert snaps, "没有生成 pre-restore 快照"
    snap = snaps[-1]
    assert (snap / "dashboard" / "server.py").read_text(encoding="utf-8") == "MUTATED\n"

    # 用快照再滚回去
    r2 = run_restore(snap, home, "--apply", "--yes")
    assert r2.returncode == 0, r2.stderr
    assert (home / "dashboard" / "server.py").read_text(encoding="utf-8") == "MUTATED\n"


def test_pre_restore_snapshots_never_collide(home):
    """两次还原（哪怕在同一秒）必须落在不同目录 —— 撞名会截断正在读的备份索引。"""
    b = run_backup(home)
    for i in range(3):
        (home / "dashboard" / "server.py").write_text("V%d\n" % i, encoding="utf-8")
        r = run_restore(b, home, "--apply", "--yes")
        assert r.returncode == 0, r.stderr
    snaps = sorted((home / "dashboard-backups").glob("pre-restore-*"))
    assert len(snaps) == 3, [p.name for p in snaps]
    assert len({p.name for p in snaps}) == 3


def test_no_pre_restore_dir_when_nothing_written(home):
    b = run_backup(home)
    r = run_restore(b, home, "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert "写入 0 个文件" in r.stdout
    assert not list((home / "dashboard-backups").glob("pre-restore-*"))


def test_restore_detects_corrupt_backup(home):
    b = run_backup(home)
    (b / "dashboard" / "server.py").write_text("TAMPERED\n", encoding="utf-8")
    r = run_restore(b, home)
    assert r.returncode == 3
    assert "校验" in (r.stdout + r.stderr)


def test_restore_reports_layout_drift(home):
    b = run_backup(home)
    (home / "profiles" / "alpha" / "skills" / "shared-skill").unlink()
    r = run_restore(b, home)
    assert r.returncode == 0, r.stderr
    assert "skills/ 符号链接布局与备份记录不一致" in r.stdout
    # 布局差异只报告，不自动重建
    assert not (home / "profiles" / "alpha" / "skills" / "shared-skill").exists()


def test_restore_rejects_non_backup_dir(tmp_path, home):
    r = run_restore(tmp_path, home)
    assert r.returncode != 0


def test_restore_is_idempotent(home):
    b = run_backup(home)
    r1 = run_restore(b, home, "--apply", "--yes")
    assert r1.returncode == 0, r1.stderr
    r2 = run_restore(b, home, "--apply", "--yes")
    assert r2.returncode == 0, r2.stderr
    assert "还原完成：写入 0 个文件" in r2.stdout
