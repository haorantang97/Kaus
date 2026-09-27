"""带外写入检测器（AD-20 / 规格 §5）。

实现 :class:`~runtime.session_host.OutOfBandWatcher` 协议：``poll() -> bool``，
边沿触发（报告后信号清零）。每个 ``HERMES_HOME`` 一个实例，被该 home 下所有
RuntimeHandle 共享。

为什么必须看 ``-wal``（探针结论原文，两轮一致）
-----------------------------------------------
    WAL 模式下 state.db 主文件的 mtime 可能直到 checkpoint 才变；软提示检测器
    必须同时看 -wal 文件或直接做只读 SQL 回读，只 stat state.db 会漏报。

实测时延：``state.db-wal`` 的 mtime/size 与 SQL 回读都在 0.31–0.37s 内可见，而
``state.db`` 自身的 **size 在 20s 内始终不变**——所以本模块**不监视它的 size**。

两级检测
--------
第一级 stat（廉价，零 SQL）：``state.db-wal`` 的 (mtime, size) + ``state.db``
的 mtime。都没变就直接结束本 tick。第二级只读 SQL 回读：对每个已登记的会话查
``MAX(id), COUNT(*)``，与水位比较。

自写抑制
--------
本 Driver 自己的 run 结束时调用 :meth:`sync_watermark`；run 进行中
（:meth:`set_run_active`）第二级只更新水位、不产出提示——否则我们自己的写入会
被当成带外写入，每发一句话就弹一次提示。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from drivers.hermes.history import read_session_row, read_watermark

#: AD-20 锁定：1s 轮询即可满足「发送前刷新」。与 §3.6 的 POLLING 共用同一个 tick。
DEFAULT_TICK_SECONDS = 1.0
#: 规格 §5.2：连续变化 250ms 内合并为一次。
DEBOUNCE_SECONDS = 0.25
#: 规格 §5.2：同一会话 30s 内至多产出一条软提示。
THROTTLE_SECONDS = 30.0
#: 规格 §5.3：state.db 不存在（全新 profile）时每 30s 重试一次存在性。
IDLE_RECHECK_SECONDS = 30.0

DETECTION_SOURCE_WAL = "hermes:state-db-wal"
DETECTION_SOURCE_SQL = "hermes:sql-readback"


@dataclass
class _Stat:
    mtime: float | None = None
    size: int | None = None


@dataclass
class HermesOutOfBandWatcher:
    """一个 ``HERMES_HOME`` 的带外检测器。

    :meth:`poll` 是 :class:`~runtime.session_host.OutOfBandWatcher` 要的那个方法；
    Session Host 在 ``send_message`` 之前调用它。返回 ``True`` 表示自上次 poll
    以来这个 home 上发生过**不是我们写的**变更。
    """

    hermes_home: Path
    #: 要跟踪水位的会话 id（一般是该 home 下所有活跃 Conversation 的原生会话）。
    session_ids: set[str] = field(default_factory=set)
    clock: object = field(default=time.monotonic)

    _db: _Stat = field(default_factory=_Stat)
    _wal: _Stat = field(default_factory=_Stat)
    # A message-only watermark misses session metadata mutations such as
    # ``hermes sessions rename``.  Include the stable session fields that can
    # change out of band so the SQL confirmation stage can attribute a cheap
    # DB/WAL stat change to one of the conversations we actually track.
    _watermarks: dict[
        str,
        tuple[int | None, int, str | None, float | None, int | None, bool, bool, bool],
    ] = field(default_factory=dict)
    _run_active: bool = False
    _pending: bool = False
    _last_change_at: float | None = None
    _last_notice_at: dict[str, float] = field(default_factory=dict)
    _last_source: str | None = None
    _primed: bool = False
    _last_existence_check: float | None = None

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #

    @property
    def state_db(self) -> Path:
        return Path(self.hermes_home) / "state.db"

    @property
    def wal_path(self) -> Path:
        return Path(self.hermes_home) / "state.db-wal"

    @property
    def detection_source(self) -> str:
        """最近一次触发是哪一级发现的（写进 ``ConcurrencyAdvisory.detection_source``）。

        它对公共层是**不透明字符串**（N §5.4）：探测方式属 Driver 内部。
        """
        return self._last_source or DETECTION_SOURCE_WAL

    # ------------------------------------------------------------------ #
    # 登记
    # ------------------------------------------------------------------ #

    def track(self, session_id: str) -> None:
        if not session_id:
            return
        self.session_ids.add(session_id)
        if session_id not in self._watermarks:
            self._watermarks[session_id] = self._read_watermark(session_id)

    def untrack(self, session_id: str) -> None:
        self.session_ids.discard(session_id)
        self._watermarks.pop(session_id, None)
        self._last_notice_at.pop(session_id, None)

    def set_run_active(self, active: bool) -> None:
        """run 进行中：第二级只更新水位、不产出提示（自写抑制）。"""
        self._run_active = active

    def sync_watermark(self, session_id: str | None = None) -> None:
        """把当前 ``MAX(id)`` 写进水位——每次本 Driver 的 run 结束时调用。"""
        targets = [session_id] if session_id else list(self.session_ids)
        for target in targets:
            if target:
                self._watermarks[target] = self._read_watermark(target)
        self._pending = False

    def prime(self) -> None:
        """把当前状态当成基线，之后的变化才算「带外」。"""
        self._db = self._stat(self.state_db)
        self._wal = self._stat(self.wal_path)
        for session_id in self.session_ids:
            self._watermarks[session_id] = self._read_watermark(session_id)
        self._primed = True
        self._pending = False

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #

    def poll(self) -> bool:
        """:class:`~runtime.session_host.OutOfBandWatcher` 的实现。

        边沿触发：报告一次之后信号清零。去抖窗口内的连续变化合并成一次；
        同一会话 30s 内至多一条提示。
        """
        now = float(self.clock())  # type: ignore[operator]
        if not self._primed:
            self.prime()
            return False
        if not self.state_db.exists():
            # 规格 §5.3：全新 profile 还没有 state.db → idle，30s 重试存在性。
            self._last_existence_check = now
            return False

        changed_cheap = self._poll_stat()
        if changed_cheap:
            self._last_change_at = now
            self._last_source = DETECTION_SOURCE_WAL

        if not changed_cheap and not self._pending:
            return False

        changed_sql = self._poll_sql()
        if changed_sql:
            self._last_source = DETECTION_SOURCE_SQL

        if self._run_active:
            # 自写抑制：我们自己正在写，水位已在 _poll_sql 里更新过了。
            self._pending = False
            return False
        if not changed_sql and not self._pending:
            return False

        # 去抖：250ms 内的连续变化合成一次，先挂起、下一 tick 再报。
        if self._last_change_at is not None and now - self._last_change_at < DEBOUNCE_SECONDS:
            self._pending = True
            return False

        self._pending = False
        throttled = all(
            now - self._last_notice_at.get(session_id, -THROTTLE_SECONDS) < THROTTLE_SECONDS
            for session_id in (self.session_ids or {""})
        )
        if throttled and self._last_notice_at:
            return False
        for session_id in self.session_ids or {""}:
            self._last_notice_at[session_id] = now
        return True

    # ------------------------------------------------------------------ #
    # 两级
    # ------------------------------------------------------------------ #

    def _poll_stat(self) -> bool:
        """第一级：stat。**只看 state.db 的 mtime，不看它的 size**（AD-20 明文）。"""
        db = self._stat(self.state_db)
        wal = self._stat(self.wal_path)
        changed = (
            db.mtime != self._db.mtime
            or wal.mtime != self._wal.mtime
            or wal.size != self._wal.size
        )
        self._db, self._wal = db, wal
        return changed

    def _poll_sql(self) -> bool:
        """第二级：只读 SQL 回读，与水位比较。"""
        changed = False
        for session_id in tuple(self.session_ids):
            current = self._read_watermark(session_id)
            previous = self._watermarks.get(session_id)
            if previous is not None and current != previous:
                changed = True
            self._watermarks[session_id] = current
        return changed

    def _read_watermark(
        self, session_id: str
    ) -> tuple[int | None, int, str | None, float | None, int | None, bool, bool, bool]:
        try:
            max_id, count = read_watermark(self.state_db, session_id)
            row = read_session_row(self.state_db, session_id) or {}
            return (
                max_id,
                count,
                row.get("title"),
                row.get("last_activity_at"),
                row.get("message_count"),
                bool(row.get("archived")),
                bool(row.get("pinned")),
                bool(row.get("hidden")),
            )
        except (sqlite3.Error, OSError):
            return (None, 0, None, None, None, False, False, False)

    @staticmethod
    def _stat(path: Path) -> _Stat:
        try:
            info = path.stat()
        except OSError:
            return _Stat()
        return _Stat(mtime=info.st_mtime, size=info.st_size)

    def signals(self) -> Mapping[str, object]:
        """给诊断面板的一行现状（不含任何密钥）。"""
        return {
            "hermesHome": str(self.hermes_home),
            "stateDbExists": self.state_db.exists(),
            "walSize": self._wal.size,
            "trackedSessions": sorted(self.session_ids),
            "runActive": self._run_active,
        }


__all__ = [
    "DEBOUNCE_SECONDS",
    "DEFAULT_TICK_SECONDS",
    "DETECTION_SOURCE_SQL",
    "DETECTION_SOURCE_WAL",
    "IDLE_RECHECK_SECONDS",
    "THROTTLE_SECONDS",
    "HermesOutOfBandWatcher",
]
