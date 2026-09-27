"""Absolute script entrypoint; independent of the conversation's cwd."""
import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from drivers.commandcode_bridge.bridge import Bridge  # noqa: E402
from drivers.stdio_bridge import Peer  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="command-code")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/kaus/commandcode-bridge")
    parser.add_argument("--turn-timeout", type=float, default=1800)
    args = parser.parse_args()
    peer = Peer()
    bridge = Bridge((args.command,), args.state_dir.expanduser().resolve(), peer, args.turn_timeout)
    try:
        await peer.serve(bridge.dispatch)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
