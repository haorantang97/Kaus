"""短暂启动一个 MCP、按 MCP 标准的 ``tools/list`` 取它的工具清单（2026-11-11）。

product-decisions「MCP 单个工具开关」。传输由官方 MCP SDK 负责（与 ``drivers.session_mcp.broker``
同一套用法）；这里只取名字与说明，取完立刻关掉进程。不调用任何工具。

``server`` 是 ACP ``mcpServers`` 的一条（``drivers.acp.mcp_projection`` 的产物）：
``{"name", "command", "args", "env": [{name, value}]}`` 或 ``{"type": "http"|"sse", "url", "headers": [...]}``。
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import tempfile
from typing import IO, Any, Mapping

MAX_TOOLS = 512
MAX_PAGES = 32
TIMEOUT_SECONDS = 20
#: How much of the server's own stderr is kept as the diagnostic of a failed listing.
STDERR_TAIL = 1500


class McpListError(RuntimeError):
    """``code``：``auth_required``（要登录或密钥才能列出）或 ``failed``。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _pairs(rows: Any) -> dict[str, str]:
    return {row["name"]: row["value"] for row in rows or () if isinstance(row, Mapping)
            and isinstance(row.get("name"), str) and isinstance(row.get("value"), str)}


async def _list(server: Mapping[str, Any], cwd: str | None, errlog: IO[str]) -> list[dict]:
    from mcp.client import Client
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client
    from drivers.session_mcp.stdio_compat import stdio_client
    import httpx2

    async with AsyncExitStack() as stack:
        kind = server.get("type", "stdio") if server.get("url") else "stdio"
        if kind == "stdio":
            transport = stdio_client(StdioServerParameters(
                command=str(server["command"]), args=[str(a) for a in server.get("args") or ()],
                env=_pairs(server.get("env")) or None, cwd=cwd), errlog=errlog)
        elif kind in ("http", "sse"):
            headers = _pairs(server.get("headers"))
            if kind == "http":
                http = await stack.enter_async_context(httpx2.AsyncClient(headers=headers, timeout=15))
                transport = streamable_http_client(str(server["url"]), http_client=http)
            else:
                transport = sse_client(str(server["url"]), headers=headers, timeout=15, sse_read_timeout=30)
        else:
            raise McpListError("failed", "不支持的传输方式")
        client = await stack.enter_async_context(Client(transport, read_timeout_seconds=15))
        tools, cursor, seen = [], None, set()
        for _ in range(MAX_PAGES):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for tool in page.tools:
                tools.append({"name": tool.name, "description": (tool.description or tool.title or "")[:500]})
            cursor = page.next_cursor
            if not cursor or cursor in seen or len(tools) >= MAX_TOOLS:
                break
            seen.add(cursor)
        return tools[:MAX_TOOLS]


def _auth_failure(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in ("401", "403", "unauthorized", "forbidden", "oauth", "auth"))


def _leaves(exc: BaseException) -> list[BaseException]:
    nested = getattr(exc, "exceptions", None)
    return [leaf for inner in nested for leaf in _leaves(inner)] if nested else [exc]


def _diagnostic(errors: list[BaseException], stderr: str, secrets: list[str]) -> str:
    """The innermost errors and the tail of what the server wrote to stderr, with key values masked."""
    lines = list(dict.fromkeys(f"{type(e).__name__}: {e}".rstrip(": ") for e in errors))
    if stderr.strip():
        lines.append(stderr.strip()[-STDERR_TAIL:])
    text = "\n".join(lines)
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def list_tools(server: Mapping[str, Any], *, cwd: str | None = None, timeout: float = TIMEOUT_SECONDS) -> list[dict]:
    """同步入口（在工作线程里调）。失败抛 :class:`McpListError`，消息附原始诊断（密钥值已遮掉）。"""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
        async def run():
            async with asyncio.timeout(timeout):
                return await _list(server, cwd, errlog)
        try:
            return asyncio.run(run())
        except McpListError:
            raise
        except BaseException as exc:  # noqa: BLE001 - every transport failure becomes a code for the tree
            errors = _leaves(exc)
            if any(_auth_failure(error) for error in errors):
                raise McpListError("auth_required", "需要登录或密钥才能列出工具") from exc
            errlog.seek(0)
            detail = _diagnostic(errors, errlog.read(), [*_pairs(server.get("env")).values(),
                                                         *_pairs(server.get("headers")).values()])
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                raise McpListError("failed", "列出工具超时" + (f"\n{detail}" if detail else "")) from exc
            raise McpListError("failed", f"列出工具失败\n{detail}") from exc


__all__ = ["McpListError", "list_tools"]
