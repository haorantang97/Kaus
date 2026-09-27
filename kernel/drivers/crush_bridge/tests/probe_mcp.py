"""Deterministic MCP tool used only by the opt-in native smoke test."""
import json
import os
from pathlib import Path
import sys


audit = Path(sys.argv[1])
for line in sys.stdin:
    frame = json.loads(line)
    method = frame.get("method")
    with audit.open("a") as output:
        output.write(json.dumps({"method": method, "literal": os.environ.get("KAUS_TEST_LITERAL")}) + "\n")
    if "id" not in frame:
        continue
    if method == "initialize":
        result = {"protocolVersion": frame.get("params", {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "native-mcp-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "native_mcp_marker", "description": "Return a local test marker",
                              "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "native-mcp-marker-ok"}]}
    elif method == "ping":
        result = {}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}), flush=True)
