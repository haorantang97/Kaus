"""Absolute entrypoint so a conversation's working directory cannot break imports."""
import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from drivers.stdio_bridge import Peer  # noqa: E402
from drivers.zcode_bridge.bridge import Bridge  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--command', default='zcode')
    parser.add_argument('--native-arg', action='append', default=[])
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/kaus/zcode-bridge')
    parser.add_argument('--turn-timeout', type=float, default=1800)
    args = parser.parse_args()
    command = (args.command, *args.native_arg, 'app-server', '--stdio')
    peer = Peer()
    bridge = Bridge(command, args.state_dir.expanduser().resolve(), peer, args.turn_timeout)
    try:
        await peer.serve(bridge.dispatch)
    finally:
        await bridge.close()


if __name__ == '__main__':
    asyncio.run(main())
