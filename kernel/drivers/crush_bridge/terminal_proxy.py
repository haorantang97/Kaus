"""Original local HTTP forwarding for native TUI configuration ownership.

Responses, events, approvals and tools pass through unchanged. Only config writes
are limited to the existing private workspace; the TUI's automatic first model
restore cannot replace the model explicitly selected for this handoff.
"""
import asyncio
import contextlib
import json
from urllib.parse import urlsplit

from .transport import MAX_BODY


class TerminalProxy:
    def __init__(self, upstream, workspace_id, current_model, previous_model=None):
        self.upstream = upstream
        self.base = '/v1/workspaces/' + workspace_id
        self.current_model = dict(current_model)
        self.previous_model = previous_model
        self.restore_pending = previous_model is not None
        self.tasks = set()
        self.server = None

    async def start(self, path):
        self.server = await asyncio.start_unix_server(self.handle, path=path, limit=65536)

    def rewrite(self, method, target, data):
        endpoint = urlsplit(target).path
        if method != 'POST' or endpoint not in (self.base + '/config/model', self.base + '/config/set'):
            return data
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('Invalid config request')
        if endpoint.endswith('/config/model') and self.restore_pending and value.get('scope') == 0 and value.get('model_type') == 'large':
            model = value.get('model') or {}
            if (model.get('provider'), model.get('model')) == self.previous_model:
                value['model'] = self.current_model
                self.restore_pending = False
        value['scope'] = 1
        return json.dumps(value).encode()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_writer = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            lines = header.decode('latin1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if not target.startswith('/') or target.startswith('//'):
                raise ValueError('Invalid local request')
            pairs = [line.split(':', 1) for line in lines[1:] if line]
            headers = {key.lower(): value.strip() for key, value in pairs}
            if 'transfer-encoding' in headers:
                raise ValueError('Unsupported request encoding')
            size = int(headers.get('content-length', '0'))
            if size < 0 or size > MAX_BODY:
                raise ValueError('Request too large')
            body = self.rewrite(method, target, await asyncio.wait_for(reader.readexactly(size), 10))
            forwarded = [(key, value.strip()) for key, value in pairs if key.lower() not in ('content-length', 'connection')]
            if body or 'content-length' in headers:
                forwarded.append(('Content-Length', str(len(body))))
            forwarded.append(('Connection', 'close'))
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(self.upstream)
            upstream_writer.write((f'{method} {target} {version}\r\n' + ''.join(f'{key}: {value}\r\n' for key, value in forwarded) + '\r\n').encode('latin1') + body)
            await upstream_writer.drain()
            while data := await upstream_reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # No native error bodies or credential-bearing config are logged.
            with contextlib.suppress(OSError):
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
        finally:
            for stream in (writer, upstream_writer):
                if stream:
                    stream.close()
                    with contextlib.suppress(OSError):
                        await stream.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
