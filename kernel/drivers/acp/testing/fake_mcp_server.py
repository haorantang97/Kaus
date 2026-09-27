"""一台**最小可用的假 MCP 服务**（stdio JSON-RPC 2.0，纯标准库）。

它是什么
--------
``probe_adapter --mcp-ping`` 的被挂载对象：探针把它当作一条 ``mcpServers`` 声明
交给 agent，然后问 agent「你能调到它吗」。回答只有两种，而且**不需要相信任何人
的自报**——要么 ``session/update`` 里出现了名为 ``kaus_ping`` 的工具调用，要么
最终文本里出现了只有这台服务知道的 nonce。

它实现三个方法，一个工具
------------------------
- ``initialize`` → ``{protocolVersion, capabilities:{tools:{}}, serverInfo}``；
- ``tools/list`` → 一个工具：``kaus_ping``（无参数）；
- ``tools/call`` → ``{content:[{type:"text", text:"pong-<nonce>"}]}``。

``notifications/initialized`` 之类的通知一律静默吃掉（它们没有 id，按 JSON-RPC
就不该有回执）。不认识的方法回 ``-32601``——**不装作会**，与本仓其它假件同一条
纪律（N §13.1）。

nonce 从哪来
------------
命令行 ``--nonce <串>``。由**调用方**（探针）生成并传进来，这样探针才有一个
只属于这一次运行的判据：agent 复述得出它，就说明这条链真的通了，而不是它凭
「kaus_ping」这个词编了一句话。不给 ``--nonce`` 时自己随机生成一个（手工把玩用）。

安全边界
--------
一行文件读取都没有，一个环境变量都不读，不联网。它唯一的输出就是那句
``pong-<nonce>``。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

SERVER_NAME = "kaus-fake-mcp"
SERVER_VERSION = "0.1.0"

#: 唯一那个工具的名字。探针按它在 ``session/update`` 里找 tool_call。
TOOL_NAME = "kaus_ping"

#: 与本仓 ACP 客户端同一个口径：协议版本是**数据**，不在代码里到处硬写。
MCP_PROTOCOL_VERSION = "2024-11-05"


def tool_descriptor() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Returns a fixed pong string. Call it and repeat its output verbatim."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }


def pong(nonce: str) -> str:
    """这台服务唯一会说的一句话。"""
    return f"pong-{nonce}"


def handle(message: dict[str, Any], nonce: str) -> dict[str, Any] | None:
    """一条请求 → 一条响应。通知（没有 ``id``）返回 ``None``。"""
    method = str(message.get("method", ""))
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": [tool_descriptor()]}
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        if name != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown tool: {name}"},
            }
        result = {
            "content": [{"type": "text", "text": pong(nonce)}],
            "isError": False,
        }
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(nonce: str, stdin=None, stdout=None) -> int:
    """一行一条 JSON-RPC，读到 EOF 为止。"""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle(message, nonce)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_mcp_server",
        description="一台只有 kaus_ping 一个工具的假 MCP 服务（stdio）。",
    )
    parser.add_argument(
        "--nonce",
        default=None,
        help="这次运行的判据串；不给就随机生成（手工把玩用）。",
    )
    args = parser.parse_args(argv)
    return serve(args.nonce or uuid.uuid4().hex[:12])


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(main())
