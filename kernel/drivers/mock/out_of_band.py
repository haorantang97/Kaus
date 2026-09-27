"""带外变更信号的假实现（AD-20 在 Mock 侧的对照物）。

真实 Backend 的检测器监视原生存储的 WAL 文件 mtime/size 并辅以 SQL 回读
（AD-20，Phase 3B 的活儿）。Mock 侧不碰任何文件：变化由测试**显式声明**
（:meth:`FakeOutOfBandWatcher.mark_changed`），于是 Session Host 的
「发送前刷新 + 软提示」这条链路可以在不运行任何真实引擎的情况下跑通。

语义与 :class:`~runtime.session_host.OutOfBandWatcher` 一致：
:meth:`poll` 报告**自上次 poll 以来**是否发生过带外变更，报告后信号自动清零
（边沿触发，不是电平）。
"""

from __future__ import annotations


class FakeOutOfBandWatcher:
    """脚本驱动的 :class:`~runtime.session_host.OutOfBandWatcher`。"""

    def __init__(self, *, changed: bool = False) -> None:
        self._changed = changed
        #: poll 被调用的次数。测试用它断言「发送前确实查了一次」。
        self.poll_count = 0

    def mark_changed(self) -> None:
        """模拟一次带外写入（例如用户自己在终端里跑了 CLI）。"""
        self._changed = True

    def poll(self) -> bool:
        self.poll_count += 1
        changed, self._changed = self._changed, False
        return changed


class AsyncFakeOutOfBandWatcher(FakeOutOfBandWatcher):
    """异步形态的同一件事：Session Host 两种都接受。"""

    async def poll(self) -> bool:  # type: ignore[override]
        return FakeOutOfBandWatcher.poll(self)


__all__ = ["AsyncFakeOutOfBandWatcher", "FakeOutOfBandWatcher"]
