import asyncio
import json
from pathlib import Path
import sys


async def test_absolute_cli_entry_works_from_an_unrelated_workspace(tmp_path):
    entry = Path(__file__).resolve().parents[1] / "cli.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(entry), "--state-dir", str(tmp_path / "state"),
        cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        frame = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
        process.stdin.write(json.dumps(frame).encode() + b"\n")
        await process.stdin.drain()
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert reply["id"] == 1
        assert reply["result"]["agentCapabilities"]["loadSession"] is True
        # initialize is a protocol handshake, not a claim that Crush is installed.
        assert not (tmp_path / "state").exists()
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
