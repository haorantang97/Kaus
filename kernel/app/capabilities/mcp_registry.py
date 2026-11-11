"""从官方 MCP 登记表添加（2026-11-12，设计稿 mcp-management 第 3 节第三步）。

只读查询 ``registry.modelcontextprotocol.io`` 的 ``/v0/servers``，把每条换成「添加 MCP」表单能直接填入的草稿：
包（npm / PyPI / OCI）→ 本地命令；远程地址（streamable-http / sse）→ 远程地址。环境变量与请求头只给名字，
值由用户填写。查询结果在内存里缓存 10 分钟。不安装、不运行任何东西。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from app.api.api_errors import ApiError

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
CACHE_SECONDS = 600
LIMIT = 30
TIMEOUT_SECONDS = 15
_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")


def _fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Kaus"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read(4 * 1024 * 1024))


def _values(arguments: Any) -> list[str]:
    out: list[str] = []
    for argument in arguments or ():
        if not isinstance(argument, dict):
            continue
        if argument.get("type") == "named" and argument.get("name"):
            out.append(str(argument["name"]))
        value = argument.get("value") if argument.get("value") is not None else argument.get("default")
        if value is not None and "{" not in str(value):
            out.append(str(value))
    return out


def _package_draft(package: dict) -> dict | None:
    kind, identifier, version = package.get("registryType"), package.get("identifier"), package.get("version")
    if not identifier or (package.get("transport") or {}).get("type", "stdio") != "stdio":
        return None
    spec = f"{identifier}@{version}" if version and kind == "npm" else \
        f"{identifier}=={version}" if version and kind == "pypi" else \
        f"{identifier}:{version}" if version and kind == "oci" and ":" not in identifier else identifier
    runtime = _values(package.get("runtimeArguments"))
    if kind == "npm":
        command, args = package.get("runtimeHint") or "npx", [*(runtime or ["-y"]), spec]
    elif kind == "pypi":
        command, args = package.get("runtimeHint") or "uvx", [*runtime, spec]
    elif kind == "oci":
        command, args = "docker", ["run", "-i", "--rm", *runtime, spec]
    else:
        return None
    env = [v for v in package.get("environmentVariables") or () if isinstance(v, dict) and v.get("name")]
    return {"transport": "stdio", "command": command, "args": [*args, *_values(package.get("packageArguments"))],
            "env": [v["name"] for v in env], "required": [v["name"] for v in env if v.get("isRequired")]}


def _remote_draft(remote: dict) -> dict | None:
    if remote.get("type") not in ("streamable-http", "sse") or not str(remote.get("url") or "").startswith("http"):
        return None
    headers = [h for h in remote.get("headers") or () if isinstance(h, dict) and h.get("name")]
    return {"transport": "http", "url": remote["url"], "headers": [h["name"] for h in headers],
            "required": [h["name"] for h in headers if h.get("isRequired")]}


def to_candidate(entry: dict) -> dict | None:
    server = entry.get("server") if isinstance(entry.get("server"), dict) else entry
    name = str(server.get("name") or "")
    draft = next((d for d in (*map(_package_draft, server.get("packages") or ()),
                              *map(_remote_draft, server.get("remotes") or ())) if d), None)
    if not name or draft is None:
        return None
    short = _NAME.sub("-", (server.get("title") or name.rsplit("/", 1)[-1])).strip(" .-_")[:64] or "mcp"
    return {"id": f"{name}@{server.get('version') or ''}", "registryName": name, "name": short,
            "description": str(server.get("description") or "")[:500], "version": server.get("version"),
            "repository": (server.get("repository") or {}).get("url"), "draft": draft}


class McpRegistry:
    def __init__(self, fetch: Callable[[str], dict] | None = None, clock: Callable[[], float] = time.monotonic):
        self.fetch = fetch or _fetch
        self.clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}

    def search(self, query: str = "") -> dict:
        query = query.strip()[:100]
        hit = self._cache.get(query)
        if hit and self.clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        params = {"limit": str(LIMIT), "version": "latest", **({"search": query} if query else {})}
        try:
            data = self.fetch(REGISTRY_URL + "?" + urllib.parse.urlencode(params))
        except Exception as exc:  # noqa: BLE001 - network, TLS or JSON failure
            raise ApiError(502, "mcp_registry_unavailable", "无法连接 MCP 登记表") from exc
        seen, items = set(), []
        for entry in data.get("servers") or () if isinstance(data, dict) else ():
            candidate = to_candidate(entry) if isinstance(entry, dict) else None
            if candidate and candidate["registryName"] not in seen:
                seen.add(candidate["registryName"])
                items.append(candidate)
        result = {"items": items}
        self._cache[query] = (self.clock(), result)
        return result


__all__ = ["McpRegistry", "to_candidate"]
