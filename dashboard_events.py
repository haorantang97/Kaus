"""Dashboard refresh events: change detection on a state signature, fanned out over SSE.

The host polls a lightweight signature of the dashboard-visible state (today:
the Project tree revision in the domain database) and pushes a
``state_changed`` event to every connected browser when it changes, so pages
learn that another client or script changed the tree without a manual
refresh. :class:`DashboardEventHub` owns the subscribers, the last known
signature and the watch loop; ``server.py`` supplies the signature reader and
mounts :meth:`DashboardEventHub.stream` on ``GET /api/events``.

Event shapes (unchanged from the previous inline implementation)::

    {"type": "state_snapshot", "scopes": [...all scopes...], "changed": {}, "version", "ts"}
    {"type": "state_changed",  "scopes": [...sorted...] or ["dashboard"], "changed": {section: [keys]}, "version", "ts"}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue as queue_mod
import threading
import time
from typing import Callable

#: Front-end refresh scopes a Project-tree change invalidates.
SNAPSHOT_SCOPES: tuple[str, ...] = ("network", "profiles", "config")

#: Sections of the signature that participate in change detection.
WATCHED_SECTIONS: tuple[str, ...] = ("project_tree",)


def signature_hash(signature: dict) -> str:
    raw = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def changed_keys(before: dict, after: dict, section: str) -> list[str]:
    b = before.get(section) or {}
    a = after.get(section) or {}
    keys = sorted(set(b) | set(a))
    return [key for key in keys if b.get(key) != a.get(key)]


def event_from_signatures(before: dict | None, after: dict) -> dict:
    """Snapshot event when there is no previous signature, otherwise the diff."""
    scopes: set[str] = set()
    changed: dict[str, list[str]] = {}
    if before is None:
        return {
            "type": "state_snapshot",
            "scopes": list(SNAPSHOT_SCOPES),
            "changed": {},
            "version": signature_hash(after),
            "ts": int(time.time() * 1000),
        }

    for section in WATCHED_SECTIONS:
        keys = changed_keys(before, after, section)
        if keys:
            changed[section] = keys

    if "project_tree" in changed:
        scopes.update(SNAPSHOT_SCOPES)

    return {
        "type": "state_changed",
        "scopes": sorted(scopes) or ["dashboard"],
        "changed": changed,
        "version": signature_hash(after),
        "ts": int(time.time() * 1000),
    }


def sse_payload(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class DashboardEventHub:
    """Subscribers, last signature and the background watch loop.

    ``read_signature`` is called on the watcher thread (and, for a cold first
    frame, on a worker thread via ``asyncio.to_thread``), never on the event
    loop: reading the signature may touch the database.
    """

    def __init__(self, read_signature: Callable[[], dict]) -> None:
        self._read_signature = read_signature
        self.subscribers: set[queue_mod.Queue] = set()
        self.lock = threading.Lock()
        #: Signature the watcher saw last; ``None`` until the first scan.
        self.last_signature: dict | None = None

    def broadcast(self, event: dict) -> None:
        with self.lock:
            subscribers = list(self.subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue_mod.Full:
                try:
                    q.get_nowait()
                except queue_mod.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue_mod.Full:
                    pass

    def watch_loop(self, interval: float = 1.5) -> None:
        """Blocking poll loop for a daemon thread; never lets an error escape."""
        try:
            self.last_signature = self._read_signature()
        except Exception:  # noqa: BLE001 - 库暂不可读不该击穿事件流
            self.last_signature = None
        while True:
            time.sleep(interval)
            try:
                current = self._read_signature()
                if self.last_signature != current:
                    event = event_from_signatures(self.last_signature, current)
                    self.last_signature = current
                    self.broadcast(event)
            except Exception:  # noqa: BLE001 - 事件流不能击穿主服务
                pass

    def stream(self):
        """``GET /api/events``: a snapshot frame, padding, then live events with keepalives."""
        from fastapi.responses import StreamingResponse

        q: queue_mod.Queue = queue_mod.Queue(maxsize=64)
        with self.lock:
            self.subscribers.add(q)

        async def event_stream():
            try:
                snapshot = self.last_signature
                if snapshot is None:
                    snapshot = await asyncio.to_thread(self._read_signature)
                yield sse_payload(event_from_signatures(None, snapshot))
                # 首帧之后推一段填充注释，隧道/代理才会把前面的字节真的转出去
                # （注释行不进 EventSource 的 message 回调）。
                yield ": " + " " * 2048 + "\n\n"
                while True:
                    try:
                        event = await asyncio.to_thread(q.get, True, 15)
                    except queue_mod.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield sse_payload(event)
            finally:
                with self.lock:
                    self.subscribers.discard(q)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


__all__ = [
    "DashboardEventHub",
    "SNAPSHOT_SCOPES",
    "changed_keys",
    "event_from_signatures",
    "signature_hash",
    "sse_payload",
]
