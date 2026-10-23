"""A private stdio broker; MCP transports remain owned by the official SDK.

One native process owns one broker. No model, sampling, account or global agent
configuration is used. EOF/cancellation closes every MCP client and subprocess.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
import httpx2

if __package__:
    from .stdio_compat import stdio_client
else:  # The native extension starts this private broker by absolute file path.
    from stdio_compat import stdio_client

MAX_BYTES = 4 * 1024 * 1024


def read_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Invalid MCP configuration")
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("servers"), list):
        raise ValueError("Invalid MCP configuration")
    names = [server.get("name") if isinstance(server, dict) else None for server in value["servers"]]
    if len(names) > 64 or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Invalid MCP server names")
    return value


def public_tool_name(server, tool):
    clean = lambda text: re.sub(r"[^a-zA-Z0-9_-]", "_", text)
    digest = hashlib.sha256((server + "\0" + tool).encode()).hexdigest()[:10]
    return "mcp__" + clean(server)[:16] + "__" + clean(tool)[:24] + "_" + digest


def pairs(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                                       or not isinstance(row.get("value"), str) for row in rows):
        raise ValueError("Invalid MCP values")
    return {row["name"]: row["value"] for row in rows}


class Broker:
    def __init__(self, config):
        self.config, self.stack = config, AsyncExitStack()
        self.tools, self.catalog = {}, []

    async def open(self):
        await self.stack.__aenter__()
        try:
            for server in self.config["servers"]:
                kind = server.get("type", "stdio")
                if kind == "stdio":
                    if not isinstance(server.get("command"), str) or not server["command"]:
                        raise ValueError("Invalid MCP command")
                    args = server.get("args", [])
                    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                        raise ValueError("Invalid MCP arguments")
                    errlog = self.stack.enter_context(open(os.devnull, "w"))
                    transport = stdio_client(StdioServerParameters(command=server["command"], args=args,
                        env=pairs(server.get("env", [])), cwd=self.config.get("cwd")), errlog=errlog)
                elif kind in ("http", "sse"):
                    url = server.get("url")
                    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                        raise ValueError("Invalid MCP URL")
                    headers = pairs(server.get("headers", []))
                    if kind == "http":
                        http = await self.stack.enter_async_context(httpx2.AsyncClient(headers=headers, timeout=15))
                        transport = streamable_http_client(url, http_client=http)
                    else:
                        transport = sse_client(url, headers=headers, timeout=15, sse_read_timeout=60)
                else:
                    raise ValueError("Unsupported MCP transport")
                client = await self.stack.enter_async_context(Client(transport, read_timeout_seconds=15))
                cursor, seen = None, set()
                for _ in range(64):
                    page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                    for tool in page.tools:
                        name = public_tool_name(server["name"], tool.name)
                        if name in self.tools:
                            raise ValueError("Duplicate MCP tool")
                        self.tools[name] = (client, tool.name)
                        self.catalog.append({"name": name, "title": tool.title or tool.name,
                            "description": tool.description or tool.name, "inputSchema": tool.input_schema,
                            "readOnly": bool(tool.annotations and tool.annotations.read_only_hint is True)})
                    if len(self.catalog) > 1024 or len(json.dumps(self.catalog).encode()) > MAX_BYTES:
                        raise ValueError("MCP catalog exceeds limit")
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("Invalid MCP catalog cursor")
                    seen.add(cursor)
                else:
                    raise ValueError("MCP catalog exceeds page limit")
        except BaseException:
            await self.close()
            raise

    async def call(self, name, arguments):
        if name not in self.tools or not isinstance(arguments, dict):
            raise ValueError("Unknown MCP tool or invalid input")
        client, native_name = self.tools[name]
        result = await client.call_tool(native_name, arguments, read_timeout_seconds=60)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def close(self):
        await self.stack.aclose()


def emit(value):
    data = json.dumps(value, ensure_ascii=False)
    if len(data.encode()) > MAX_BYTES:
        data = json.dumps({"id": value.get("id"), "error": "MCP result exceeds limit"})
    print(data, flush=True)


async def serve(config, *, probe=False):
    broker = Broker(config)
    tasks = {}
    try:
        async with asyncio.timeout(25):
            await broker.open()
        emit({"ready": True, "tools": broker.catalog})
        if probe:
            return
        reader = asyncio.StreamReader(limit=MAX_BYTES + 1)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        async def call(frame):
            ident = frame["id"]
            try:
                result = await broker.call(frame.get("name"), frame.get("arguments", {}))
                emit({"id": ident, "result": result})
            except asyncio.CancelledError:
                emit({"id": ident, "error": "MCP call cancelled"})
            except Exception:
                # Transport diagnostics can contain URLs, commands or secrets.
                emit({"id": ident, "error": "MCP tool call failed"})
            finally:
                tasks.pop(ident, None)
        try:
            while line := await reader.readline():
                if len(line) > MAX_BYTES:
                    raise ValueError("MCP request exceeds limit")
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError("Invalid MCP request")
                if "cancel" in frame:
                    task = tasks.get(frame["cancel"])
                    if task:
                        task.cancel()
                    continue
                ident = frame.get("id")
                if not isinstance(ident, str) or ident in tasks:
                    raise ValueError("Invalid MCP request id")
                tasks[ident] = asyncio.create_task(call(frame))
        finally:
            transport.close()
    finally:
        for task in tuple(tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(tasks.values()), return_exceptions=True)
        await broker.close()


async def main():
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    try:
        await serve(read_config(sys.argv[1]), probe="--probe" in sys.argv[2:])
        return 0
    except asyncio.CancelledError:
        return 130
    except BaseException:
        emit({"ready": False, "error": "MCP connection failed"})
        return 78


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
