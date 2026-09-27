"""Bounded native NDJSON transport (ZCode frames do not have jsonrpc)."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque

from drivers.error_text import safe_error_text, safe_exception_message
from drivers.stdio_bridge import BridgeError, MAX_FRAME


class Native:
    def __init__(self, command: tuple[str, ...], notify, request, disconnected, *, env=None, cwd=None):
        self.command, self.notify, self.request = command, notify, request
        self.disconnected = disconnected
        self.env, self.cwd = env, cwd
        self.process = None
        self.pending = {}
        self.counter = 0
        self.tasks = set()
        self.stderr = deque(maxlen=12)
        self.closed = False

    async def start(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME, env=self.env, cwd=self.cwd,
            )
        except OSError as exc:
            raise BridgeError('ZCode could not start: ' + safe_error_text(str(exc))) from None
        self.reader = asyncio.create_task(self.read())
        self.drainer = asyncio.create_task(self.drain())

    async def drain(self):
        while line := await self.process.stderr.readline():
            self.stderr.append(safe_error_text(line.decode(errors='replace').strip(), limit=500))

    def send(self, frame):
        if self.closed or not self.process or self.process.returncode is not None:
            raise BridgeError('ZCode transport is disconnected')
        wire = (json.dumps(frame, ensure_ascii=False) + '\n').encode()
        if len(wire) > MAX_FRAME:
            raise BridgeError('Native request exceeds frame limit', -32602)
        self.process.stdin.write(wire)

    async def call(self, method, params, timeout=60):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            self.send({'id': ident, 'method': method, 'params': params})
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise BridgeError('ZCode did not answer ' + method + ' in time') from None
        finally:
            self.pending.pop(ident, None)

    async def answer(self, frame):
        try:
            result = await self.request(frame['method'], frame.get('params') or {})
            self.send({'id': frame['id'], 'result': result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                self.send({'id': frame['id'], 'error': {
                    'code': exc.code if isinstance(exc, BridgeError) else -32603,
                    'message': safe_error_text(str(exc)),
                }})

    async def read(self):
        error = BridgeError('ZCode transport closed before the turn completed')
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError('Native frame must be an object')
                if 'method' in frame:
                    if 'id' in frame:
                        task = asyncio.create_task(self.answer(frame))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    else:
                        self.notify(frame['method'], frame.get('params') or {})
                else:
                    future = self.pending.get(frame.get('id'))
                    if future is None or future.done():
                        continue
                    if frame.get('error'):
                        native_error = frame['error']
                        exc = BridgeError(native_error.get('message') or 'ZCode operation failed', native_error.get('code', -32603))
                        exc.data = native_error.get('data')
                        future.set_exception(BridgeError(safe_exception_message(exc), exc.code))
                    else:
                        future.set_result(frame.get('result') or {})
        except asyncio.CancelledError:
            return
        except (ValueError, OSError, KeyError, TypeError) as exc:
            error = BridgeError('Invalid ZCode transport: ' + safe_error_text(str(exc)))
        finally:
            self.closed = True
            detail = next((row for row in reversed(self.stderr) if 'error' in row.lower() or 'failed' in row.lower()), None)
            if detail:
                error = BridgeError(str(error) + ': ' + detail)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            for task in self.tasks:
                task.cancel()
            self.disconnected(error)

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        for name in ('reader', 'drainer'):
            task = getattr(self, name, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await asyncio.gather(*self.tasks, return_exceptions=True)
