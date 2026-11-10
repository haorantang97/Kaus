"""The screen sign shared by every Kaus process of this user (2026-11-10).

An Agent takes it before taking over the whole screen and puts it down when it
is done; waiters are served first come, first served. Kaus does not recognise
computer-use tools and holds nothing per turn: background work on one window
needs no sign, and an Agent that never takes it is not protected.

The sign is an ``flock`` on a private file, so it falls when the holder's
process exits. A hold also expires after ``hold_seconds`` unless renewed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # Importing the package stays possible on Windows.
    fcntl = None


class ResourceBusyError(TimeoutError):
    pass


def default_lease_root() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    # macOS app/terminal processes can have different TMPDIR values. Use the
    # common POSIX location so all Kaus processes for the user share one inode.
    base = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    return base / f"kaus-computer-use-{uid}"


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ResourceLease:
    """One holder at a time; ``acquire`` queues in arrival order."""

    def __init__(self, resource_id: str = "desktop:local", *, root: str | Path | None = None,
                 owner: dict | None = None, ticket_ttl: float = 120.0, poll_interval: float = 0.05):
        if not resource_id or not resource_id.startswith("desktop:"):
            raise ValueError("Invalid computer-use resource")
        if ticket_ttl <= 0 or poll_interval <= 0:
            raise ValueError("Invalid lease wait interval")
        self.resource_id = resource_id
        self.root = Path(root) if root is not None else default_lease_root()
        self.owner = dict(owner or {})
        self.ticket_ttl, self.poll_interval = ticket_ttl, poll_interval
        self.expires_at: float | None = None
        self._fd: int | None = None
        self._ticket: Path | None = None
        self._timer: threading.Timer | None = None
        self._guard = threading.RLock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def _stem(self) -> str:
        return hashlib.sha256(self.resource_id.encode()).hexdigest()

    @property
    def _queue(self) -> Path:
        return self.root / (self._stem + ".queue")

    @property
    def _holder_path(self) -> Path:
        return self.root / (self._stem + ".holder.json")

    def _private(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise PermissionError("Invalid computer-use lease directory")
        os.chmod(path, 0o700)

    def _open_lock(self) -> int:
        self._private(self.root)
        fd = os.open(self.root / (self._stem + ".lock"), os.O_CREAT | os.O_RDWR |
                     getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            os.close(fd)
            raise PermissionError("Invalid computer-use lease file")
        return fd

    def acquire(self, timeout: float | None = None, *, hold_seconds: float | None = None) -> bool:
        """Take the sign, waiting up to ``timeout`` seconds (``None``: until free).

        Returns ``False`` when the wait ran out; the place in the queue is kept
        for ``ticket_ttl`` seconds so the next call continues from it. Calling
        it while holding renews the hold.
        """
        if fcntl is None:
            raise RuntimeError("Computer-use resource locking is unavailable on this platform")
        with self._guard:
            if self.held:
                self._arm(hold_seconds)
                self._write_holder()
                return True
        started = time.monotonic()
        while True:
            with self._guard:
                if self.held:
                    return True
                if self._first_in_line() and self._try_lock():
                    self._leave_queue()
                    self._arm(hold_seconds)
                    self._write_holder()
                    return True
            if timeout is not None and time.monotonic() - started >= timeout:
                return False
            time.sleep(self.poll_interval)

    def _try_lock(self) -> bool:
        fd = self._open_lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                return False
            raise
        self._fd = fd
        return True

    def _first_in_line(self) -> bool:
        self._private(self._queue)
        if self._ticket is None:
            self._ticket = self._queue / f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            self._ticket.write_text(json.dumps({"pid": os.getpid()}))
        else:
            try:
                os.utime(self._ticket)
            except FileNotFoundError:
                # Swept as stale while we were away: rejoin at the back.
                self._ticket = None
                return self._first_in_line()
        return self._live_tickets()[0] == self._ticket

    def _live_tickets(self) -> list[Path]:
        live, now = [], time.time()
        for ticket in sorted(self._queue.iterdir()):
            try:
                pid = json.loads(ticket.read_text()).get("pid")
                stale = now - ticket.stat().st_mtime > self.ticket_ttl
            except (OSError, ValueError, AttributeError):
                continue
            if ticket != self._ticket and (stale or not _alive(pid)):
                ticket.unlink(missing_ok=True)
                continue
            live.append(ticket)
        return live

    def position(self) -> int:
        """1-based place in the queue; 0 when holding or not queued."""
        with self._guard:
            if self.held or self._ticket is None:
                return 0
            tickets = self._live_tickets()
            return tickets.index(self._ticket) + 1 if self._ticket in tickets else 0

    def _leave_queue(self) -> None:
        if self._ticket is not None:
            self._ticket.unlink(missing_ok=True)
            self._ticket = None

    def _arm(self, hold_seconds: float | None) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.expires_at = None
        if hold_seconds is not None:
            self.expires_at = time.time() + hold_seconds
            self._timer = threading.Timer(hold_seconds, self.release)
            self._timer.daemon = True
            self._timer.start()

    def _write_holder(self) -> None:
        value = {**self.owner, "pid": os.getpid(), "since": self._since(), "expiresAt": self.expires_at}
        temporary = self._holder_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(self._holder_path)

    def _since(self) -> float:
        try:
            current = json.loads(self._holder_path.read_text())
            if current.get("pid") == os.getpid() and isinstance(current.get("since"), (int, float)):
                return current["since"]
        except (OSError, ValueError, AttributeError):
            pass
        return time.time()

    def holder(self) -> dict | None:
        """Who holds the sign now, as the holder recorded it; ``None`` when free."""
        if fcntl is None:
            return None
        with self._guard:
            if not self.held:
                fd = self._open_lock()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return None
                except OSError:
                    pass
                finally:
                    os.close(fd)
            try:
                value = json.loads(self._holder_path.read_text())
            except (OSError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

    def release(self) -> None:
        """Put the sign down (and leave the queue). Safe to call at any time."""
        with self._guard:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.expires_at = None
            self._leave_queue()
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            try:
                current = json.loads(self._holder_path.read_text())
                if current.get("pid") == os.getpid():
                    self._holder_path.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass
            # Never unlink the lock file: old and new descriptors would lock
            # different inodes.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
