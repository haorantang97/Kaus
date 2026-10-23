"""One-use, same-user Unix-socket transfer of terminal environment overrides.

Only socket paths go into launch scripts. Values live in the launcher and child
process memory; this module is also the stdlib-only client entrypoint.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import struct
import sys
import tempfile

MAX_PAYLOAD = 512 * 1024
_REQUEST = b'KAUS-TERMINAL-ENV-1\n'
_PROTECTED = {'PATH', 'SHELL', 'SHLVL', 'PWD', 'OLDPWD', 'TERM', 'COLORTERM',
              'KAUS_LAUNCH_ID', 'KAUS_PID_FILE', 'KAUS_EXIT_FILE'}
_PROTECTED_PREFIXES = ('CMUX_', 'cmux_', 'TERM_', 'ITERM_', 'LC_TERMINAL')


def selected_environment(spec, passthrough=()):
    """Connection overrides plus declared inherited auth/provider keys only."""
    inherited = spec.build_env()
    values = {key: inherited[key] for key in passthrough if key in inherited}
    values.update(spec.env)
    return {key: value for key, value in values.items()
            if key not in _PROTECTED and not key.startswith(_PROTECTED_PREFIXES)}


def _peer_uid(sock):
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    # macOS/BSD expose getpeereid through libc, not Python's socket wrapper.
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, 'getpeereid', None)
    if fn is None:
        raise OSError('Peer credentials unavailable')
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if fn(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError('Peer credentials unavailable')
    return uid.value


def _validated(values):
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
        or (value is not None and (not isinstance(value, str) or '\0' in value))
        for key, value in values.items()
    ):
        raise ValueError('终端环境配置无效')
    return {key: value for key, value in values.items()
            if key not in _PROTECTED and not key.startswith(_PROTECTED_PREFIXES)}


class EnvironmentRelay:
    def __init__(self, values, *, timeout, on_close=None):
        self._values = _validated(dict(values))
        if len(json.dumps(self._values).encode()) > MAX_PAYLOAD:
            raise ValueError('终端环境配置过大')
        self.timeout = timeout
        self._on_close = on_close
        self.directory = None
        self.path = None
        self._server = None
        self._expiry = None
        self._closed = False
        self._claimed = False
        self.consumed = False
        self.done = asyncio.Event()
        self._writers = set()

    async def start(self):
        # A short private path also fits macOS sockaddr_un.sun_path (104 bytes).
        self.directory = Path(tempfile.mkdtemp(prefix='kaus-env-', dir='/tmp'))
        self.directory.chmod(0o700)
        self.path = self.directory / 'env.sock'
        try:
            self._server = await asyncio.start_unix_server(self._receive, path=str(self.path), limit=128)
            self.path.chmod(0o600)
            self._expiry = asyncio.create_task(self._expire())
        except BaseException:
            await self.aclose()
            raise
        return self

    async def _expire(self):
        await asyncio.sleep(self.timeout)
        await self.aclose()

    async def _receive(self, reader, writer):
        self._writers.add(writer)
        owns_claim = False
        try:
            sock = writer.get_extra_info('socket')
            if _peer_uid(sock) != os.getuid() or await asyncio.wait_for(reader.readline(), 2) != _REQUEST:
                return
            if self._closed or self._claimed:
                return
            self._claimed = True
            owns_claim = True
            self._server.close()
            payload = json.dumps(self._values, ensure_ascii=True).encode()
            writer.write(struct.pack('!I', len(payload)) + payload)
            await asyncio.wait_for(writer.drain(), 3)
            self.consumed = True
        except (OSError, ValueError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            if owns_claim:
                await self.aclose()

    async def wait(self):
        await self.done.wait()
        return self.consumed

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._values.clear()
        for writer in tuple(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._expiry and self._expiry is not asyncio.current_task():
            self._expiry.cancel()
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.done.set()
        if self._on_close:
            self._on_close()

    def command(self, command):
        return (sys.executable, str(Path(__file__).resolve()), str(self.path), *command)


def _read_exact(sock, size):
    output = bytearray()
    while len(output) < size:
        block = sock.recv(size - len(output))
        if not block:
            raise ValueError('Environment transfer closed')
        output.extend(block)
    return bytes(output)


def receive(path):
    path = Path(path)
    parent, endpoint = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
            or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
        raise ValueError('Environment endpoint unavailable')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        if _peer_uid(client) != os.getuid():
            raise ValueError('Environment endpoint unavailable')
        client.sendall(_REQUEST)
        length = struct.unpack('!I', _read_exact(client, 4))[0]
        if length > MAX_PAYLOAD:
            raise ValueError('Environment transfer too large')
        return _validated(json.loads(_read_exact(client, length)))


def main(argv):
    if len(argv) < 2:
        return 125
    try:
        values = receive(argv[0])
        environment = dict(os.environ)
        for key, value in values.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        values.clear()
        os.execvpe(argv[1], argv[1:], environment)
    except (OSError, ValueError, TypeError):
        print('终端环境已失效，请从 Kaus 重新打开。', file=sys.stderr)
        return 125


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
