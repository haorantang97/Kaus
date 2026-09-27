"""Absolute-path entry point, including when an ACP client changes cwd."""
from pathlib import Path
import asyncio
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from drivers.crush_bridge.__main__ import main


if __name__ == "__main__":
    asyncio.run(main())
