"""Workspace terminal: a plain shell in a conversation's working directory.

2026-11-23「页头面板」⑥ (product-decisions): the side panel's Terminal is the same
kind of thing as the reference desktop apps' terminal: a login shell in the project
folder. It does not take over the conversation and never touches the native
session; moving the conversation itself to a terminal stays with 「到终端继续」.

One shell per conversation. Closing the panel only detaches the viewer: the shell
keeps running and the next viewer gets the recent output replayed. A shell ends
when it exits, when nobody has been attached for :data:`IDLE_SECONDS`, or with the
kernel (closing the PTY master hangs up the shell even if the kernel dies).

POSIX only; stdlib only.
"""
from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import pwd
import signal
import struct
import subprocess
import sys
import termios
from dataclasses import dataclass, field
from typing import Callable

LOGGER = logging.getLogger(__name__)

#: Output kept for a viewer that attaches later.
REPLAY_LIMIT = 256 * 1024
#: A shell nobody has looked at for this long is ended.
IDLE_SECONDS = 30 * 60
#: At most this many shells at once; the longest-detached one makes room.
MAX_SESSIONS = 8
_READ_CHUNK = 64 * 1024
#: Kaus's own process settings are not the user's shell environment.
_DROPPED_PREFIXES = ("KAUS_", "DASH_")
#: Runs in the child before the shell: make the PTY its controlling terminal, then
#: exec a login shell. Spawned through ``subprocess`` (fork-safe in a threaded server).
_EXEC_SHELL = (
    "import fcntl,os,sys,termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "shell = sys.argv[1]\n"
    "os.execv(shell, ['-' + os.path.basename(shell)])\n"
)


def default_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if shell and os.path.isfile(shell) and os.access(shell, os.X_OK):
        return shell
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    return shell if shell and os.path.isfile(shell) else "/bin/sh"


def shell_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if not key.startswith(_DROPPED_PREFIXES)}
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "Kaus"})
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@dataclass(eq=False)
class TerminalSession:
    key: str
    cwd: str
    pid: int
    master: int
    buffer: bytearray = field(default_factory=bytearray)
    viewers: set[asyncio.Queue] = field(default_factory=set)
    exit_code: int | None = None
    detached_at: float | None = None
    process: subprocess.Popen | None = None
    _pending: bytearray = field(default_factory=bytearray)
    _idle: asyncio.TimerHandle | None = None

    @property
    def alive(self) -> bool:
        return self.exit_code is None

    def write(self, data: bytes) -> None:
        if not self.alive or not data:
            return
        idle = not self._pending
        self._pending += data
        if idle:
            self._flush()

    def _flush(self) -> None:
        loop = asyncio.get_running_loop()
        while self._pending:
            try:
                written = os.write(self.master, self._pending)
            except BlockingIOError:
                # The shell is not reading yet; continue when it can take more.
                loop.add_writer(self.master, self._on_writable)
                return
            except OSError:
                self._pending.clear()
                break
            del self._pending[:written]
        try:
            loop.remove_writer(self.master)
        except (ValueError, OSError):
            pass

    def _on_writable(self) -> None:
        self._flush()

    def resize(self, cols: int, rows: int) -> None:
        if self.alive and 1 <= cols <= 1000 and 1 <= rows <= 1000:
            try:
                _set_size(self.master, cols, rows)
            except OSError:
                pass


class TerminalManager:
    """Owns the workspace shells of one kernel process."""

    def __init__(self, *, shell: Callable[[], str] = default_shell, idle_seconds: float = IDLE_SECONDS,
                 max_sessions: int = MAX_SESSIONS) -> None:
        self._shell = shell
        self._idle_seconds = idle_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, TerminalSession] = {}
        atexit.register(self.close_all)

    def get(self, key: str) -> TerminalSession | None:
        return self._sessions.get(key)

    def ensure(self, key: str, cwd: str, *, cols: int = 80, rows: int = 24) -> TerminalSession:
        """The live shell for ``key``, started in ``cwd`` when there is none."""
        session = self._sessions.get(key)
        if session is not None and session.alive and session.cwd == cwd:
            return session
        if session is not None:
            self.close(key)
        self._make_room()
        session = self._spawn(key, cwd, cols, rows)
        self._sessions[key] = session
        return session

    def attach(self, session: TerminalSession) -> tuple[asyncio.Queue, bytes]:
        queue: asyncio.Queue = asyncio.Queue()
        session.viewers.add(queue)
        session.detached_at = None
        if session._idle is not None:
            session._idle.cancel()
            session._idle = None
        return queue, bytes(session.buffer)

    def detach(self, session: TerminalSession, queue: asyncio.Queue) -> None:
        session.viewers.discard(queue)
        if session.viewers or not session.alive:
            return
        loop = asyncio.get_running_loop()
        session.detached_at = loop.time()
        session._idle = loop.call_later(self._idle_seconds, self._expire, session)

    def close(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        if session._idle is not None:
            session._idle.cancel()
        self._stop_reading(session)
        session._pending.clear()
        if session.alive:
            try:
                os.killpg(session.pid, signal.SIGHUP)
            except OSError:
                pass
        self._close_master(session)
        if session.alive:
            self._reap(session, block=False)
        self._broadcast(session, None)

    def close_all(self) -> None:
        for key in list(self._sessions):
            self.close(key)

    # --- internals ---------------------------------------------------------- #

    def _spawn(self, key: str, cwd: str, cols: int, rows: int) -> TerminalSession:
        shell = self._shell()
        env = shell_environment()
        env["PWD"] = cwd
        master, slave = os.openpty()
        try:
            _set_size(slave, max(cols, 2), max(rows, 2))
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", _EXEC_SHELL, shell],
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                start_new_session=True, close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        session = TerminalSession(key=key, cwd=cwd, pid=process.pid, master=master, process=process)
        asyncio.get_running_loop().add_reader(master, self._on_readable, session)
        return session

    def _on_readable(self, session: TerminalSession) -> None:
        try:
            data = os.read(session.master, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._on_exit(session)
            return
        session.buffer += data
        if len(session.buffer) > REPLAY_LIMIT:
            del session.buffer[: len(session.buffer) - REPLAY_LIMIT]
        self._broadcast(session, data)

    def _on_exit(self, session: TerminalSession) -> None:
        self._stop_reading(session)
        self._reap(session, block=True)
        self._close_master(session)
        if self._sessions.get(session.key) is session:
            del self._sessions[session.key]
        self._broadcast(session, None)

    def _reap(self, session: TerminalSession, *, block: bool) -> None:
        process = session.process
        if process is None:
            return
        try:
            code = process.wait(timeout=2 if block else 0)
        except subprocess.TimeoutExpired:
            # Hung up but not gone yet: collect it in the background.
            session.exit_code = -1
            try:
                asyncio.get_running_loop().run_in_executor(None, process.wait)
            except RuntimeError:  # interpreter exit: the hangup is enough
                pass
            return
        session.exit_code = code

    def _stop_reading(self, session: TerminalSession) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(session.master)
            loop.remove_writer(session.master)
        except (RuntimeError, ValueError, OSError):
            pass

    @staticmethod
    def _close_master(session: TerminalSession) -> None:
        try:
            os.close(session.master)
        except OSError:
            pass

    @staticmethod
    def _broadcast(session: TerminalSession, data: bytes | None) -> None:
        for queue in list(session.viewers):
            queue.put_nowait(data)

    def _expire(self, session: TerminalSession) -> None:
        if self._sessions.get(session.key) is session and not session.viewers:
            LOGGER.info("Workspace terminal %s ended after idling", session.key)
            self.close(session.key)

    def _make_room(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        detached = [s for s in self._sessions.values() if not s.viewers]
        victim = min(detached or self._sessions.values(), key=lambda s: s.detached_at or 0.0)
        self.close(victim.key)


__all__ = ["IDLE_SECONDS", "REPLAY_LIMIT", "TerminalManager", "TerminalSession", "default_shell", "shell_environment"]
