"""Run with the Kaus Python environment; native Crush is installed separately."""
import argparse
import asyncio
from pathlib import Path

from .bridge import Bridge
from .protocol import Peer


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crush-command", default="crush")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/kaus/crush-bridge")
    parser.add_argument("--turn-timeout", type=float, default=1800)
    args = parser.parse_args()
    peer = Peer()
    bridge = Bridge(args.crush_command, args.state_dir.expanduser().resolve(), peer, args.turn_timeout)
    try:
        await peer.serve(bridge.dispatch)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
