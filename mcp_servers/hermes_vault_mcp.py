#!/usr/bin/env python3
"""
Hermes Vault MCP server —— 把仪表盘的全局知识库 REST API 包成 MCP 工具
（vault_list / vault_read / vault_write），让任意 agent 用 tool call 浏览 / 读 / 写同一个库。

设计：知识库是【全局】的（所有 agent 共享一个 llmwiki / Obsidian 库），不分 profile、不继承。
      检索 / RAG 由 llmwiki 负责，这层 MCP 只暴露 浏览 / 读 / 写，不做索引。

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。
依赖：标准库（json / sys / os / urllib）—— 不引外部包，避免 venv 麻烦。

环境变量：
  HERMES_DASHBOARD_URL   默认 http://127.0.0.1:8877。仪表盘 server.py 跑的地址

配置示例（写到任意 profile 的 config.yaml，或主 agent 的 ~/.hermes/config.yaml）：
  mcp_servers:
    vault:
      command: python3
      args: ["/path/to/Kaus/mcp_servers/hermes_vault_mcp.py"]
      env:
        HERMES_DASHBOARD_URL: http://127.0.0.1:8877
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DASHBOARD = os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:8877").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "vault_list",
        "description": "列出全局知识库的文件夹树（所有 agent 共享同一个库）。"
                       "返回每篇笔记的相对路径 / 标题 / 大小，按目录分层缩进。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_read",
        "description": "读取一篇知识库笔记的完整 markdown 内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "笔记相对路径，必须 .md 结尾，例如 'wiki/concepts/rag.md'"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vault_write",
        "description": "写一篇笔记到全局知识库（覆盖已有同名笔记前会自动 .dashbak 备份；新建会自动建中间目录）。"
                       "注意：这是大家共享的库，结构化写作（双链 / frontmatter）建议遵循库里 TheSchema.md 的约定。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "笔记相对路径，必须 .md 结尾"},
                "content": {"type": "string", "description": "markdown 正文"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
]


def _http(method: str, url: str, body: dict | None = None) -> dict:
    """简单 HTTP 调用，返回 JSON dict 或抛 RuntimeError。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method,
                                 headers={"Content-Type": "application/json"} if data else {},
                                 data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = str(e)
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 {DASHBOARD}: {e.reason}") from e


def _flatten(nodes: list, depth: int = 0) -> list:
    """把目录树压成缩进文本行：目录用 [name]/，文件用 标题 + 路径。"""
    lines = []
    for n in nodes:
        pad = "  " * depth
        if n.get("type") == "dir":
            lines.append(f"{pad}[{n['name']}]/")
            lines.extend(_flatten(n.get("children") or [], depth + 1))
        else:
            lines.append(f"{pad}{n.get('title', '')}  ·  {n.get('path', '')}")
    return lines


def call_tool(name: str, args: dict) -> dict:
    """执行 MCP 工具调用，返回 {content: [...], isError?: bool}。"""
    base = f"{DASHBOARD}/api/vault"
    try:
        if name == "vault_list":
            d = _http("GET", base)
            if not d.get("exists"):
                return {"content": [{"type": "text",
                                     "text": f"知识库目录不存在：{d.get('root')}"}], "isError": True}
            body = "\n".join(_flatten(d.get("tree") or [])) or "（空）"
            text = f"全局知识库 · {d.get('root')} · {d.get('file_count', 0)} 篇\n\n{body}"
            return {"content": [{"type": "text", "text": text}]}
        elif name == "vault_read":
            path = args.get("path", "")
            d = _http("GET", f"{base}/note?path={urllib.parse.quote(path)}")
            return {"content": [{"type": "text", "text": f"{d['path']}\n\n{d['content']}"}]}
        elif name == "vault_write":
            path = args.get("path", "")
            content = args.get("content", "")
            d = _http("POST", f"{base}/note", {"path": path, "content": content})
            verb = "覆盖" if d.get("overwritten") else "创建"
            return {"content": [{"type": "text",
                                 "text": f"{verb}成功：{d['path']}（{d['size']} bytes）"}]}
        else:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}
    except RuntimeError as e:
        return {"content": [{"type": "text", "text": f"调用失败: {e}"}], "isError": True}


def handle_request(req: dict) -> dict | None:
    """处理 JSON-RPC 请求；通知（无 id）不返回。"""
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hermes-vault", "version": "0.2.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        return {"jsonrpc": "2.0", "id": rid, "result": call_tool(tool_name, tool_args)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    """读 stdin 一行一条 JSON-RPC，往 stdout 写响应。stderr 留给日志。"""
    print(f"hermes-vault MCP（全局）启动 · dashboard={DASHBOARD}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"解析失败: {e}\n")
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
