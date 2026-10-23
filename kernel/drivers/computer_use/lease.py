"""Cooperative per-user computer resource leases shared by Kaus processes.

Locks cover a whole agent turn, not individual clicks. They coordinate Kaus
runtimes; external apps or unmanaged CLIs must opt in to participate.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Callable

try:
    import fcntl
except ImportError:  # Importing capability discovery remains possible on Windows.
    fcntl = None


class ResourceBusyError(TimeoutError):
    pass


def default_lease_root() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    # macOS app/terminal processes can have different TMPDIR values. Use the
    # common POSIX location so all Kaus processes for the user share one inode.
    base = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    return base / f"kaus-computer-use-{uid}"


class ResourceLease:
    def __init__(self, resource_id: str, *, root: str | Path | None = None,
                 timeout: float | None = None, poll_interval: float = 0.05,
                 on_waiting: Callable[[], None] | None = None):
        if not resource_id or not resource_id.startswith(("desktop:", "browser:")):
            raise ValueError("Invalid computer-use resource")
        if timeout is not None and timeout < 0 or poll_interval <= 0:
            raise ValueError("Invalid lease wait interval")
        self.resource_id = resource_id
        self.root = Path(root) if root is not None else default_lease_root()
        self.timeout, self.poll_interval = timeout, poll_interval
        self.on_waiting = on_waiting
        self._fd: int | None = None
        self._acquiring = False
        self._card_reservation_id: str | None = None
        self.waiting = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def reservation_path(self) -> Path:
        return self.root / (hashlib.sha256(self.resource_id.encode()).hexdigest() + ".external.json")

    def reserve_external(self, launch_id: str, directory: Path, *, remote: bool = False) -> None:
        """Persist ownership before the existing terminal launcher starts a process."""
        if not self.held:
            raise RuntimeError("An external reservation requires the resource lease")
        from drivers.terminals.managed_process import process_identity
        owner = process_identity(os.getpid())
        if owner is None:
            raise RuntimeError("Cannot establish terminal reservation ownership")
        value = {"launchId": launch_id, "directory": str(directory.resolve()),
                 "owner": owner, "remote": remote}
        temporary = self.reservation_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(self.reservation_path)

    def reserve_card(self, owner_id: str, reference: dict, *, transport: dict | None = None) -> None:
        """Persist a remote turn before prompt dispatch, without runtime secrets."""
        if not self.held:
            raise RuntimeError("A card reservation requires the resource lease")
        from drivers.terminals.managed_process import process_identity
        owner = process_identity(os.getpid())
        if owner is None:
            raise RuntimeError("Cannot establish card reservation ownership")
        value = {"kind": "card", "launchId": owner_id, "directory": str(self.root.resolve()),
                 "resource": self.resource_id, "remote": True, "owner": owner,
                 "transport": transport, "reference": reference, "phase": "prepared"}
        self._write_card_reservation(value)
        self._card_reservation_id = owner_id

    def _write_card_reservation(self, value):
        temporary = self.reservation_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.reservation_path)

    def mark_card_dispatched(self) -> None:
        if self._card_reservation_id is None:
            return
        value = json.loads(self.reservation_path.read_text())
        if value.get("launchId") != self._card_reservation_id:
            raise RuntimeError("Card reservation ownership changed")
        self._write_card_reservation({**value, "phase": "dispatched"})

    async def clear_external(self, launch_id: str) -> None:
        """Called only after the launch was revoked or its tools were stopped."""
        try:
            if json.loads(self.reservation_path.read_text()).get("launchId") != launch_id:
                return
        except FileNotFoundError:
            return
        await self.acquire(_ignore_reservation=True)
        try:
            try:
                value = json.loads(self.reservation_path.read_text())
            except FileNotFoundError:
                return
            if value.get("launchId") == launch_id:
                self.reservation_path.unlink(missing_ok=True)
        finally:
            self.release()

    async def acquire(self, *, _ignore_reservation: bool = False) -> "ResourceLease":
        if self.held or self._acquiring:
            raise RuntimeError("Resource lease already acquired or pending")
        if fcntl is None:
            raise RuntimeError("Computer-use resource locking is unavailable on this platform")
        self._acquiring = True
        fd = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or (
                hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                raise PermissionError("Invalid computer-use lease directory")
            os.chmod(self.root, 0o700)
            name = hashlib.sha256(self.resource_id.encode()).hexdigest() + ".lock"
            fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR |
                         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (
                hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                raise PermissionError("Invalid computer-use lease file")
            started = time.monotonic()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    from .cua_guard import reconcile as reconcile_desktop
                    desktop_idle = (_ignore_reservation or self.resource_id != "desktop:local"
                                    or await asyncio.to_thread(reconcile_desktop, self.root))
                    if desktop_idle and (_ignore_reservation or not await asyncio.to_thread(_external_active, self.reservation_path)):
                        self._fd, fd = fd, None
                        return self
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError as exc:
                    if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                        raise
                if not self.waiting:
                    self.waiting = True
                    if self.on_waiting is not None:
                        self.on_waiting()
                if self.timeout is not None and time.monotonic() - started >= self.timeout:
                    raise ResourceBusyError("Computer-use resource is in use")
                await asyncio.sleep(self.poll_interval)
        finally:
            if fd is not None:
                os.close(fd)
            self._acquiring = False
            self.waiting = False

    def release(self) -> None:
        # No await: cancellation cannot interrupt ownership release. Never unlink
        # lock files, otherwise old/new descriptors could lock different inodes.
        if self._fd is not None:
            if self._card_reservation_id is not None:
                try:
                    value = json.loads(self.reservation_path.read_text())
                    if value.get("kind") == "card" and value.get("launchId") == self._card_reservation_id:
                        self.reservation_path.unlink()
                except FileNotFoundError:
                    pass
                self._card_reservation_id = None
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    async def __aenter__(self) -> "ResourceLease":
        return await self.acquire()

    async def __aexit__(self, *_: object) -> None:
        self.release()


def _external_active(path: Path) -> bool:
    """A server restart cannot release a still-running native terminal's tools."""
    from drivers.terminals.managed_process import process_identity, _group_active, owned_children_active
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    try:
        directory = Path(value["directory"])
        if value.get("remote"):
            # Local CLI exit cannot establish remote task completion. The
            # existing surface monitor retries the driver's stop confirmation.
            return True
        process = directory / "process.json"
        if process.exists():
            identity = json.loads(process.read_text())
            if identity["uid"] != os.getuid() or identity["pid"] != identity["pgid"]:
                return True
            current = process_identity(identity["pid"])
            if current is not None and current != identity:
                # PID reuse must not transfer this reservation to an unrelated
                # process. A surviving original group remains separately visible.
                return _group_active(identity["pgid"]) or owned_children_active(directory)
            active = _group_active(identity["pgid"])
        elif (directory / "revoked").exists():
            active = False
        elif process_identity(value["owner"]["pid"]) == value["owner"]:
            active = True
        else:
            # The backend died before the native entrypoint claimed the launch.
            # Revoke first: a delayed terminal must not start after we unlock.
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (directory / "revoked").touch(mode=0o600)
            if process.exists():
                identity = json.loads(process.read_text())
                active = _group_active(identity["pgid"])
            else:
                active = False
        return active or owned_children_active(directory)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return True
