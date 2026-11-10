#!/usr/bin/env python3
"""
Kaus 内置 MCP —— Kaus 自动附到每条会话的通用工具，不进技能树。

参数里带着会话所在的 Project 与 Agent；Agent 不必知道 Project id。

工具：
  kaus_memory_search   按关键词查本 Project（含上级）的有效记忆
  kaus_memory_write    新增 / 补充 / 使失效 / 不变；新增前先返回相似条目让 Agent 选
  kaus_screen_occupy   接管整个屏幕前占用；被占用时先来后到排队（2026-11-10）
  kaus_screen_release  接管结束后释放

记忆存在 Kaus 的领域库里，换 Agent 不变；上级 Project 的条目只读；只连本机 Kaus，用本机会话
token 鉴权。组内成员会话带 ``--no-memory``：组员写的是组内资料，不进 Project 记忆，只有屏幕两项。

屏幕只是一块牌子（``drivers.computer_use.lease``）：Kaus 不识别具体的电脑操作工具，后台操作
某个窗口不用占用；不调用这两个工具的 Agent 不受保护。本进程退出时牌子随之放下，到期也会放下。

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。

参数：--project <projectId> [--agent <backendId>] [--url <Kaus 地址>] [--token-file <路径>] [--no-memory]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "kaus"

MEMORY_INSTRUCTIONS = (
    "这是所在 Project 在 Kaus 里的共享记忆，换 Agent 也不变。需要项目背景时先用 kaus_memory_search 查。"
    "发现值得长期记住的内容（项目事实、约定、用户偏好、踩过的坑和解决办法、当前目标）时直接用 kaus_memory_write 记下，"
    "不用征求用户同意；一次性的细节、密钥和密码、能从代码里直接看出来的内容不要记。"
    "只在一段时间内成立的状态（进行中的任务、本周安排）写 type=temporary，到期自动收起；"
    "与已有条目相同或几乎相同的内容 Kaus 会自动合并。"
)
SCREEN_INSTRUCTIONS = (
    "接管整个屏幕（移动真实鼠标、往前台窗口打字、抢窗口焦点）之前先调用 kaus_screen_occupy，结束后立刻调用 "
    "kaus_screen_release；被别的 Agent 占用时它会排队，返回「等待」就再调用一次继续等，等到之前不要接管屏幕。"
    "只在后台操作某个窗口时不用占用。"
)

#: 一次占用最长多久、一次调用最多等多久。后者低于常见 MCP 客户端 60 秒的工具超时。
MAX_HOLD_MINUTES = 60
DEFAULT_HOLD_MINUTES = 10
MAX_WAIT_SECONDS = 50
DEFAULT_WAIT_SECONDS = 45


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


MEMORY_TOOLS = [
    {"name": "kaus_memory_search",
     "description": "按关键词查本 Project（含上级 Project）的有效记忆，返回条目 id、主题、正文和来源。",
     "inputSchema": _schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
                            ("query",))},
    {"name": "kaus_memory_write",
     "description": "写项目记忆。action=add 新增（有相似条目时先返回它们，确认是新内容再带 confirm=true）；"
                    "action=update 补充或改写已有条目（memory_id）；action=invalidate 让过时条目失效（可带 replaced_by）；"
                    "action=keep 表示没有要改的。kind=overview 写项目概述（这个 Project 做什么、当前目标，只有一条）。"
                    "type=fact（缺省）长期有效；type=temporary 是临时状态，expires_days 天后（缺省 14，最长 90）自动收起。"
                    "与已有条目相同或几乎相同的新增会直接并进那一条（action=merged）。"
                    "返回索引长度；超出上限时请在同一次整理里合并或作废旧条目。上级 Project 的条目只读。",
     "inputSchema": _schema({"action": {"type": "string", "enum": ["add", "update", "invalidate", "keep"]},
                             "topic": {"type": "string"}, "body": {"type": "string"},
                             "memory_id": {"type": "string"}, "replaced_by": {"type": "string"},
                             "kind": {"type": "string", "enum": ["note", "overview"]},
                             "type": {"type": "string", "enum": ["fact", "temporary"]},
                             "expires_days": {"type": "integer", "minimum": 1, "maximum": 90},
                             "confirm": {"type": "boolean"}}, ("action",))},
]
SCREEN_TOOLS = [
    {"name": "kaus_screen_occupy",
     "description": "接管整个屏幕前调用：占用这台电脑的屏幕，同一时间只有一个 Agent 能占用，其余先来后到排队。"
                    "返回 occupied 才能接管；返回 waiting 时带着当前占用者和你的位置，再调用一次继续等。"
                    "已占用时再调用就是续期。到期（minutes，默认 10）自动释放。后台操作某个窗口不用占用。",
     "inputSchema": _schema({"minutes": {"type": "integer", "minimum": 1, "maximum": MAX_HOLD_MINUTES},
                             "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS}})},
    {"name": "kaus_screen_release",
     "description": "接管屏幕结束后立刻调用，把屏幕让给排队的 Agent；也会取消你的排队。",
     "inputSchema": _schema({})},
]


class KausError(RuntimeError):
    pass


class Client:
    """Kaus 本机接口；``memory=False`` 时不提供记忆工具（组内成员）。"""

    def __init__(self, url: str, token_file: Path, project: str, agent: str | None, *,
                 memory: bool = True, screen: "Screen | None" = None) -> None:
        self.url, self.token_file, self.project, self.agent = url.rstrip("/"), token_file, project, agent
        self.memory, self.screen = memory, screen

    @property
    def tools(self) -> list[dict]:
        return [*(MEMORY_TOOLS if self.memory else ()), *SCREEN_TOOLS]

    @property
    def instructions(self) -> str:
        return (MEMORY_INSTRUCTIONS if self.memory else "") + SCREEN_INSTRUCTIONS

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
            if name == "kaus_memory_search" and self.memory:
                query = urllib.parse.urlencode({"q": args["query"], "limit": int(args.get("limit") or 8)})
                return _text(self.http("GET", f"{self.base()}/search?{query}"))
            if name == "kaus_memory_write" and self.memory:
                body = {key: args[key] for key in ("action", "topic", "body", "memory_id", "replaced_by", "kind", "type",
                                                   "expires_days", "confirm") if key in args}
                if self.agent:
                    body["actor"] = self.agent
                return _text(self.http("POST", f"{self.base()}/write", body))
            if name == "kaus_screen_occupy":
                return _text(self._screen().occupy(args.get("minutes"), args.get("wait_seconds")))
            if name == "kaus_screen_release":
                return _text(self._screen().release())
            return _error(f"未知工具：{name}")
        except (KausError, KeyError, ValueError, OSError) as exc:
            return _error(str(exc))

    def _screen(self) -> "Screen":
        if self.screen is None:
            self.screen = Screen({"agent": self.agent, "project": self.project})
        return self.screen


def _bounded(value: object, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("参数必须是数字")
    return max(low, min(high, int(value)))


class Screen:
    """本会话手里的屏幕牌子。"""

    def __init__(self, owner: dict, *, root: str | Path | None = None) -> None:
        _kernel_path()
        from drivers.computer_use.lease import ResourceLease
        self.lease = ResourceLease("desktop:local", root=root, owner={k: v for k, v in owner.items() if v})

    def occupy(self, minutes: object = None, wait_seconds: object = None) -> dict:
        hold = _bounded(minutes, DEFAULT_HOLD_MINUTES, 1, MAX_HOLD_MINUTES) * 60
        wait = _bounded(wait_seconds, DEFAULT_WAIT_SECONDS, 0, MAX_WAIT_SECONDS)
        if self.lease.acquire(timeout=wait, hold_seconds=hold):
            return {"status": "occupied", "expiresAt": _clock(self.lease.expires_at),
                    "next": "接管完成后调用 kaus_screen_release。"}
        holder = self.lease.holder()
        return {"status": "waiting", "position": self.lease.position(),
                **({"holder": holder} if holder else {}),
                "next": "屏幕正被占用。再次调用 kaus_screen_occupy 继续排队；等到之前不要接管屏幕。"}

    def release(self) -> dict:
        held = self.lease.held
        self.lease.release()
        return {"status": "released" if held else "not_held"}


def _clock(value: float | None) -> str | None:
    return time.strftime("%H:%M:%S", time.localtime(value)) if value else None


def _kernel_path() -> None:
    kernel = str(Path(__file__).resolve().parents[1] / "kernel")
    if kernel not in sys.path:
        sys.path.insert(0, kernel)


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
            "serverInfo": {"name": SERVER_NAME, "version": "0.2.0"}, "instructions": client.instructions}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": client.tools}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": client.call(params.get("name", ""), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def parse_args(argv: list[str]) -> Client:
    parser = argparse.ArgumentParser(prog="kaus_builtin_mcp")
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent")
    parser.add_argument("--url", default=os.environ.get("KAUS_URL", "http://127.0.0.1:8877"))
    parser.add_argument("--token-file", default=os.environ.get("KAUS_SESSION_TOKEN_FILE")
                        or os.environ.get("DASH_SESSION_API_TOKEN_FILE")
                        or str(Path(__file__).resolve().parents[1] / "state" / "session_api.token"))
    parser.add_argument("--no-memory", action="store_true")
    args = parser.parse_args(argv)
    return Client(args.url, Path(args.token_file), args.project, args.agent, memory=not args.no_memory)


def main(argv: list[str] | None = None) -> None:
    client = parse_args(sys.argv[1:] if argv is None else argv)
    print(f"Kaus 内置 MCP 启动 · {client.project}", file=sys.stderr)
    try:
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
    finally:
        if client.screen is not None:
            client.screen.lease.release()


if __name__ == "__main__":
    main()
