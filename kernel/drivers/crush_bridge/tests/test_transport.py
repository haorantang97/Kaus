import asyncio
import contextlib
import json
from pathlib import Path
import tempfile

import pytest

from drivers.crush_bridge.protocol import BridgeError
from drivers.crush_bridge.transport import NativeHTTP


@contextlib.asynccontextmanager
async def endpoint(handler):
    # Keep the socket path below macOS's sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="kaus-crush-test-", dir="/tmp") as directory:
        path = str(Path(directory) / "server.sock")
        server = await asyncio.start_unix_server(handler, path=path)
        client = NativeHTTP(path)
        try:
            yield client
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()


async def read_request(reader):
    headers = await reader.readuntil(b"\r\n\r\n")
    size = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                 if line.lower().startswith(b"content-length:")), 0)
    body = await reader.readexactly(size) if size else b""
    return headers, body


async def test_real_unix_http_json_request_and_empty_health():
    requests = []

    async def handler(reader, writer):
        headers, body = await read_request(reader)
        requests.append((headers, body))
        payload = b'{"ok":true}' if body else b""
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with endpoint(handler) as client:
        assert await client.request("GET", "/v1/health") == {}
        assert await client.request("POST", "/v1/workspaces", {"path": "/workspace 中文"}) == {"ok": True}
    assert json.loads(requests[1][1]) == {"path": "/workspace 中文"}


async def test_real_chunked_sse_multiline_json_and_partial_network_reads():
    expected = {"type": "message", "payload": {"text": "你好"}}

    async def handler(reader, writer):
        await read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        data = ': keepalive\r\ndata: {"type":"message",\r\ndata: "payload":{"text":"你好"}}\r\n\r\n'.encode()
        for start in range(0, len(data), 7):
            chunk = data[start:start + 7]
            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            await writer.drain()
            await asyncio.sleep(0)
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with endpoint(handler) as client:
        ready = asyncio.Event()
        frames = [frame async for frame in client.events("/events", ready.set)]
        assert ready.is_set()
        assert frames == [expected]


async def test_native_http_failure_does_not_expose_provider_error_body():
    async def handler(reader, writer):
        await read_request(reader)
        payload = b'{"api_key":"secret-provider-value"}'
        writer.write(b"HTTP/1.1 500 Internal Error\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with endpoint(handler) as client:
        with pytest.raises(BridgeError, match="HTTP 500") as error:
            await client.request("GET", "/config")
        assert "secret" not in str(error.value)


async def test_close_unblocks_an_idle_sse_reader():
    disconnected = asyncio.Event()

    async def handler(reader, writer):
        await read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        await reader.read()
        disconnected.set()
        writer.close()
        await writer.wait_closed()

    async with endpoint(handler) as client:
        ready = asyncio.Event()

        async def consume():
            with contextlib.suppress(Exception):
                async for _frame in client.events("/events", ready.set):
                    pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 1)
        await client.aclose()
        await asyncio.wait_for(task, 1)
        await asyncio.wait_for(disconnected.wait(), 1)
