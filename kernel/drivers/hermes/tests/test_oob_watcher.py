"""带外检测器（AD-20 / 规格 §5，验收点 5）。

用一个临时 SQLite 文件模拟带外写入：WAL 模式、真的 INSERT 一行 message，
断言 1s 轮询之内触发。这里刻意**不 mock 文件系统**——AD-20 那条结论
（「只 stat state.db 会漏报」）正是文件系统行为决定的，mock 掉就什么都没验证。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from drivers.hermes.oob_watcher import (
    DEFAULT_TICK_SECONDS,
    DETECTION_SOURCE_SQL,
    HermesOutOfBandWatcher,
)
from runtime.session_host import OutOfBandWatcher

SESSION_ID = "api_1788339747_a7afdfa5"


def _make_state_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT, role TEXT, content TEXT, timestamp REAL, active TEXT)"
    )
    connection.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, title TEXT, last_activity_at REAL,"
        " message_count INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,"
        " pinned INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO sessions (id, title, message_count) VALUES (?, 'before', 1)",
        (SESSION_ID,),
    )
    connection.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active)"
        " VALUES (?, 'user', 'hello', 1788339714.4, '1')",
        (SESSION_ID,),
    )
    connection.commit()
    connection.close()


def _write_out_of_band(path: Path, content: str) -> None:
    """模拟别的客户端（外部 CLI）往同一个 state.db 里写了一条。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active)"
        " VALUES (?, 'assistant', ?, ?, '1')",
        (SESSION_ID, content, time.time()),
    )
    connection.commit()
    connection.close()


def _rename_out_of_band(path: Path, title: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, SESSION_ID))
    connection.commit()
    connection.close()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    _make_state_db(tmp_path / "state.db")
    return tmp_path


def test_implements_the_public_watcher_protocol(home: Path) -> None:
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    assert isinstance(watcher, OutOfBandWatcher)


def test_quiet_when_nothing_changed(home: Path) -> None:
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()
    assert watcher.poll() is False
    assert watcher.poll() is False


def test_detects_out_of_band_write_within_one_poll_tick(home: Path) -> None:
    """核心断言：1s 轮询周期内必须触发。"""
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()

    started = time.monotonic()
    _write_out_of_band(home / "state.db", "written by someone else")

    fired = False
    # 用远小于 1s 的 tick 去逼近「1s 周期内」这个上界：只要在 1s 之内落到 True，
    # 真实的 1s 轮询最迟下一拍就会报。
    while time.monotonic() - started < DEFAULT_TICK_SECONDS:
        if watcher.poll():
            fired = True
            break
        time.sleep(0.02)
    elapsed = time.monotonic() - started
    assert fired, f"{DEFAULT_TICK_SECONDS}s 内没有检测到带外写入"
    assert elapsed < DEFAULT_TICK_SECONDS
    assert watcher.detection_source == DETECTION_SOURCE_SQL


def test_detects_out_of_band_session_rename(home: Path) -> None:
    """Session metadata changes are OOB writes even when no message row changes."""
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()

    _rename_out_of_band(home / "state.db", "renamed elsewhere")

    _spin_until_true(watcher)
    assert watcher.detection_source == DETECTION_SOURCE_SQL


def test_edge_triggered_signal_clears_after_reporting(home: Path) -> None:
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()
    _write_out_of_band(home / "state.db", "one")
    _spin_until_true(watcher)
    assert watcher.poll() is False, "边沿触发：报过一次就该清零"


def test_self_write_suppression_while_a_run_is_active(home: Path) -> None:
    """规格 §5.2：run 进行中只更新水位、不产出提示——否则每发一句都弹提示。"""
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()
    watcher.set_run_active(True)
    _write_out_of_band(home / "state.db", "our own run")
    for _ in range(20):
        assert watcher.poll() is False
        time.sleep(0.01)
    watcher.set_run_active(False)
    # 水位已经在抑制期间被推上去了，所以之后也不该补报。
    assert watcher.poll() is False


def test_sync_watermark_after_our_own_run(home: Path) -> None:
    watcher = HermesOutOfBandWatcher(hermes_home=home)
    watcher.track(SESSION_ID)
    watcher.prime()
    _write_out_of_band(home / "state.db", "our own run")
    watcher.sync_watermark(SESSION_ID)
    for _ in range(20):
        if watcher.poll():
            pytest.fail("对齐水位之后不该再把自己的写入报成带外写入")
        time.sleep(0.01)


def test_missing_state_db_is_idle_not_an_error(tmp_path: Path) -> None:
    """规格 §5.3：全新 profile 还没有 state.db → idle，不抛、不误报。"""
    watcher = HermesOutOfBandWatcher(hermes_home=tmp_path / "brand-new")
    watcher.track(SESSION_ID)
    assert watcher.poll() is False
    assert watcher.poll() is False


def test_wal_signal_is_visible_before_the_main_file_size_changes(home: Path) -> None:
    """AD-20 的实测依据：``state.db`` 的 **size** 不动，但 ``-wal`` 会动。

    写入者必须**保持连接**——真实场景里那是常驻的 Hermes 进程。最后一个连接关闭时
    SQLite 会 checkpoint 并删掉 ``-wal``，那不是带外写入的现场。
    """
    db = home / "state.db"
    before_size = db.stat().st_size
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        for index in range(20):
            writer.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, active)"
                " VALUES (?, 'assistant', ?, ?, '1')",
                (SESSION_ID, f"row-{index}", time.time()),
            )
        writer.commit()
        wal = home / "state.db-wal"
        assert wal.exists(), "WAL 模式下应该有 -wal 边车文件"
        assert wal.stat().st_size > 0
        assert db.stat().st_size == before_size, (
            "探针结论：state.db 自身的 size 直到 checkpoint 才变——所以不监视它"
        )
    finally:
        writer.close()


def _spin_until_true(watcher: HermesOutOfBandWatcher, timeout: float = 1.0) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if watcher.poll():
            return
        time.sleep(0.02)
    pytest.fail("检测器在超时之前没有触发")
