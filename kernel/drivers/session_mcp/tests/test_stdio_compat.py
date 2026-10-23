"""Small pipe chunks must not make the SDK repeatedly scan long image lines."""
import asyncio
from collections import deque
import json

import anyio
from anyio.streams.text import TextReceiveStream
from mcp.client import stdio
import pytest

from drivers.session_mcp import stdio_compat


class Chunks:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.reads = 0
        self.closed = False

    async def receive(self):
        self.reads += 1
        if not self.chunks:
            raise anyio.EndOfStream
        return self.chunks.popleft()

    async def aclose(self):
        self.closed = True


async def sdk_lines(stream):
    # The upstream reader remains unchanged; exercise its exact delimiter logic.
    pending = ""
    result = []
    async for chunk in stream:
        lines = (pending + chunk).split("\n")
        pending = lines.pop()
        result.extend(lines)
    return result


@pytest.mark.parametrize("chunks", [
    [b'{"id":1}\n{"id":2}\n'],
    [b'{"id":', b'1}', b'\n', b'{"id":2}', b'\n'],
    [b'{"id":1}\r', b'\n{"id":2}\r', b'\n'],
    [b'{"id":1}\n{"id":', b'2}\n{"id":3}\npartial'],
])
async def test_chunk_boundaries_and_multiple_messages_match_sdk(chunks):
    expected = await sdk_lines(TextReceiveStream(Chunks(chunks)))
    actual = await sdk_lines(stdio_compat._LineBufferedTextReceiveStream(Chunks(chunks)))
    assert actual == expected
    assert [json.loads(line)["id"] for line in actual] == list(range(1, len(actual) + 1))


async def test_unicode_split_at_every_byte_preserves_text_and_crlf():
    data = json.dumps({"text": "图片🦜é"}, ensure_ascii=False).encode() + b"\r\n"
    stream = stdio_compat._LineBufferedTextReceiveStream(Chunks(bytes([b]) for b in data))
    assert [json.loads(line) for line in await sdk_lines(stream)] == [{"text": "图片🦜é"}]


async def test_large_image_reaches_sdk_in_one_complete_chunk():
    frame = b'{"data":"' + b"A" * (4 * 1024 * 1024) + b'"}\n'
    source = Chunks(frame[i:i + 512] for i in range(0, len(frame), 512))
    stream = stdio_compat._LineBufferedTextReceiveStream(source)
    received = [chunk async for chunk in stream]
    assert len(received) == 1  # The SDK scans the large line only once.
    assert received[0].encode() == frame
    assert source.reads == (len(frame) + 511) // 512 + 1


async def test_partial_line_does_not_survive_eof_and_close_is_forwarded():
    source = Chunks([b'{"partial":'])
    stream = stdio_compat._LineBufferedTextReceiveStream(source)
    assert await sdk_lines(stream) == []
    await stream.aclose()
    assert source.closed


async def test_cancellation_interrupts_incomplete_frame_without_read_ahead():
    blocked = asyncio.Event()
    cancelled = asyncio.Event()

    class Waiting(Chunks):
        async def receive(self):
            if self.chunks:
                return await super().receive()
            blocked.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    source = Waiting([b'{"partial":'])
    stream = stdio_compat._LineBufferedTextReceiveStream(source)
    task = asyncio.create_task(stream.receive())
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    await stream.aclose()
    assert source.closed


@pytest.fixture
def factory(monkeypatch):
    stdio_compat._transport_factory.cache_clear()
    monkeypatch.setattr(stdio_compat, "version", lambda name: "2.3.0")
    yield
    stdio_compat._transport_factory.cache_clear()


def test_compat_keeps_installed_sdk_and_lifecycle_code_unchanged(factory):
    original = stdio.stdio_client.__wrapped__
    adapted = stdio_compat._transport_factory().__wrapped__
    assert adapted is not original
    assert adapted.__code__ is original.__code__
    assert adapted.__closure__ is original.__closure__
    assert adapted.__defaults__ == original.__defaults__
    assert stdio.TextReceiveStream is TextReceiveStream
    assert original.__globals__["TextReceiveStream"] is TextReceiveStream
    assert adapted.__globals__["TextReceiveStream"] is stdio_compat._LineBufferedTextReceiveStream
    assert {key for key in original.__globals__ if original.__globals__[key] is not adapted.__globals__[key]} == {"TextReceiveStream"}


def test_other_sdk_versions_keep_native_transport(factory, monkeypatch):
    monkeypatch.setattr(stdio_compat, "version", lambda name: "2.4.0")
    assert stdio_compat._transport_factory() is stdio.stdio_client


def test_existing_custom_reader_is_not_overridden(factory, monkeypatch):
    monkeypatch.setattr(stdio, "TextReceiveStream", object())
    assert stdio_compat._transport_factory() is stdio.stdio_client
