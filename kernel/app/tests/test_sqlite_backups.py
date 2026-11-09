from __future__ import annotations

import sqlite3
from datetime import datetime

from app.persistence.sqlite import SqliteDatabase
from app.persistence.sqlite.backups import daily_backup, sqlite_files
from app.persistence.sqlite.migrations import MIGRATIONS, current_version


def _rows(path, sql):
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_upgrading_an_existing_database_keeps_a_copy_of_the_old_version(tmp_path):
    path = tmp_path / "domain.sqlite3"
    older = MIGRATIONS[-2].version
    SqliteDatabase(path, apply_migrations=False).close()
    from app.persistence.sqlite.migrations import migrate
    database = SqliteDatabase(path, apply_migrations=False)
    migrate(database, target_version=older)
    database.close()

    upgraded = SqliteDatabase(path)
    assert current_version(upgraded) == MIGRATIONS[-1].version
    upgraded.close()

    (backup,) = (tmp_path / "backups").glob("domain-pre-v*.sqlite3")
    assert backup.name.startswith(f"domain-pre-v{MIGRATIONS[-1].version}-")
    assert _rows(backup, "SELECT MAX(version) FROM schema_version") == [(older,)]


def test_new_and_current_databases_are_not_backed_up(tmp_path):
    path = tmp_path / "domain.sqlite3"
    SqliteDatabase(path).close()
    SqliteDatabase(path).close()
    assert not (tmp_path / "backups").exists()


def _sqlite(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.execute("INSERT INTO t VALUES (?)", (value,))
    connection.commit()
    return connection  # 保持打开：WAL 里还有未合并的写入


def test_daily_backup_copies_every_sqlite_file_once_a_day(tmp_path):
    root = tmp_path / "state"
    live = [_sqlite(root / "domain.sqlite3", "a"), _sqlite(root / "workbench/module-data/kith/kith.db", "b")]
    (root / "notes.txt").write_text("not a database")
    outside = _sqlite(tmp_path / "elsewhere/custom.sqlite3", "c")
    try:
        made = daily_backup(root, [tmp_path / "elsewhere/custom.sqlite3"], now=datetime(2026, 11, 9, 10))
        assert made == root / "backups/daily-20261109"
        assert _rows(made / "domain.sqlite3", "SELECT v FROM t") == [("a",)]
        assert _rows(made / "workbench/module-data/kith/kith.db", "SELECT v FROM t") == [("b",)]
        assert _rows(made / "external/custom.sqlite3", "SELECT v FROM t") == [("c",)]
        assert not (made / "notes.txt").exists()
        assert daily_backup(root, now=datetime(2026, 11, 9, 23)) is None
        assert root / "backups/daily-20261109/domain.sqlite3" not in sqlite_files(root)
    finally:
        for connection in (*live, outside):
            connection.close()


def test_daily_backups_keep_the_latest_seven_days(tmp_path):
    root = tmp_path / "state"
    _sqlite(root / "domain.sqlite3", "a").close()
    for day in range(1, 10):
        daily_backup(root, now=datetime(2026, 11, day, 9))
    kept = sorted(p.name for p in (root / "backups").iterdir())
    assert kept == [f"daily-202611{day:02d}" for day in range(3, 10)]
