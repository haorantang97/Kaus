"""Durable ownership for calls dispatched to Kaus's persistent CuaDriver.

An MCP child exiting cannot prove its daemon finished an action. Record the
actual Unix peer and caller before dispatch, and clear only on a returned RPC
or confirmed daemon exit. Receipts contain no tool arguments or screen data.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

RECEIPT = "cua-inflight.json"


def _root(root=None):
    if root is None:
        from .lease import default_lease_root
        root = default_lease_root()
    return Path(root)


def _private_directory(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("Invalid computer-use resource directory")
    root.chmod(0o700)


def _lock_fd(root, name):
    if fcntl is None:
        raise OSError("Desktop action locking unavailable")
    _private_directory(root)
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise OSError("Invalid computer-use lock")
    return fd


@contextmanager
def _state_lock(root, *, blocking=False):
    fd = _lock_fd(root, "cua-state.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def _read(root):
    try:
        fd = os.open(root / RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 16384:
            raise ValueError("Invalid desktop action receipt")
        value = json.load(stream)
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("id"), str)
            or not all(isinstance(value.get(key), dict) for key in ("daemon", "caller"))
            or not all(isinstance(value.get(key), str) for key in ("socket", "executable"))):
        raise ValueError("Invalid desktop action receipt")
    for key in ("daemon", "caller"):
        if (not isinstance(value[key].get("pid"), int) or value[key]["pid"] <= 1
                or value[key].get("uid") != os.getuid()
                or not isinstance(value[key].get("started"), str)):
            raise ValueError("Invalid desktop action identity")
    return value


def _write(root, value):
    fd, temporary = tempfile.mkstemp(prefix="cua-action-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / RECEIPT)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "uid=,pgid=,lstart=,stat="],
                            capture_output=True, text=True, timeout=3)
    fields = result.stdout.strip().split()
    if result.returncode == 0 and len(fields) >= 8:
        if fields[-1].startswith("Z"):
            return None
        return {"pid": pid, "uid": int(fields[0]), "pgid": int(fields[1]),
                "started": " ".join(fields[2:-1])}
    # A failed process query is not proof of exit.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    raise OSError("Desktop process identity unavailable")


def _peer_identity(endpoint):
    endpoint = Path(endpoint)
    parent = endpoint.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise OSError("Desktop host identity unavailable")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(endpoint))
        if sys.platform == "darwin":
            # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=0x002.
            pid = struct.unpack("i", connection.getsockopt(0, 0x002, 4))[0]
        elif hasattr(socket, "SO_PEERCRED"):
            pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise OSError("Desktop host identity unavailable")
        else:
            raise OSError("Desktop host identity unavailable")
    identity = _identity(pid)
    if identity is None or identity["uid"] != os.getuid():
        raise OSError("Desktop host identity unavailable")
    return identity


def _owned_daemon(value):
    endpoint = Path(value["socket"])
    # This is the installer's private host, never /tmp/cua-driver.sock or a
    # user-supplied desktop service. The current process command must match too.
    if (endpoint.name != "driver.sock" or str(endpoint.parent.parent) != "/tmp"
            or not re.fullmatch(rf"kaus-cua-{os.getuid()}-[0-9a-f]{{20}}", endpoint.parent.name)):
        return False
    executable = value["executable"]
    if not executable.endswith("/CuaDriver.app/Contents/MacOS/cua-driver"):
        return False
    result = subprocess.run(["/bin/ps", "-p", str(value["daemon"]["pid"]), "-o", "command="],
                            capture_output=True, text=True, timeout=3)
    command = result.stdout.strip()
    return result.returncode == 0 and command == (
        f"{executable} serve --socket {endpoint} --no-permissions-gate --no-overlay")


def _stop_orphan(value):
    identity = value["daemon"]
    if not _owned_daemon(value):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if _identity(identity["pid"]) != identity:
            return True
        # Recheck command as well as PID incarnation before each signal.
        if not _owned_daemon(value):
            return False
        try:
            os.kill(identity["pid"], sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline:
            if _identity(identity["pid"]) != identity:
                return True
            time.sleep(.025)
    return _identity(identity["pid"]) != identity


def reconcile(root=None, *, operation_lock_held=False):
    """True only when no desktop RPC is outstanding; safe after host restart."""
    root = _root(root)
    if not root.exists():
        return True
    if fcntl is None:
        # No managed desktop call can be dispatched on this platform. Keep
        # discovery/default-off settings usable for other native capabilities.
        return not (root / RECEIPT).exists()
    serial = None
    try:
        if not operation_lock_held:
            serial = _lock_fd(root, "cua-operations.lock")
            # A live operation (or a call preparing dispatch) retains control.
            # Never stop its daemon merely because another RPC caller exited.
            fcntl.flock(serial, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _state_lock(root):
            value = _read(root)
            if value is None:
                return True
            daemon = _identity(value["daemon"]["pid"])
            if daemon == value["daemon"]:
                if _identity(value["caller"]["pid"]) == value["caller"]:
                    return False
                if not _stop_orphan(value):
                    return False
            # Same-state lock is held across verification and deletion. A new
            # action cannot replace this receipt while the old host is stopped.
            (root / RECEIPT).unlink()
            return True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False
    finally:
        if serial is not None:
            os.close(serial)


class Operation:
    """Serialize a desktop call and persist ownership until its RPC returns."""
    def __init__(self, executable, endpoint, *, root=None, prepare=None):
        self.root = _root(root)
        self.executable, self.endpoint = os.fspath(executable), os.fspath(endpoint)
        self.fd = None
        self.receipt_id = uuid.uuid4().hex
        self.dispatched = False
        self.prepare = prepare

    async def __aenter__(self):
        self.fd = _lock_fd(self.root, "cua-operations.lock")
        try:
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    await asyncio.sleep(.025)
            if not await asyncio.to_thread(reconcile, self.root, operation_lock_held=True):
                raise RuntimeError("Desktop action still pending")
            if self.prepare is not None:
                # Reconciliation may just have retired the old daemon. Prepare
                # its replacement before recording or dispatching a new RPC.
                await self.prepare()
            daemon = await asyncio.to_thread(_peer_identity, self.endpoint)
            caller = await asyncio.to_thread(_identity, os.getpid())
            if caller is None:
                raise RuntimeError("Desktop caller identity unavailable")
            with _state_lock(self.root, blocking=True):
                if _read(self.root) is not None:
                    raise RuntimeError("Desktop action still pending")
                _write(self.root, {"version": 1, "id": self.receipt_id,
                    "daemon": daemon, "caller": caller,
                    "socket": self.endpoint, "executable": self.executable})
            self.dispatched = True
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    async def __aexit__(self, exc_type, *_):
        try:
            if exc_type is None:
                # No await between the acknowledged result and receipt removal.
                with _state_lock(self.root, blocking=True):
                    value = _read(self.root)
                    if value is not None and value["id"] == self.receipt_id:
                        (self.root / RECEIPT).unlink()
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
