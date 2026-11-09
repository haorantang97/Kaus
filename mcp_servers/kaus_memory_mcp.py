#!/usr/bin/env python3
"""
Kaus memory MCP —— 让 Kaus 里的每个 Agent 读写所在 Project 的共享记忆。

每条 Kaus 会话启动时由 Kaus 自动挂上（设计稿 2026-11-02 第 5 节），参数里带着会话所在的
Project 与 Agent；Agent 不必知道 Project id。记忆存在 Kaus 的领域库里，换 Agent 不变。

工具：
  kaus_memory_search  按关键词查本 Project（含上级）的有效记忆
  kaus_memory_write   新增 / 补充 / 使失效 / 不变；新增前先返回相似条目让 Agent 选

不弹卡片、不打断用户。上级 Project 的条目只读。只连本机 Kaus，用本机会话 token 鉴权。

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。仅标准库。

参数：--project <projectId> [--agent <backendId>] [--url <Kaus 地址>] [--token-file <路径>]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"

INSTRUCTIONS = (
    "这是所在 Project 在 Kaus 里的共享记忆，换 Agent 也不变。需要项目背景时先用 kaus_memory_search 查。"
    "发现值得长期记住的内容（项目事实、约定、用户偏好、踩过的坑和解决办法、当前目标）时直接用 kaus_memory_write 记下，"
    "不用征求用户同意；一次性的细节、密钥和密码、能从代码里直接看出来的内容不要记。"
)


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "kaus_memory_search",
     "description": "按关键词查本 Project（含上级 Project）的有效记忆，返回条目 id、主题、正文和来源。",
     "inputSchema": _schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
                            ("query",))},
    {"name": "kaus_memory_write",
     "description": "写项目记忆。action=add 新增（有相似条目时先返回它们，确认是新内容再带 confirm=true）；"
                    "action=update 补充或改写已有条目（memory_id）；action=invalidate 让过时条目失效（可带 replaced_by）；"
                    "action=keep 表示没有要改的。kind=overview 写项目概述（这个 Project 做什么、当前目标，只有一条）。"
                    "返回索引长度；超出上限时请在同一次整理里合并或作废旧条目。上级 Project 的条目只读。",
     "inputSchema": _schema({"action": {"type": "string", "enum": ["add", "update", "invalidate", "keep"]},
                             "topic": {"type": "string"}, "body": {"type": "string"},
                             "memory_id": {"type": "string"}, "replaced_by": {"type": "string"},
                             "kind": {"type": "string", "enum": ["note", "overview"]},
                             "confirm": {"type": "boolean"}}, ("action",))},
]


class KausError(RuntimeError):
    pass


class Client:
    def __init__(self, url: str, token_file: Path, project: str, agent: str | None) -> None:
        self.url, self.token_file, self.project, self.agent = url.rstrip("/"), token_file, project, agent

    def _token(self) -> str:
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KausError(f"读不到 Kaus 会话 token：{self.token_file}") from exc

    def http(self, method: str, path: str, body: object | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8")).get("error") or {}
            except ValueError:
                error = {}
            raise KausError(f"{error.get('code', exc.code)}: {error.get('message', '')}".strip()) from exc
        except urllib.error.URLError as exc:
            raise KausError(f"无法连接 Kaus（{self.url}）：{exc.reason}") from exc

    def base(self) -> str:
        return f"/api/projects/{urllib.parse.quote(self.project, safe='')}/memories"

    def call(self, name: str, args: dict) -> dict:
        try:
            if name == "kaus_memory_search":
                query = urllib.parse.urlencode({"q": args["query"], "limit": int(args.get("limit") or 8)})
                return _text(self.http("GET", f"{self.base()}/search?{query}"))
            if name == "kaus_memory_write":
                body = {key: args[key] for key in ("action", "topic", "body", "memory_id", "replaced_by", "kind", "confirm")
                        if key in args}
                if self.agent:
                    body["actor"] = self.agent
                return _text(self.http("POST", f"{self.base()}/write", body))
            return _error(f"未知工具：{name}")
        except (KausError, KeyError, ValueError) as exc:
            return _error(str(exc))


def _text(value: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle_request(client: Client, req: dict) -> dict | None:
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": "kaus-memory", "version": "0.1.0"}, "instructions": INSTRUCTIONS}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": client.call(params.get("name", ""), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def parse_args(argv: list[str]) -> Client:
    parser = argparse.ArgumentParser(prog="kaus_memory_mcp")
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent")
    parser.add_argument("--url", default=os.environ.get("KAUS_URL", "http://127.0.0.1:8877"))
    parser.add_argument("--token-file", default=os.environ.get("KAUS_SESSION_TOKEN_FILE")
                        or os.environ.get("DASH_SESSION_API_TOKEN_FILE")
                        or str(Path(__file__).resolve().parents[1] / "state" / "session_api.token"))
    args = parser.parse_args(argv)
    return Client(args.url, Path(args.token_file), args.project, args.agent)


def main(argv: list[str] | None = None) -> None:
    client = parse_args(sys.argv[1:] if argv is None else argv)
    print(f"kaus-memory MCP 启动 · {client.project}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"解析失败：{exc}\n")
            continue
        response = handle_request(client, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
