"""本地数据库的自动备份。

两种时机：

1. **升级前**：领域库有待应用的迁移时，先把当前版本整份存下来
   （``backups/<库名>-pre-v<目标版本>-<时间>.sqlite3``，保留最近 5 份）；
2. **每天一次**：数据目录下所有 SQLite 文件（领域库、工作台、原生归档、模块自己的库）
   按相对路径存进 ``backups/daily-<日期>/``，保留最近 7 天。

一律走 SQLite 在线备份接口：WAL 模式下读不阻塞写，得到的是一份一致的单文件，
不需要连带 ``-wal`` / ``-shm``。只认文件头是 SQLite 的文件，不管扩展名；
附件等其他文件不在这里备份。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

BACKUP_DIR_NAME = "backups"
PRE_MIGRATION_KEEP = 5
DAILY_KEEP = 7

_SQLITE_HEADER = b"SQLite format 3\x00"
_DAILY_RE = re.compile(r"^daily-\d{8}$")


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def copy_connection(source: sqlite3.Connection, destination: Path) -> Path:
    """把一条已打开连接的库整份写到 ``destination``（先写临时文件再改名）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    target = sqlite3.connect(str(partial))
    try:
        source.backup(target)
    finally:
        target.close()
    os.replace(partial, destination)
    destination.chmod(0o600)
    return destination


def copy_file(source: Path, destination: Path) -> Path:
    """另开一条只读连接备份 ``source``，不打扰正在写它的进程。"""
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return copy_connection(connection, destination)
    finally:
        connection.close()


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def backup_before_migration(
    connection: sqlite3.Connection, database_path: Path, target_version: int, *, now: datetime | None = None
) -> Path:
    stamp = _stamp(now or datetime.now())
    backups = database_path.parent / BACKUP_DIR_NAME
    prefix = f"{database_path.stem}-pre-v"
    destination = copy_connection(connection, backups / f"{prefix}{target_version}-{stamp}.sqlite3")
    older = sorted((p for p in backups.glob(f"{prefix}*.sqlite3") if p != destination),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in older[PRE_MIGRATION_KEEP - 1:]:
        stale.unlink(missing_ok=True)
    return destination


def sqlite_files(data_root: Path) -> list[Path]:
    """数据目录里所有 SQLite 主文件（跳过备份目录与 ``-wal`` / ``-shm``）。"""
    found = []
    for path in sorted(data_root.rglob("*")):
        relative = path.relative_to(data_root)
        if relative.parts[0] == BACKUP_DIR_NAME or not path.is_file():
            continue
        if path.name.endswith(("-wal", "-shm", "-journal", ".partial")):
            continue
        if is_sqlite_file(path):
            found.append(path)
    return found


def daily_backup(
    data_root: Path, extra: Iterable[Path] = (), *, now: datetime | None = None, keep: int = DAILY_KEEP
) -> Path | None:
    """今天还没备份就备份一次，返回备份目录；已有今天的则返回 ``None``。

    ``extra`` 是不在数据目录下的库（例如用环境变量另指的领域库）。
    """
    now = now or datetime.now()
    backups = data_root / BACKUP_DIR_NAME
    destination = backups / f"daily-{now:%Y%m%d}"
    if destination.is_dir():
        return None
    partial = backups / f".daily-{now:%Y%m%d}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    sources = {path.resolve(): path.relative_to(data_root) for path in sqlite_files(data_root)}
    for path in extra:
        resolved = path.resolve()
        if resolved not in sources and is_sqlite_file(resolved):
            sources[resolved] = Path("external") / resolved.name
    for source, relative in sources.items():
        copy_file(source, partial / relative)
    partial.mkdir(parents=True, exist_ok=True)
    os.replace(partial, destination)
    days = sorted(p for p in backups.iterdir() if p.is_dir() and _DAILY_RE.match(p.name))
    for stale in days[:-keep]:
        shutil.rmtree(stale, ignore_errors=True)
    return destination


async def daily_backup_loop(
    data_root: Path, extra: Iterable[Path] = (), *, log: Callable[[str], None] = print, interval: float = 3600
) -> None:
    """启动时备份一次，之后每小时看一眼日期变了没有。失败只记日志，下一轮再试。"""
    extra = tuple(extra)
    while True:
        try:
            made = await asyncio.to_thread(daily_backup, data_root, extra)
            if made is not None:
                log(f"[backup] 每日备份：{made}")
        except Exception as exc:  # noqa: BLE001 - 备份失败不能拖垮服务
            log(f"[backup] 每日备份失败：{exc!r}")
        await asyncio.sleep(interval)


__all__ = [
    "BACKUP_DIR_NAME",
    "backup_before_migration",
    "copy_file",
    "daily_backup",
    "daily_backup_loop",
    "is_sqlite_file",
    "sqlite_files",
]
