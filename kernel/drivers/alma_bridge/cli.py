"""Entrypoint independent of the conversation working directory."""
import argparse
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from drivers.alma_bridge.bridge import Bridge  # noqa: E402
from drivers.stdio_bridge import Peer  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=os.environ.get('ALMA_API_URL', 'http://127.0.0.1:23001'))
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/kaus/alma-bridge')
    args = parser.parse_args()
    bridge = Bridge(args.url, args.state_dir, Peer())
    try:
        await bridge.peer.serve(bridge.dispatch)
    finally:
        await bridge.close()


if __name__ == '__main__':
    asyncio.run(main())
