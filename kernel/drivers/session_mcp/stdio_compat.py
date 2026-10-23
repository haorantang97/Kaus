"""Linear framing for the private broker's pinned MCP 2.3.0 stdio transport.

Upstream source (MIT; notice in MCP-SDK-NOTICE.txt):
https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/stdio.py

Its reader splits the entire accumulated line after every pipe chunk. Large
image lines therefore take quadratic time. Supply complete lines to that reader
without changing its JSON parser, sessions, cancellation or process lifecycle.
The SDK module and other callers retain their original transport.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.metadata import version
import inspect
from types import FunctionType

from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import stdio_client as sdk_stdio_client


class _LineBufferedTextReceiveStream(TextReceiveStream):
    async def receive(self) -> str:
        fragments = []
        while True:
            # Keep AnyIO's incremental decoder, including split UTF-8 sequences.
            chunk = await super().receive()
            fragments.append(chunk)
            if "\n" in chunk:
                return "".join(fragments)


@lru_cache(maxsize=1)
def _transport_factory():
    if version("mcp") != "2.3.0":
        return sdk_stdio_client
    original = getattr(sdk_stdio_client, "__wrapped__", None)
    if (not inspect.isasyncgenfunction(original)
            or original.__globals__.get("TextReceiveStream") is not TextReceiveStream):
        return sdk_stdio_client
    # Reuse the installed function's code, closure and helpers verbatim. Only
    # this function object's global lookup gets our stream; no SDK monkeypatch.
    adapted = FunctionType(original.__code__,
        {**original.__globals__, "TextReceiveStream": _LineBufferedTextReceiveStream},
        original.__name__, original.__defaults__, original.__closure__)
    adapted.__kwdefaults__ = original.__kwdefaults__
    return asynccontextmanager(adapted)


def stdio_client(*args, **kwargs):
    return _transport_factory()(*args, **kwargs)
